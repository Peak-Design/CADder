# SPDX-License-Identifier: GPL-3.0-or-later
"""Rig-manifest parsing and validation.

No bpy import: this module runs under plain Python in CI. The shape of the
file is fixed by schema/rig-manifest.schema.json and its meaning by
schema/SCHEMA.md. This parser accepts unknown extra fields (minor-version
additions) and refuses unknown major versions.

Limits are stored as the file carries them (absolute mate values plus
value_at_rest). Consumers pose in deltas from rest: use Limit.delta_min /
Limit.delta_max.
"""

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

SUPPORTED_MAJOR = 1

JOINT_TYPES = ("fixed", "revolute", "prismatic", "cylindrical", "ball", "planar",
               "pin_slot", "screw", "path", "surface", "free")
COUPLING_KINDS = ("gear", "rack_pinion", "screw", "linear_coupler", "mirror", "table", "cam")

Vec3 = Tuple[float, float, float]
Mat4 = Tuple[Tuple[float, ...], ...]  # 4 rows of 4, row-major [R|t]


class ManifestError(ValueError):
    """A manifest that must not be consumed. The message says why, naming the
    offending id where there is one."""


@dataclass
class Limit:
    min: float
    max: float
    value_at_rest: float

    @property
    def delta_min(self) -> float:
        return self.min - self.value_at_rest

    @property
    def delta_max(self) -> float:
        return self.max - self.value_at_rest


@dataclass
class Coupling:
    kind: str
    driver_joint: Optional[str] = None
    ratio: Optional[float] = None
    meters_per_radian: Optional[float] = None
    lead_m_per_rev: Optional[float] = None
    # mirror only: the driven joint's body poses as the exact mirror image
    # of the driver's body across this plane (SCHEMA.md, 2026-08-23).
    mirror_plane_point: Optional[Vec3] = None
    mirror_plane_normal: Optional[Vec3] = None
    # mirror only: how much of the pose is reflected. "plane" is a symmetric
    # MATE between planar faces: a plane-to-plane relation, so only the
    # translation along the normal and the two tilts follow. "rigid" is an
    # assembly MIRROR FEATURE, where the instance is a full reflection of its
    # source and every channel follows.
    mirror_scope: str = "plane"
    # table only: the driven joint's value as a sampled function of the
    # driver's, [[x, y], ...] with x ascending, both in the joints' own
    # units relative to the exported pose (SCHEMA.md, 2026-09-15). A cam
    # profile or a universal joint's fluctuation, read off the SolidWorks
    # solver; interpolated linearly, repeated every `period` when periodic.
    samples: Optional[List[Tuple[float, float]]] = None
    periodic: bool = False
    period: float = 0.0
    # cam only: the cam path's faces (global metres, rest pose), the cam's
    # joint axis with a point on it, and the follower's contact entity. The
    # rig holds the follower on the faces live (cam_contact.py), for a cam
    # the exporter could not table: free in its plane, on a slide, or the
    # probe off (SCHEMA.md, 2026-09-15).
    cam_axis: Optional[Vec3] = None
    cam_origin: Optional[Vec3] = None
    cam_surface_points: Optional[List[Vec3]] = None
    cam_surface_triangles: Optional[List[List[int]]] = None
    follower_kind: Optional[str] = None          # vertex | roller | flat
    follower_point: Optional[Vec3] = None
    follower_axis: Optional[Vec3] = None         # roller
    follower_radius: Optional[float] = None      # roller
    follower_normal: Optional[Vec3] = None       # flat


@dataclass
class Component:
    id: str
    sw_path: str
    step_name: str
    transform: Mat4
    sw_persistent_id: Optional[str] = None
    step_occurrence_path: Optional[str] = None
    bbox_min: Optional[Vec3] = None
    bbox_max: Optional[Vec3] = None
    suppressed: bool = False
    subassembly_solving: Optional[str] = None


@dataclass
class RigidGroup:
    id: str
    name: str
    components: List[str]
    grounded: bool
    frame: Optional[Mat4] = None
    bbox_diag: Optional[float] = None


@dataclass
class Joint:
    id: str
    type: str
    parent_group: str
    child_group: str
    origin: Optional[Vec3] = None
    axis: Optional[Vec3] = None
    secondary_axis: Optional[Vec3] = None
    rotation_limit: Optional[Limit] = None
    translation_limit: Optional[Limit] = None
    coupling: Optional[Coupling] = None
    source_mates: List[Dict[str, str]] = field(default_factory=list)
    confidence: str = "high"
    notes: Optional[str] = None
    # Path joints only: the sampled curve (ordered, global metres) and
    # whether it closes on itself. None for every other type.
    path_points: Optional[List[Vec3]] = None
    path_closed: bool = False
    # Surface joints only: the face the child's point stays on, triangulated
    # in global metres. None for every other type.
    surface_points: Optional[List[Vec3]] = None
    surface_triangles: Optional[List[List[int]]] = None


@dataclass
class DriverCandidate:
    """One input the exporter weighed for a loop, with the cut and closure
    that choice implies. Applied whole, never the joint alone (inputs.py)."""
    joint: str
    closure_joint: str
    closure_kind: str = "ik"


@dataclass
class Loop:
    id: str
    member_joints: List[str]
    closure_joint: str
    suggested_driver_joint: Optional[str] = None
    planar: bool = False
    plane_normal: Optional[Vec3] = None
    # How many inputs this loop takes: its joints' freedom less the three a
    # planar closure spends. A loop of mobility m must leave m-1 bones of
    # the driven chain OUT of the solve, nearest the root, for the user to
    # pose. 1 for almost every loop, and for every manifest written before
    # the field.
    mobility: int = 1
    # Every input the exporter weighed, the chosen one first. Empty for
    # manifests written before the field.
    driver_candidates: List[DriverCandidate] = field(default_factory=list)
    # How to re-close the cut. "ik" is a point coincidence solved by rotating
    # the driven chain. "aim_pair" is a slider-crank: the bodies either side
    # of the cut hang off their own pins and aim at each other, because no
    # rotational solver can lengthen a slide. "none" means the exporter cut
    # the loop so that the TREE already carries the motion, and solving it
    # would move a body the mates never let move. Absent means "ik", that is
    # what every manifest written before the field meant.
    closure_kind: str = "ik"


@dataclass
class InputOption:
    """One input a mechanism can take, as a complete alternative: the
    mechanism's loops under that input (same ids, same order) and the
    joints whose parent and child swap because the new tree reaches them
    from the other side. Applied whole (inputs.py)."""
    joint: str
    loops: List[Loop]
    flipped_joints: List[str] = field(default_factory=list)
    # Joints whose limits differ under this input: joint id -> (rotation,
    # translation), each a Limit or None. A stroke limit derived onto a
    # slider-crank's crank belongs to the crank-driven configuration.
    joint_limits: Dict[str, Tuple[Optional["Limit"], Optional["Limit"]]] = field(
        default_factory=dict)


@dataclass
class Mechanism:
    """Loops that share joints: one degree of freedom, one input. The
    exporter's choice is inputs[0]; `active` is the option applied now."""
    id: str
    loop_ids: List[str]
    inputs: List[InputOption]
    active: int = 0
    # The manifest's own limits of every joint an option has changed, so
    # leaving the option puts them back.
    original_limits: Dict[str, Tuple[Optional["Limit"], Optional["Limit"]]] = field(
        default_factory=dict)


@dataclass
class Warning:
    code: str
    message: str
    components: List[str] = field(default_factory=list)
    joints: List[str] = field(default_factory=list)


@dataclass
class Manifest:
    manifest_version: str
    generator: Dict[str, str]
    step_file: str
    step_sha1: Optional[str]
    components: List[Component]
    rigid_groups: List[RigidGroup]
    joints: List[Joint]
    loops: List[Loop]
    warnings: List[Warning]
    source_path: Optional[str] = None
    # Empty for manifests written before the field: the per-loop
    # candidates then stand in, applied loop by loop.
    mechanisms: List[Mechanism] = field(default_factory=list)

    def component_by_id(self) -> Dict[str, Component]:
        return {c.id: c for c in self.components}

    def group_by_id(self) -> Dict[str, RigidGroup]:
        return {g.id: g for g in self.rigid_groups}

    def joint_by_id(self) -> Dict[str, Joint]:
        return {j.id: j for j in self.joints}

    def grounded_groups(self) -> List[RigidGroup]:
        return [g for g in self.rigid_groups if g.grounded]


def _vec3(value, what: str) -> Vec3:
    if not (isinstance(value, (list, tuple)) and len(value) == 3):
        raise ManifestError(f"{what}: expected a 3-vector, got {value!r}")
    return (float(value[0]), float(value[1]), float(value[2]))



def _closure_kind(lp: dict) -> str:
    kind = lp.get("closure_kind", "ik")
    if kind not in ("ik", "aim_pair", "none"):
        raise ManifestError(
            f"loop {lp['id']}: closure_kind {kind!r} is not one of "
            "'ik', 'aim_pair', 'none'")
    return kind

def _opt_vec3(value, what: str) -> Optional[Vec3]:
    return None if value is None else _vec3(value, what)


def _mat4(value, what: str) -> Mat4:
    if not (isinstance(value, (list, tuple)) and len(value) == 4
            and all(isinstance(r, (list, tuple)) and len(r) == 4 for r in value)):
        raise ManifestError(f"{what}: expected a 4x4 row-major matrix")
    return tuple(tuple(float(x) for x in row) for row in value)


def _limit(value, what: str) -> Optional[Limit]:
    if value is None:
        return None
    try:
        lim = Limit(min=float(value["min"]), max=float(value["max"]),
                    value_at_rest=float(value["value_at_rest"]))
    except (KeyError, TypeError) as exc:
        raise ManifestError(f"{what}: bad limit object ({exc})")
    if lim.min > lim.max:
        raise ManifestError(f"{what}: limit min {lim.min} exceeds max {lim.max}")
    return lim


def parse(data: dict, source_path: Optional[str] = None) -> Manifest:
    """Parses and validates an already-decoded manifest dict.

    Raises ManifestError on anything that would make the rig wrong: unknown
    major version, dangling ids, a joint with an axis but no secondary axis,
    a loop closure joint outside its member list.
    """
    version = data.get("manifest_version")
    if not isinstance(version, str) or not version.count(".") == 2:
        raise ManifestError(f"manifest_version missing or malformed: {version!r}")
    major = int(version.split(".")[0])
    if major != SUPPORTED_MAJOR:
        raise ManifestError(
            f"manifest_version {version} is major {major}; this add-on supports major {SUPPORTED_MAJOR}")

    units = data.get("units", {})
    if units.get("length") != "meter" or units.get("angle") != "radian":
        raise ManifestError(f"unsupported units: {units!r} (expected meter/radian)")
    frame = data.get("frame", {})
    if frame.get("up_axis") != "Z" or frame.get("handedness") != "right":
        raise ManifestError(f"unsupported frame: {frame!r} (expected right-handed Z-up)")

    step = data.get("step_export") or {}
    if not step.get("file"):
        raise ManifestError("step_export.file is missing")

    components = []
    for c in data.get("components", []):
        components.append(Component(
            id=c["id"],
            sw_path=c["sw_path"],
            step_name=c["step_name"],
            transform=_mat4(c["transform"], f"component {c['id']} transform"),
            sw_persistent_id=c.get("sw_persistent_id"),
            step_occurrence_path=c.get("step_occurrence_path"),
            bbox_min=_opt_vec3((c.get("bbox_local") or {}).get("min"), f"component {c['id']} bbox min"),
            bbox_max=_opt_vec3((c.get("bbox_local") or {}).get("max"), f"component {c['id']} bbox max"),
            suppressed=bool(c.get("suppressed", False)),
            subassembly_solving=c.get("subassembly_solving"),
        ))
    component_ids = {c.id for c in components}
    if len(component_ids) != len(components):
        raise ManifestError("duplicate component ids")

    groups = []
    for g in data.get("rigid_groups", []):
        missing = [cid for cid in g["components"] if cid not in component_ids]
        if missing:
            raise ManifestError(f"rigid group {g['id']} references unknown components {missing}")
        groups.append(RigidGroup(
            id=g["id"],
            name=g.get("name") or g["id"],
            components=list(g["components"]),
            grounded=bool(g["grounded"]),
            frame=None if g.get("frame") is None else _mat4(g["frame"], f"group {g['id']} frame"),
            bbox_diag=g.get("bbox_diag"),
        ))
    group_ids = {g.id for g in groups}
    if len(group_ids) != len(groups):
        raise ManifestError("duplicate rigid group ids")

    joints = []
    for j in data.get("joints", []):
        jid = j["id"]
        jtype = j["type"]
        if jtype not in JOINT_TYPES:
            raise ManifestError(f"joint {jid}: unknown type {jtype!r}")
        for side in ("parent_group", "child_group"):
            if j[side] not in group_ids:
                raise ManifestError(f"joint {jid}: {side} {j[side]!r} is not a rigid group")
        axis = _opt_vec3(j.get("axis"), f"joint {jid} axis")
        secondary = _opt_vec3(j.get("secondary_axis"), f"joint {jid} secondary_axis")
        if axis is not None and secondary is None:
            raise ManifestError(f"joint {jid}: axis set but secondary_axis missing")
        limits = j.get("limits") or {}
        coupling = None
        if j.get("coupling") is not None:
            cdata = j["coupling"]
            if cdata.get("kind") not in COUPLING_KINDS:
                raise ManifestError(f"joint {jid}: unknown coupling kind {cdata.get('kind')!r}")
            mp = cdata.get("mirror_plane")
            if cdata["kind"] == "mirror":
                if mp is None:
                    raise ManifestError(
                        f"joint {jid}: mirror coupling without a mirror_plane")
                if not cdata.get("driver_joint"):
                    raise ManifestError(
                        f"joint {jid}: mirror coupling without a driver_joint")
                if cdata.get("mirror_scope") not in (None, "plane", "rigid"):
                    raise ManifestError(
                        f"joint {jid}: unknown mirror_scope "
                        f"{cdata['mirror_scope']!r}")
            samples = None
            if cdata["kind"] == "table":
                if not cdata.get("driver_joint"):
                    raise ManifestError(f"joint {jid}: table coupling without a driver_joint")
                raw = cdata.get("samples") or []
                if len(raw) < 2:
                    raise ManifestError(f"joint {jid}: table coupling needs two samples or more")
                samples = []
                for s in raw:
                    if not isinstance(s, (list, tuple)) or len(s) != 2:
                        raise ManifestError(f"joint {jid}: table sample {s!r} is not [x, y]")
                    samples.append((float(s[0]), float(s[1])))
                for a, b in zip(samples, samples[1:]):
                    if b[0] <= a[0]:
                        raise ManifestError(
                            f"joint {jid}: table samples must ascend in x, got {a[0]} then {b[0]}")
                if cdata.get("periodic") and not (cdata.get("period") or 0) > 0:
                    raise ManifestError(f"joint {jid}: periodic table without a period")
            cam = {}
            if cdata["kind"] == "cam":
                if not cdata.get("driver_joint"):
                    raise ManifestError(f"joint {jid}: cam coupling without a driver_joint")
                block = cdata.get("cam")
                if not isinstance(block, dict):
                    raise ManifestError(f"joint {jid}: cam coupling without a cam block")
                surf = block.get("surface") or {}
                cpts = [_vec3(p, f"joint {jid} cam point") for p in (surf.get("points") or [])]
                ctris = surf.get("triangles") or []
                if len(cpts) < 3 or not ctris:
                    raise ManifestError(f"joint {jid}: cam surface needs points and triangles")
                for t in ctris:
                    if (not isinstance(t, (list, tuple)) or len(t) != 3
                            or any(not isinstance(i, int) or i < 0 or i >= len(cpts) for i in t)):
                        raise ManifestError(
                            f"joint {jid}: cam triangle {t!r} is not three point indices")
                fol = block.get("follower") or {}
                fkind = fol.get("kind")
                if fkind not in ("vertex", "roller", "flat"):
                    raise ManifestError(f"joint {jid}: unknown cam follower kind {fkind!r}")
                if fol.get("point") is None:
                    raise ManifestError(f"joint {jid}: cam follower without a point")
                if fkind == "roller" and not (fol.get("radius") or 0) > 0:
                    raise ManifestError(f"joint {jid}: roller follower without a radius")
                if fkind == "flat" and fol.get("normal") is None:
                    raise ManifestError(f"joint {jid}: flat follower without a normal")
                cam = dict(
                    cam_axis=_vec3(block.get("axis"), f"joint {jid} cam axis"),
                    cam_origin=_vec3(block.get("origin"), f"joint {jid} cam origin"),
                    cam_surface_points=cpts,
                    cam_surface_triangles=[[int(i) for i in t] for t in ctris],
                    follower_kind=fkind,
                    follower_point=_vec3(fol["point"], f"joint {jid} follower point"),
                    follower_axis=_vec3(fol["axis"], f"joint {jid} follower axis")
                    if fol.get("axis") is not None else None,
                    follower_radius=float(fol["radius"]) if fol.get("radius") is not None else None,
                    follower_normal=_vec3(fol["normal"], f"joint {jid} follower normal")
                    if fol.get("normal") is not None else None,
                )
            coupling = Coupling(
                kind=cdata["kind"],
                driver_joint=cdata.get("driver_joint"),
                ratio=cdata.get("ratio"),
                meters_per_radian=cdata.get("meters_per_radian"),
                lead_m_per_rev=cdata.get("lead_m_per_rev"),
                mirror_plane_point=_vec3(mp["point"], f"joint {jid} mirror point")
                if mp is not None else None,
                mirror_plane_normal=_vec3(mp["normal"], f"joint {jid} mirror normal")
                if mp is not None else None,
                mirror_scope=cdata.get("mirror_scope") or "plane",
                samples=samples,
                periodic=bool(cdata.get("periodic", False)),
                period=float(cdata.get("period") or 0.0),
                **cam,
            )
        path_points = None
        path_closed = False
        if j.get("path") is not None:
            pdata = j["path"]
            raw = pdata.get("points") or []
            if len(raw) < 2:
                raise ManifestError(f"joint {jid}: path needs at least 2 points")
            path_points = [_vec3(p, f"joint {jid} path point") for p in raw]
            path_closed = bool(pdata.get("closed", False))
        if jtype == "path" and path_points is None:
            raise ManifestError(f"joint {jid}: type is path but no path.points given")
        surface_points = None
        surface_triangles = None
        if j.get("surface") is not None:
            sdata = j["surface"]
            raw = sdata.get("points") or []
            if len(raw) < 3:
                raise ManifestError(f"joint {jid}: surface needs at least 3 points")
            surface_points = [_vec3(p, f"joint {jid} surface point") for p in raw]
            surface_triangles = []
            for t in sdata.get("triangles") or []:
                if len(t) != 3 or any(not isinstance(i, int) or i < 0
                                      or i >= len(surface_points) for i in t):
                    raise ManifestError(
                        f"joint {jid}: surface triangle {t} is not three valid "
                        f"point indices")
                surface_triangles.append([int(i) for i in t])
            if not surface_triangles:
                raise ManifestError(f"joint {jid}: surface has no triangles")
        if jtype == "surface" and surface_triangles is None:
            raise ManifestError(
                f"joint {jid}: type is surface but no surface.triangles given")
        joints.append(Joint(
            id=jid,
            type=jtype,
            parent_group=j["parent_group"],
            child_group=j["child_group"],
            origin=_opt_vec3(j.get("origin"), f"joint {jid} origin"),
            axis=axis,
            secondary_axis=secondary,
            rotation_limit=_limit(limits.get("rotation"), f"joint {jid} rotation limit"),
            translation_limit=_limit(limits.get("translation"), f"joint {jid} translation limit"),
            coupling=coupling,
            source_mates=list(j.get("source_mates", [])),
            confidence=j.get("confidence", "high"),
            notes=j.get("notes"),
            path_points=path_points,
            path_closed=path_closed,
            surface_points=surface_points,
            surface_triangles=surface_triangles,
        ))
    joint_ids = {j.id for j in joints}
    if len(joint_ids) != len(joints):
        raise ManifestError("duplicate joint ids")
    for j in joints:
        if j.coupling and j.coupling.driver_joint and j.coupling.driver_joint not in joint_ids:
            raise ManifestError(f"joint {j.id}: coupling driver {j.coupling.driver_joint!r} is not a joint")

    def parse_loop(lp) -> Loop:
        members = list(lp["member_joints"])
        unknown = [m for m in members if m not in joint_ids]
        if unknown:
            raise ManifestError(f"loop {lp['id']} references unknown joints {unknown}")
        if lp["closure_joint"] not in members:
            raise ManifestError(f"loop {lp['id']}: closure joint {lp['closure_joint']!r} is not a member")
        candidates = []
        for c in lp.get("driver_candidates") or []:
            if c.get("joint") not in members or c.get("closure_joint") not in members:
                raise ManifestError(
                    f"loop {lp['id']}: driver candidate {c!r} names a joint "
                    f"that is not a member")
            candidates.append(DriverCandidate(
                joint=c["joint"], closure_joint=c["closure_joint"],
                closure_kind=_closure_kind({"id": lp["id"],
                                            "closure_kind": c.get("closure_kind", "ik")})))
        return Loop(
            id=lp["id"],
            member_joints=members,
            closure_joint=lp["closure_joint"],
            suggested_driver_joint=lp.get("suggested_driver_joint"),
            closure_kind=_closure_kind(lp),
            planar=bool(lp.get("planar", False)),
            plane_normal=_opt_vec3(lp.get("plane_normal"), f"loop {lp['id']} plane normal"),
            mobility=max(1, int(lp.get("mobility", 1) or 1)),
            driver_candidates=candidates,
        )

    loops = [parse_loop(lp) for lp in data.get("loops", [])]
    loop_ids = {lp.id for lp in loops}

    mechanisms = []
    for md in data.get("mechanisms") or []:
        mid = md.get("id", "mech%03d" % (len(mechanisms) + 1))
        ids = list(md.get("loops") or [])
        missing = [x for x in ids if x not in loop_ids]
        if missing:
            raise ManifestError(f"mechanism {mid} names unknown loops {missing}")
        options = []
        for od in md.get("inputs") or []:
            joint = od.get("joint")
            if joint not in joint_ids:
                raise ManifestError(f"mechanism {mid}: input {joint!r} is not a joint")
            opt_loops = [parse_loop(lp) for lp in od.get("loops") or []]
            if [lp.id for lp in opt_loops] != ids:
                raise ManifestError(
                    f"mechanism {mid}: input {joint} lists loops "
                    f"{[lp.id for lp in opt_loops]}, the mechanism has {ids}")
            flipped = list(od.get("flipped_joints") or [])
            unknown = [x for x in flipped if x not in joint_ids]
            if unknown:
                raise ManifestError(f"mechanism {mid}: input {joint} flips unknown joints {unknown}")
            joint_limits = {}
            for ld in od.get("joint_limits") or []:
                jid_l = ld.get("joint")
                if jid_l not in joint_ids:
                    raise ManifestError(
                        f"mechanism {mid}: input {joint} limits unknown joint {jid_l!r}")
                lim = ld.get("limits") or {}
                joint_limits[jid_l] = (
                    _limit(lim.get("rotation"), f"mechanism {mid} {jid_l} rotation limit"),
                    _limit(lim.get("translation"), f"mechanism {mid} {jid_l} translation limit"))
            options.append(InputOption(joint=joint, loops=opt_loops, flipped_joints=flipped,
                                       joint_limits=joint_limits))
        mechanisms.append(Mechanism(id=mid, loop_ids=ids, inputs=options))

    warnings = [Warning(code=w["code"], message=w["message"],
                        components=list(w.get("components", [])),
                        joints=list(w.get("joints", [])))
                for w in data.get("warnings", [])]

    return Manifest(
        manifest_version=version,
        generator=dict(data.get("generator", {})),
        step_file=step["file"],
        step_sha1=step.get("sha1"),
        components=components,
        rigid_groups=groups,
        joints=joints,
        loops=loops,
        warnings=warnings,
        source_path=source_path,
        mechanisms=mechanisms,
    )


def step_path(manifest: Manifest) -> Optional[str]:
    """The STEP file the manifest names, next to the manifest itself. None
    when either one is not known."""
    if not manifest.step_file or not manifest.source_path:
        return None
    return os.path.join(os.path.dirname(os.path.abspath(manifest.source_path)),
                        manifest.step_file)


def stale_step(manifest: Manifest, path: Optional[str] = None) -> Optional[str]:
    """A warning when the STEP file is not the one the manifest was written
    for, else None.

    The manifest carries the SHA-1 of its STEP file for this, and nothing
    read it: a STEP file written again after the manifest was matched
    against it without a word. A manifest with no hash (a direct send
    writes none) and a STEP file that is not there are not checked. The
    import says so when the file is missing."""
    want = (manifest.step_sha1 or "").strip().lower()
    path = path or step_path(manifest)
    if not want or not path or not os.path.isfile(path):
        return None
    digest = hashlib.sha1()
    try:
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                digest.update(block)
    except OSError:
        return None
    if digest.hexdigest() == want:
        return None
    return ("The SHA-1 of %s is not the SHA-1 in the manifest. The file "
            "changed after the export, and parts can match incorrectly. "
            "Export the assembly again" % os.path.basename(path))


def load(path: str) -> Manifest:
    """Reads and validates a .rig.json file. UTF-8, as the writer emits it."""
    with open(path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as exc:
            raise ManifestError(f"{path}: not valid JSON ({exc})")
    return parse(data, source_path=path)
