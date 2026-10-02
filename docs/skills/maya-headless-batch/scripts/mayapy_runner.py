#!/usr/bin/env python
"""Headless Maya batch scaffold — run with mayapy, never with system python.

    /Applications/Autodesk/maya<VER>/Maya.app/Contents/bin/mayapy mayapy_runner.py \
        --scene shotA.ma --scene shotB.ma --out /tmp/out

Replace ``do_work`` with the real operation. Everything else exists to stop the
three failures that make headless runs waste an afternoon:

1. Plugin commands (AbcExport, mayaUSDExport, FBXExport, arnoldExportAss) do not
   exist in a bare standalone session. They are loaded below, each guarded, so a
   plugin missing on this build is reported and skipped rather than aborting.
2. One bad scene killing the whole batch. Each file runs in its own try and the
   run continues; failures are collected and reported at the end.
3. A non-zero exit that says nothing. The summary distinguishes "nothing ran"
   from "ran and some files failed".
4. An exit code that lies. Once ``maya.standalone.initialize()`` has run,
   mayapy 2027 discards ``sys.exit(N)`` and exits 0 (measured Chat 109), so a
   failed batch looked successful. The code leaves through ``os._exit`` instead.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback

# Loaded per-plugin, guarded. mtoa is licence-gated and slow headlessly; a
# failure there is normal on an unlicensed box and must not be fatal.
PIPELINE_PLUGINS = (
    "mayaUsdPlugin",   # mayaUSDImport / mayaUSDExport
    "AbcImport",
    "AbcExport",
    "fbxmaya",         # FBXImport / FBXExport
    "objExport",
    "mtoa",            # arnoldExportAss / arnoldRender
)


def load_plugins(names=PIPELINE_PLUGINS):
    """Best-effort plugin load. Returns (loaded, failed)."""
    import maya.cmds as cmds

    loaded, failed = [], []
    for plugin in names:
        try:
            if not cmds.pluginInfo(plugin, query=True, loaded=True):
                cmds.loadPlugin(plugin, quiet=True)
            loaded.append(plugin)
        except Exception:
            failed.append(plugin)
    return loaded, failed


def do_work(scene_path, args):
    """REPLACE ME. Runs with the scene already open. Return a summary string.

    Reminders that bite headless:
      - No UI: no playblast, no viewport capture, no Qt.
      - Render to a file with kick on an exported .ass, never cmds.render()
        (which writes scene-linear and ignores colour management).
      - Verify a cache export by re-importing and comparing the element count
        AND one varying channel. Do not clear the scene in between.
    """
    import maya.cmds as cmds

    meshes = cmds.ls(type="mesh", noIntermediate=True) or []
    return "{} mesh(es)".format(len(meshes))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", action="append", default=[],
                        help="Scene to process (repeatable). Omit to run once on an empty scene.")
    parser.add_argument("--out", default=None, help="Output directory, passed through to do_work.")
    parser.add_argument("--save", action="store_true",
                        help="Save each scene after do_work. OFF by default — opt in deliberately.")
    args = parser.parse_args(argv)

    try:
        import maya.standalone
    except ImportError:
        sys.stderr.write("Not running under mayapy: maya.standalone is unavailable.\n")
        return 2

    maya.standalone.initialize(name="python")
    import maya.cmds as cmds

    loaded, failed = load_plugins()
    sys.stderr.write("plugins loaded: {}\n".format(", ".join(loaded) or "none"))
    if failed:
        sys.stderr.write("plugins unavailable (continuing): {}\n".format(", ".join(failed)))

    scenes = args.scene or [None]
    ok, errors = [], []

    try:
        for scene in scenes:
            try:
                if scene:
                    cmds.file(scene, open=True, force=True)
                else:
                    cmds.file(new=True, force=True)

                summary = do_work(scene, args)

                if args.save and scene:
                    cmds.file(save=True, force=True)

                ok.append((scene, summary))
                sys.stderr.write("OK   {}: {}\n".format(scene or "<empty scene>", summary))
            except Exception as exc:
                errors.append((scene, exc))
                sys.stderr.write("FAIL {}: {}\n".format(scene or "<empty scene>", exc))
                traceback.print_exc()
    finally:
        # Flushes pending writes. Skipping it can truncate files Maya still holds.
        try:
            maya.standalone.uninitialize()
        except Exception:
            pass

    sys.stderr.write("\n{} ok, {} failed, {} total\n".format(len(ok), len(errors), len(scenes)))
    if not ok and errors:
        return 1
    return 1 if errors else 0


if __name__ == "__main__":
    code = main()
    # sys.exit(code) is swallowed after maya.standalone.initialize() — mayapy
    # then exits 0 whatever the code. os._exit keeps it; flush first, because
    # os._exit skips interpreter teardown.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
