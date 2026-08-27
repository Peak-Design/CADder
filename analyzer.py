# Pre-import file analyzer for STEPper NEXT.
#
# Fast chunked text scan of a STEP file (no OpenCASCADE, no Transfer):
# schema, declared units, entity counts and per-preset import-time estimates
# based on this machine's calibration (updated after every real import).

import json
import os
import re

import bpy

_ENTITY_PATTERNS = {
    "products": rb"=\s*PRODUCT\s*\(",
    "assembly_links": rb"=\s*NEXT_ASSEMBLY_USAGE_OCCURRENCE\s*\(",
    "faces": rb"=\s*ADVANCED_FACE\s*\(",
    "shells": rb"=\s*(?:CLOSED_SHELL|OPEN_SHELL)\s*\(",
    # WITH_KNOTS appears exactly once per B-spline surface entity, in both
    # the simple and the complex (rational) instance forms. The bare
    # substring would count complex instances 2-3 times.
    "bspline_surfaces": rb"B_SPLINE_SURFACE_WITH_KNOTS\s*\(",
    "entities": rb"\n#\d+\s*=",
}

# Order matters: imperial files declare a conversion-based INCH/FOOT unit on
# top of SI base units, so those hints must win over the SI patterns.
_UNIT_HINTS = [
    (rb"'INCH'", "inch"),
    (rb"'FOOT'", "foot"),
    (rb"SI_UNIT\s*\(\s*\.MILLI\.\s*,\s*\.METRE\.", "millimeter"),
    (rb"SI_UNIT\s*\(\s*\.CENTI\.\s*,\s*\.METRE\.", "centimeter"),
    (rb"SI_UNIT\s*\(\s*\$\s*,\s*\.METRE\.", "meter"),
]

# Rough tessellation-cost multipliers relative to the BALANCED preset
_PRESET_FACTORS = {"DRAFT": 0.5, "BALANCED": 1.0, "FINE": 2.5, "ULTRA": 6.0}


def scan_step_file(filepath, chunk_size=8 * 1024 * 1024):
    """Chunked regex scan of a STEP file. Returns a result dict."""
    counts = {k: 0 for k in _ENTITY_PATTERNS}
    schema = ""
    units = "unknown"
    size = os.path.getsize(filepath)

    with open(filepath, "rb") as f:
        header = f.read(64 * 1024)
        m = re.search(rb"FILE_SCHEMA\s*\(\s*\(\s*'([^']+)'", header)
        if m:
            schema = m.group(1).decode("ascii", "ignore")

        f.seek(0)
        tail = b""
        found_units = False
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            buf = tail + chunk
            # Matches ending inside the carried-over tail were already
            # counted in the previous iteration. Matches spanning the chunk
            # boundary complete (and count) only now.
            base = len(tail)
            for key, pat in _ENTITY_PATTERNS.items():
                counts[key] += sum(
                    1 for m in re.finditer(pat, buf) if m.end() > base)
            if not found_units:
                for pat, name in _UNIT_HINTS:
                    if re.search(pat, buf):
                        units = name
                        found_units = True
                        break
            tail = buf[-256:]

    return {
        "file": filepath,
        "size_mb": size / 1e6,
        "schema": schema,
        "units": units,
        **counts,
    }


def estimate_times(scan, prefs):
    """Per-preset import time estimates from machine calibration (or None)."""
    try:
        cal = json.loads(prefs.perf_calibration or "{}")
    except Exception:
        cal = {}
    eps = cal.get("parse_eps")
    if not eps or scan["entities"] <= 0:
        return None
    parse_s = scan["entities"] / eps
    # Meshing roughly tracks parse cost on typical files. Scale per preset
    return {p: parse_s * (1.0 + f) for p, f in _PRESET_FACTORS.items()}


class STEPPER_OT_analyze_file(bpy.types.Operator):
    """Analyze a STEP file before you import it. The report shows entity counts,
    units and estimated import times. This does not load any geometry."""
    bl_idname = "stepper.analyze_file"
    bl_label = "Analyze STEP File"

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(
        default="*.step;*.stp;*.st", options={"HIDDEN"})

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        if not os.path.isfile(self.filepath):
            self.report({"ERROR"}, "File not found")
            return {"CANCELLED"}
        try:
            scan = scan_step_file(self.filepath)
        except Exception as e:
            self.report({"ERROR"}, f"Scan failed: {e}")
            return {"CANCELLED"}

        from .main import _get_addon_prefs
        times = estimate_times(scan, _get_addon_prefs())

        lines = [
            f"File: {os.path.basename(scan['file'])} ({scan['size_mb']:.1f} MB)",
            f"Schema: {scan['schema'] or 'unknown'}   Units: {scan['units']}",
            f"Parts: {scan['products']:,}   "
            f"Assembly links: {scan['assembly_links']:,}",
            f"Faces: {scan['faces']:,}   Shells: {scan['shells']:,}   "
            f"B-spline surfaces: {scan['bspline_surfaces']:,}",
            f"Total entities: {scan['entities']:,}",
        ]
        if times:
            est = "   ".join(f"{p.title()}: ~{t:.0f}s" for p, t in times.items())
            lines.append(f"Estimated import: {est}")
        else:
            lines.append("Estimated import: unavailable "
                         "(calibrates after your first import)")

        for ln in lines:
            print("[Analyzer]", ln)
        # Popup report
        def draw(popup, _context):
            for ln in lines:
                popup.layout.label(text=ln)
        context.window_manager.popup_menu(
            draw, title="STEP file analysis", icon="INFO")
        self.report({"INFO"}, "; ".join(lines[2:4]))
        return {"FINISHED"}


classes = (STEPPER_OT_analyze_file,)
