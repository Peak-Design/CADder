# CADder 1.2.3

A fix for a warning in the Blender preferences, and Blender 5.3 support.
Blender 5.1 or later. Install the zip for your platform the same way as the
first time.

CADder 1.2.3 works with CADder Bridge 1.2.0 and 1.2.1. Use
[CADder Bridge 1.2.1](https://github.com/Peak-Design/CADder-SW-Bridge/releases/latest)
for the fix of face appearances in parts with several configurations. See
[Which CADder Bridge works with this CADder](https://github.com/Peak-Design/CADder/blob/main/docs/GUIDE.md#which-cadder-bridge-works-with-this-cadder).

## Bug fixes

- Blender no longer shows "Policy violation with sys.path" for CADder in the
  Extensions preferences
  ([issue #4](https://github.com/Peak-Design/CADder/issues/4)). The warning
  did not change what CADder does.

## Changes

- CADder is tested on Blender 5.3.
- The install steps say that the CADder Bridge `.zip` is not a Blender
  add-on. Install CADder Bridge in SolidWorks with its `setup.exe`.
