# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless smoke for the defeature switch: which parts travel without their
small features.

The switch lives on the part and on the collection above it, and the rule
that decides between them is the whole feature. Four things have to hold:

- a part inside a collection that is set follows the collection, whatever
  its own switch says;
- the NEAREST collection that is set wins, so a subassembly can differ from
  the assembly it is in;
- the settings ride the request, once per component;
- they survive a rebuild of the whole assembly, which replaces every object.

Run:  blender -b --factory-startup -P rig_defeature_smoke.py
"""

import os
import sys

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from CADder import tools  # noqa: E402
from CADder.rig import defeature, ui  # noqa: E402


def part(name, component, collection):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    obj = bpy.data.objects.new(name, mesh)
    obj["RIG_component_id"] = component
    collection.objects.link(obj)
    return obj


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="CADder")
    scene = bpy.context.scene

    # An assembly with a subassembly in it, two parts in each.
    assembly = bpy.data.collections.new("assembly")
    scene.collection.children.link(assembly)
    sub = bpy.data.collections.new("gearbox")
    assembly.children.link(sub)
    plate = part("plate", "c001", assembly)
    cover = part("cover", "c002", assembly)
    gear = part("gear", "c003", sub)
    everything = [plate, cover, gear]

    # 1. Nothing is set, so nothing is asked for. A scene that says nothing
    #    gets the geometry it has always got.
    assert defeature.orders(everything, scene) == [], "asked without being told"
    assert defeature.source_of(plate, scene) is None

    # 2. One part on its own.
    plate.cad_defeature.enabled = True
    plate.cad_defeature.size = 0.008
    asked = defeature.orders(everything, scene)
    assert len(asked) == 1, asked
    assert asked[0]["component"] == "c001", asked
    assert abs(asked[0]["size_m"] - 0.008) < 1e-6, asked
    assert asked[0]["curved"] is False, asked
    # A part with no SolidWorks reference is named by its component id
    # alone. A part that has one sends it too: an edit to the assembly can
    # give the component id to another part.
    assert "persistent_id" not in asked[0], asked
    plate["SWMESH_persistent_id"] = "pid-plate"
    asked = defeature.orders(everything, scene)
    assert asked[0]["persistent_id"] == "pid-plate", asked

    # 3. The collection covers everything in it, including the parts of the
    #    subassembly below it, and it overrides a part's own switch.
    assembly.cad_defeature.enabled = True
    assembly.cad_defeature.size = 0.02
    assembly.cad_defeature.curved = True
    assert defeature.source_of(plate, scene) is assembly
    assert defeature.source_of(gear, scene) is assembly
    asked = {row["component"]: row for row in defeature.orders(everything, scene)}
    assert set(asked) == {"c001", "c002", "c003"}, asked
    for row in asked.values():
        assert abs(row["size_m"] - 0.02) < 1e-6, row
        assert row["curved"] is True, row

    # 4. The nearest collection wins, so the subassembly can differ from the
    #    assembly it sits in.
    sub.cad_defeature.enabled = True
    sub.cad_defeature.size = 0.004
    sub.cad_defeature.curved = False
    assert defeature.source_of(gear, scene) is sub
    assert defeature.source_of(plate, scene) is assembly
    assert defeature.above(sub, scene) is assembly
    assert defeature.above(assembly, scene) is None
    asked = {row["component"]: row for row in defeature.orders(everything, scene)}
    assert abs(asked["c003"]["size_m"] - 0.004) < 1e-6, asked["c003"]
    assert abs(asked["c001"]["size_m"] - 0.02) < 1e-6, asked["c001"]

    # 5. One component, one entry. A multibody part arrives as one object
    #    per body and they are one piece of geometry to the CAD application.
    second_body = part("gear.body002", "c003", sub)
    asked = defeature.orders(everything + [second_body], scene)
    assert len([r for r in asked if r["component"] == "c003"]) == 1, asked
    bpy.data.objects.remove(second_body)

    # 6. The scope is READ from the selection, not set on a panel.
    #    Parts that are selected are the parts covered.
    def active(collection):
        layer = bpy.context.view_layer.layer_collection
        for child in layer.children:
            if child.collection is collection:
                return child
            for deeper in child.children:
                if deeper.collection is collection:
                    return deeper
        return layer

    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    gear.select_set(True)
    bpy.context.view_layer.objects.active = gear
    covered = {o.name for o in ui._scope_objects(bpy.context)}
    assert covered == {"gear"}, covered

    plate.select_set(True)
    covered = {o.name for o in ui._scope_objects(bpy.context)}
    assert covered == {"gear", "plate"}, covered

    #    With nothing selected it is the collection that is active in the
    #    outliner, and every collection below it. That is how a whole
    #    subassembly is covered without picking its parts out, and how the
    #    root covers the whole assembly.
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    bpy.context.view_layer.active_layer_collection = active(sub)
    covered = {o.name for o in ui._scope_objects(bpy.context)}
    assert covered == {"gear"}, covered
    bpy.context.view_layer.active_layer_collection = active(assembly)
    covered = {o.name for o in ui._scope_objects(bpy.context)}
    assert covered == {"plate", "cover", "gear"}, covered
    bpy.context.view_layer.active_layer_collection = \
        bpy.context.view_layer.layer_collection

    # 6b. Two placements of one part are ONE mesh, and what is asked for
    #     has to be asked for together or the link is gone.
    twin = bpy.data.objects.new("plate-2", plate.data)
    assembly.objects.link(twin)
    twin["RIG_component_id"] = "c004"
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    plate.select_set(True)
    bpy.context.view_layer.objects.active = plate
    covered, holder = tools.scope_of(
        bpy.context, lambda o: tools.from_step(o) or tools.from_cad_link(o))
    assert {o.name for o in covered} == {"plate"}, covered
    assert holder is None
    together = tools.linked_parts(bpy.context, covered)
    assert {o.name for o in together} == {"plate", "plate-2"}, together

    # 6c. The button turns the switch on: a collection where the scope came
    #     from one, and the parts themselves where it did not. A part a
    #     collection already covers is left alone, so nothing is left behind
    #     when the collection goes off.
    assembly.cad_defeature.enabled = False
    sub.cad_defeature.enabled = False
    plate.cad_defeature.enabled = False
    groups, parts = defeature.turn_on(None, together, scene)
    assert (groups, parts) == (0, 2), (groups, parts)
    assert plate.cad_defeature.enabled and twin.cad_defeature.enabled

    plate.cad_defeature.enabled = False
    twin.cad_defeature.enabled = False
    groups, parts = defeature.turn_on(assembly, together, scene)
    assert (groups, parts) == (1, 0), (groups, parts)
    assert assembly.cad_defeature.enabled
    assert not plate.cad_defeature.enabled, \
        "a part the collection already covers got a switch of its own"
    bpy.data.objects.remove(twin)
    assembly.cad_defeature.enabled = True
    assembly.cad_defeature.size = 0.02
    assembly.cad_defeature.curved = True
    sub.cad_defeature.enabled = True
    plate.cad_defeature.enabled = True
    plate.cad_defeature.size = 0.008

    # 6d. The panel points where the button does. With nothing selected
    #     the button covers the active collection, so that is what the
    #     panel has to be about: otherwise the switch the button just set
    #     cannot be cleared, because the panel is still showing the part
    #     that was active before.
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    bpy.context.view_layer.objects.active = plate
    view = bpy.context.view_layer
    view.active_layer_collection = view.layer_collection.children[
        assembly.name]
    assert defeature.target(bpy.context) is assembly, \
        "with nothing selected the panel is about the collection"
    plate.select_set(True)
    assert defeature.target(bpy.context) is plate, \
        "with a part selected the panel is about the part"

    # 7. A rebuild of the whole assembly replaces every object and every
    #    collection. The settings are written down first and put back on
    #    what arrives, matched by component and by collection name.
    held = defeature.snapshot(scene)
    assert set(held["components"]) == {"c001"}, held["components"]
    assert set(held["collections"]) == {"assembly", "gearbox"}, held["collections"]

    for obj in list(everything):
        bpy.data.objects.remove(obj)
    bpy.data.collections.remove(sub)
    bpy.data.collections.remove(assembly)
    assembly = bpy.data.collections.new("assembly")
    scene.collection.children.link(assembly)
    sub = bpy.data.collections.new("gearbox")
    assembly.children.link(sub)
    plate = part("plate", "c001", assembly)
    gear = part("gear", "c003", sub)
    # A part that is no longer in the assembly is simply not there to set.
    missing = defeature.restore(held, scene)
    assert missing == (1, 2), missing
    assert plate.cad_defeature.enabled
    assert abs(plate.cad_defeature.size - 0.008) < 1e-6
    assert assembly.cad_defeature.enabled
    assert abs(assembly.cad_defeature.size - 0.02) < 1e-6
    assert assembly.cad_defeature.curved
    assert abs(sub.cad_defeature.size - 0.004) < 1e-6
    assert defeature.source_of(gear, scene) is sub

    print("rig_defeature_smoke: OK")


main()
