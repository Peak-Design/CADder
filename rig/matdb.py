# SPDX-License-Identifier: GPL-3.0-or-later
"""The material database, applied to a direct send.

CADder keeps databases of authored materials: a .blend holding the
replacement materials and a map from the name a CAD import gave a material
to the name of the material that should stand in its place. A STEP import
applies the active database on the way in. A direct send now does the
same, because it names its materials after the SolidWorks appearance
("polished gold") and writes the STEP_materials property the database
matches on.

Everything the database needs already lives in main.py; this is the seam
between it and the direct link, kept apart so a failure in one is not a
failure of the import.
"""


def active_path():
    """The file of the database the user selected, or "" for none."""
    try:
        from .. import main
        return main._get_active_matdb_path()
    except Exception as exc:                     # a preference read can fail
        print("[CADLink matdb] no active database: %s" % exc)
        return ""


def apply(objects, where="import"):
    """Replaces the materials of freshly imported objects with the active
    database's. Returns the number of slots replaced (0 when no database is
    selected, which is the normal case)."""
    path = active_path()
    if not path or not objects:
        return 0
    try:
        from .. import main
        mappings = main._ensure_matdb_materials(path)
        if not mappings:
            return 0
        replaced = main._apply_matdb_to_objects(objects, mappings)
        if replaced:
            main._cleanup_unused_step_materials(known_names=set(mappings))
            print("[CADLink matdb] %s: %d material slot(s) from %s"
                  % (where, replaced, path))
        return replaced
    except Exception as exc:
        print("[CADLink matdb] %s: the database was not applied (%s)" % (where, exc))
        return 0
