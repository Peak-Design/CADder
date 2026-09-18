# SPDX-License-Identifier: GPL-3.0-or-later
"""Sizes the empties of an import to what hangs under them.

An empty is drawn at a fixed size in its own units. A CAD assembly can be
a watch or a factory, so no one size fits: at 2 m, the empties of an engine
a third of a metre long cover the whole viewport. Each empty is sized to
the parts under it instead, so an assembly empty is a small cross inside
its assembly, and a subassembly's is smaller again.
"""

import numpy as np

# The arms of an empty reach this share of the diagonal of what hangs under
# it: a tenth, so the cross stays inside the parts it holds.
FRACTION = 0.1


def fit(empties, content):
    """Sizes each tree empty in `empties` to the objects of `content` under
    it. An empty with nothing under it takes the size of the nearest empty
    above it. Either list can hold objects of any kind: only the empties
    that hold a tree are sized, and only the objects with geometry are
    measured. Returns how many empties it sized.

    World matrices must be up to date: call view_layer.update() first when
    the objects were just placed or parented."""
    empties = [o for o in empties if _is_tree_empty(o)]
    if not empties:
        return 0
    wanted = {e.name_full: e for e in empties}
    lo, hi = {}, {}
    boxes = {}

    # Each object with geometry adds its box to every empty above it. One
    # walk up from each object, so a deep tree costs its depth and not a
    # search of every subtree.
    for obj in content:
        if _is_tree_empty(obj):
            continue
        corners = _world_corners(obj, boxes)
        if corners is None:
            continue
        low, high = corners.min(axis=0), corners.max(axis=0)
        parent = obj.parent
        while parent is not None:
            key = parent.name_full
            if key in wanted:
                if key in lo:
                    lo[key] = np.minimum(lo[key], low)
                    hi[key] = np.maximum(hi[key], high)
                else:
                    lo[key], hi[key] = low, high
            parent = parent.parent

    sized = {}
    for key, e in wanted.items():
        if key not in lo:
            continue
        diagonal = float(np.linalg.norm(hi[key] - lo[key]))
        if diagonal > 0.0:
            sized[key] = diagonal * FRACTION
    for key, e in wanted.items():
        world = sized.get(key)
        parent = e.parent
        while world is None and parent is not None:
            world = sized.get(parent.name_full)
            parent = parent.parent
        if world is None:
            continue
        scale = max(abs(s) for s in e.matrix_world.to_scale())
        e.empty_display_size = world / scale if scale > 0.0 else world
    return len(sized)


def _is_tree_empty(obj):
    return obj.type == "EMPTY" and obj.instance_collection is None


def _world_corners(obj, boxes):
    """The corners of an object's box in world space, or None when it has
    no box. A collection instance takes the box of the collection it draws,
    measured once for each collection and kept in `boxes`."""
    m = np.array(obj.matrix_world, dtype=np.float64)
    if obj.type == "EMPTY":
        collection = obj.instance_collection
        if collection is None:
            return None
        key = collection.name_full
        if key not in boxes:
            found = [_world_corners(o, boxes) for o in collection.all_objects
                     if o.type != "EMPTY"]
            found = [b for b in found if b is not None]
            boxes[key] = (np.vstack(found) - np.array(collection.instance_offset)
                          if found else None)
        local = boxes[key]
        if local is None:
            return None
        return local @ m[:3, :3].T + m[:3, 3]
    try:
        box = np.array([tuple(c) for c in obj.bound_box], dtype=np.float64)
    except (AttributeError, TypeError):
        return None
    if not box.any():
        return None
    return box @ m[:3, :3].T + m[:3, 3]
