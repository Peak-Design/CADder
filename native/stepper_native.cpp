/*
 * stepper_native.cpp: Fast mesh extraction from OCC triangulations.
 *
 * Python extension module that receives a BinTools-serialized compound of
 * faces (bytes), deserializes it with its own OCCT copy, meshes any face
 * still missing triangulation, extracts triangulation data (vertices,
 * normals, UVs, faces) from each face in compound order, and returns
 * concatenated numpy arrays ready for Blender import.
 *
 * The serialize handoff keeps this module independent of the Python OCC
 * bindings (OCP/pybind11): only bytes cross the boundary, so the two OCCT
 * copies in the process never exchange live objects.
 *
 * Copyright 2026 Peak-Design
 * License: GPL v3
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>

/* numpy C-API */
#define NPY_NO_DEPRECATED_API NPY_1_7_API_VERSION
#include <numpy/arrayobject.h>

/* OpenCascade headers */
#include <BRep_Tool.hxx>
#include <BRepMesh_IncrementalMesh.hxx>
#include <IMeshTools_Parameters.hxx>
#include <Precision.hxx>
#include <BinTools.hxx>
#include <Poly_Triangulation.hxx>
#include <Standard_Failure.hxx>
#include <TopAbs_Orientation.hxx>
#include <TopAbs_ShapeEnum.hxx>
#include <TopExp_Explorer.hxx>
#include <TopLoc_Location.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Shape.hxx>
#include <gp_Pnt.hxx>
#include <gp_Pnt2d.hxx>

#include <cmath>
#include <cstdint>
#include <sstream>
#include <string>
#include <vector>

/* ------------------------------------------------------------------ */
/* Helper: extract triangulation data from a single face              */
/* ------------------------------------------------------------------ */
struct FaceMesh {
    std::vector<float> verts;   /* Nx3 flattened */
    std::vector<float> norms;   /* Nx3 flattened (placeholder) */
    std::vector<float> uvs;     /* Nx2 flattened */
    std::vector<int32_t> faces; /* Tx3 flattened (global indices) */
    int n_verts = 0;
    int n_tris  = 0;
    bool failed = false;
    bool undef_norms = false;
};

static FaceMesh extract_one_face(const TopoDS_Face& face,
                                 int global_vert_offset)
{
    FaceMesh fm;
    TopLoc_Location loc;
    Handle(Poly_Triangulation) tri = BRep_Tool::Triangulation(face, loc);

    if (tri.IsNull() || tri->NbTriangles() == 0 || tri->NbNodes() == 0) {
        fm.failed = true;
        return fm;
    }

    bool not_forward = (face.Orientation() != TopAbs_FORWARD);
    int nb_nodes = tri->NbNodes();
    int nb_tris  = tri->NbTriangles();
    bool has_uvs = tri->HasUVNodes();

    fm.n_verts = nb_nodes;
    fm.n_tris  = nb_tris;
    fm.verts.resize(nb_nodes * 3);
    fm.norms.resize(nb_nodes * 3);
    fm.uvs.resize(nb_nodes * 2);
    fm.faces.resize(nb_tris * 3);

    /* Extract vertices and UVs */
    for (int i = 1; i <= nb_nodes; ++i) {
        gp_Pnt pt = tri->Node(i);
        /* Node positions are already in the triangulation's local frame.
         * The location embedded in the triangulation (loc) is the mesh
         * placement; we must apply it so coordinates are in shape space. */
        if (!loc.IsIdentity()) {
            pt.Transform(loc);
        }
        int idx = (i - 1) * 3;
        fm.verts[idx + 0] = static_cast<float>(pt.X());
        fm.verts[idx + 1] = static_cast<float>(pt.Y());
        fm.verts[idx + 2] = static_cast<float>(pt.Z());

        /* UVs */
        int ui = (i - 1) * 2;
        if (has_uvs) {
            gp_Pnt2d uv = tri->UVNode(i);
            fm.uvs[ui + 0] = static_cast<float>(uv.X());
            fm.uvs[ui + 1] = static_cast<float>(uv.Y());
        } else {
            fm.uvs[ui + 0] = 0.0f;
            fm.uvs[ui + 1] = 0.0f;
        }

        /* Placeholder normals; Python recomputes from analytic surface */
        int ni = (i - 1) * 3;
        fm.norms[ni + 0] = 0.0f;
        fm.norms[ni + 1] = 0.0f;
        fm.norms[ni + 2] = 1.0f;
    }
    /* undef_norms stays false. A placeholder is not an undefined normal:
     * Python calculates the real normals from the CAD surface after this
     * (_recompute_face_normals). Set here, it made every native import
     * report each face under "Undefined normals". */

    /* Extract triangles with global vertex offset */
    for (int i = 1; i <= nb_tris; ++i) {
        int n1, n2, n3;
        tri->Triangle(i).Get(n1, n2, n3);
        if (not_forward) {
            std::swap(n1, n2);
        }
        int idx = (i - 1) * 3;
        fm.faces[idx + 0] = (n1 - 1) + global_vert_offset;
        fm.faces[idx + 1] = (n2 - 1) + global_vert_offset;
        fm.faces[idx + 2] = (n3 - 1) + global_vert_offset;
    }

    return fm;
}

/* ------------------------------------------------------------------ */
/* Python-callable function                                           */
/* ------------------------------------------------------------------ */
static PyObject* py_mesh_and_extract(PyObject* /*self*/, PyObject* args)
{
    const char* data = nullptr;
    Py_ssize_t data_len = 0;
    double lin_def = 0.8;
    double ang_def = 0.5;
    int relative = 0;

    if (!PyArg_ParseTuple(args, "y#dd|p", &data, &data_len, &lin_def, &ang_def,
                          &relative))
        return NULL;

    /* Deserialize, mesh and extract without holding the GIL. This is the
     * expensive, pure-C++ part. */
    std::vector<FaceMesh> meshes;
    int total_verts = 0;
    int total_tris  = 0;
    std::string error_msg;

    Py_BEGIN_ALLOW_THREADS
    try {
        std::string buffer(data, static_cast<size_t>(data_len));
        std::istringstream stream(buffer, std::ios::in | std::ios::binary);

        TopoDS_Shape shape;
        BinTools::Read(shape, stream);
        if (shape.IsNull()) {
            error_msg = "BinTools::Read produced a null shape";
        } else {
            /* Safety net: mesh faces that still lack triangulation.  Faces
             * already meshed at compatible deflection are skipped by OCCT,
             * so this is a no-op for the normal pre-tessellated handoff. */
            IMeshTools_Parameters mp;
            mp.Deflection = lin_def;
            mp.Angle = ang_def;
            mp.Relative = relative ? Standard_True : Standard_False;
            mp.InParallel = relative ? Standard_True : Standard_False;
            mp.MinSize = Precision::Confusion();
            BRepMesh_IncrementalMesh mesher(shape, mp);

            for (TopExp_Explorer ex(shape, TopAbs_FACE); ex.More(); ex.Next()) {
                const TopoDS_Face& face = TopoDS::Face(ex.Current());
                FaceMesh fm = extract_one_face(face, total_verts);
                if (!fm.failed) {
                    total_verts += fm.n_verts;
                    total_tris  += fm.n_tris;
                }
                meshes.push_back(std::move(fm));
            }
        }
    } catch (const Standard_Failure& e) {
        error_msg = std::string("OCCT exception: ") +
                    (e.GetMessageString() ? e.GetMessageString() : "unknown");
    } catch (const std::exception& e) {
        error_msg = std::string("C++ exception: ") + e.what();
    }
    Py_END_ALLOW_THREADS

    if (!error_msg.empty()) {
        PyErr_SetString(PyExc_ValueError, error_msg.c_str());
        return NULL;
    }

    Py_ssize_t n = static_cast<Py_ssize_t>(meshes.size());

    /* Allocate output numpy arrays */
    npy_intp verts_dims[2] = {total_verts, 3};
    npy_intp norms_dims[2] = {total_verts, 3};
    npy_intp uvs_dims[2]   = {total_verts, 2};
    npy_intp faces_dims[2] = {total_tris,  3};
    npy_intp n_faces_dim   = {n};

    PyObject* all_verts = PyArray_ZEROS(2, verts_dims, NPY_FLOAT32, 0);
    PyObject* all_norms = PyArray_ZEROS(2, norms_dims, NPY_FLOAT32, 0);
    PyObject* all_uvs   = PyArray_ZEROS(2, uvs_dims,   NPY_FLOAT32, 0);
    PyObject* all_faces = PyArray_ZEROS(2, faces_dims, NPY_INT32, 0);

    PyObject* face_starts  = PyArray_ZEROS(1, &n_faces_dim, NPY_INT32, 0);
    PyObject* face_counts  = PyArray_ZEROS(1, &n_faces_dim, NPY_INT32, 0);
    PyObject* vert_starts  = PyArray_ZEROS(1, &n_faces_dim, NPY_INT32, 0);
    PyObject* vert_counts  = PyArray_ZEROS(1, &n_faces_dim, NPY_INT32, 0);
    PyObject* failed_mask  = PyArray_ZEROS(1, &n_faces_dim, NPY_BOOL, 0);
    PyObject* undef_mask   = PyArray_ZEROS(1, &n_faces_dim, NPY_BOOL, 0);

    if (!all_verts || !all_norms || !all_uvs || !all_faces ||
        !face_starts || !face_counts || !vert_starts || !vert_counts ||
        !failed_mask || !undef_mask) {
        Py_XDECREF(all_verts); Py_XDECREF(all_norms);
        Py_XDECREF(all_uvs);   Py_XDECREF(all_faces);
        Py_XDECREF(face_starts); Py_XDECREF(face_counts);
        Py_XDECREF(vert_starts); Py_XDECREF(vert_counts);
        Py_XDECREF(failed_mask); Py_XDECREF(undef_mask);
        return PyErr_NoMemory();
    }

    /* Get raw data pointers */
    float*   v_ptr = static_cast<float*>(PyArray_DATA((PyArrayObject*)all_verts));
    float*   n_ptr = static_cast<float*>(PyArray_DATA((PyArrayObject*)all_norms));
    float*   u_ptr = static_cast<float*>(PyArray_DATA((PyArrayObject*)all_uvs));
    int32_t* f_ptr = static_cast<int32_t*>(PyArray_DATA((PyArrayObject*)all_faces));

    int32_t* fs_ptr = static_cast<int32_t*>(PyArray_DATA((PyArrayObject*)face_starts));
    int32_t* fc_ptr = static_cast<int32_t*>(PyArray_DATA((PyArrayObject*)face_counts));
    int32_t* vs_ptr = static_cast<int32_t*>(PyArray_DATA((PyArrayObject*)vert_starts));
    int32_t* vc_ptr = static_cast<int32_t*>(PyArray_DATA((PyArrayObject*)vert_counts));
    npy_bool* fl_ptr = static_cast<npy_bool*>(PyArray_DATA((PyArrayObject*)failed_mask));
    npy_bool* un_ptr = static_cast<npy_bool*>(PyArray_DATA((PyArrayObject*)undef_mask));

    /* Copy data from per-face meshes into concatenated arrays */
    int v_offset = 0;
    int f_offset = 0;

    for (Py_ssize_t i = 0; i < n; ++i) {
        const FaceMesh& fm = meshes[i];

        fl_ptr[i] = fm.failed ? NPY_TRUE : NPY_FALSE;
        un_ptr[i] = fm.undef_norms ? NPY_TRUE : NPY_FALSE;

        if (fm.failed) {
            fs_ptr[i] = f_offset;
            fc_ptr[i] = 0;
            vs_ptr[i] = v_offset;
            vc_ptr[i] = 0;
            continue;
        }

        vs_ptr[i] = v_offset;
        vc_ptr[i] = fm.n_verts;
        fs_ptr[i] = f_offset;
        fc_ptr[i] = fm.n_tris;

        /* Copy vertices: n_verts * 3 floats */
        std::memcpy(v_ptr + v_offset * 3, fm.verts.data(),
                    fm.n_verts * 3 * sizeof(float));
        /* Copy normals */
        std::memcpy(n_ptr + v_offset * 3, fm.norms.data(),
                    fm.n_verts * 3 * sizeof(float));
        /* Copy UVs: n_verts * 2 floats */
        std::memcpy(u_ptr + v_offset * 2, fm.uvs.data(),
                    fm.n_verts * 2 * sizeof(float));
        /* Copy faces: n_tris * 3 int32s */
        std::memcpy(f_ptr + f_offset * 3, fm.faces.data(),
                    fm.n_tris * 3 * sizeof(int32_t));

        v_offset += fm.n_verts;
        f_offset += fm.n_tris;
    }

    /* Return tuple of 10 arrays */
    PyObject* result = PyTuple_Pack(10,
        all_verts, all_norms, all_uvs, all_faces,
        face_starts, face_counts, vert_starts, vert_counts,
        failed_mask, undef_mask);

    /* PyTuple_Pack increments refcounts; release our references */
    Py_DECREF(all_verts); Py_DECREF(all_norms);
    Py_DECREF(all_uvs);   Py_DECREF(all_faces);
    Py_DECREF(face_starts); Py_DECREF(face_counts);
    Py_DECREF(vert_starts); Py_DECREF(vert_counts);
    Py_DECREF(failed_mask); Py_DECREF(undef_mask);

    return result;
}

/* ------------------------------------------------------------------ */
/* Module definition                                                  */
/* ------------------------------------------------------------------ */
static PyMethodDef methods[] = {
    {"mesh_and_extract", py_mesh_and_extract, METH_VARARGS,
     "Deserialize a BinTools compound of faces, mesh missing faces and\n"
     "extract all triangulations.\n\n"
     "Args:\n"
     "    brep_bytes: bytes - BinTools-serialized compound (triangulation\n"
     "        payload included; faces are extracted in compound order)\n"
     "    lin_def: float - linear deflection for the safety-net meshing\n"
     "    ang_def: float - angular deflection for the safety-net meshing\n\n"
     "Returns:\n"
     "    Tuple of 10 numpy arrays:\n"
     "    (all_verts, all_norms, all_uvs, all_faces,\n"
     "     face_starts, face_counts, vert_starts, vert_counts,\n"
     "     failed_mask, undef_mask)"},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef module_def = {
    PyModuleDef_HEAD_INIT,
    "stepper_native",
    "Native C++ mesh extraction for CADder Blender addon.",
    -1,
    methods
};

PyMODINIT_FUNC PyInit_stepper_native(void)
{
    import_array();  /* Initialize numpy C-API */
    PyObject* m = PyModule_Create(&module_def);
    if (m != NULL) {
        PyModule_AddIntConstant(m, "ABI_VERSION", 2);
    }
    return m;
}
