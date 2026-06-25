"""
review_build.py
===============
Deterministic in-Maya **review turntable** recipe — codified so the playblast
step never depends on the LLM improvising (which hung the console: a Viewport-2.0
playblast that actually captured an Arnold/IPR panel saturated Maya's main thread
and timed out). Chat 71.

Runs INSIDE Maya (uses ``maya.cmds``); maya-mcp's server ships this module's
source over the Command Port and calls :func:`review_turntable`. All Maya imports
are inside the functions so the module imports cleanly in the server process.

What :func:`review_turntable` does, deterministically:
1. Sets fps (25 → Maya 'pal') WITHOUT rescaling existing keys, and the frame
   range ``[start, end]``.
2. Frames the model and builds a camera that orbits it 360° over the range.
3. Camera Film Aspect = 1.778, lens squeeze (pixel aspect) = 1.0, Film Fit =
   Overscan → 16:9 square-pixel framing (the anti-anamorphic guarantee comes
   from the camera plus an explicit 16:9 ``widthHeight``, not render globals).
4. Resolves the panel the playblast WILL capture (the focused model panel, else
   the last one), forces **Viewport 2.0** (NEVER Arnold) + the turntable camera
   ON THAT panel, and playblasts **onscreen (visible)** with ``editorPanelName``
   pinned — so the captured panel is exactly the one set to VP2.0 (this is what
   prevents the Chat 71 Arnold-on-main-thread hang; an on-screen viewport also
   always has a valid render context, so it never comes back empty the way the
   offScreen buffer did when Maya was occluded). QuickTime/H.264 with an
   avfoundation→PNG-sequence fallback if the encoder is unavailable.
5. In a ``finally`` block, restores the panel camera/renderer, deletes the
   throwaway turntable nodes, and restores the time unit — the deliverable scene
   is left exactly as found.
6. Returns the .mov path plus, if Maya was launched via the ``tk-maya`` engine,
   the asset/task context + a suggested Version code ``{Asset}_{Task}`` so the
   ShotGrid Version is named after the task it was generated in.

It does NOT touch ShotGrid. The caller resolves ``out_path`` via fpt
``tk_resolve_path(movie_asset_publish)`` and does the Version create + set
``sg_path_to_movie`` + ``sg_upload`` to ``sg_uploaded_movie`` with ``version_code``.

GOTCHA baked in (memory ``feedback_maya_gs_arnold_ipr_hang`` /
``feedback_maya_heavy_ops_crash``): VP2.0 onscreen, pinned to the captured
panel; one attempt — the caller must not retry on error.
"""

from __future__ import annotations

import os as _os
import time as _time

# 25 fps → Maya's 'pal' time unit. Integer broadcast rates only; fractional
# rates (23.976 / 29.97) are not remapped — the scene keeps its current unit.
_FPS_TIME_UNIT = {24: "film", 25: "pal", 30: "ntsc", 48: "show", 60: "ntscf"}

# Best-effort per-phase trace → a tailable log. review_turntable runs on Maya's
# main thread; when the output comes out empty or a step stalls, these lines
# (objs framed, bbox/size, captured panel, visible-mesh count, playblast
# start/done) pinpoint the cause without guessing. Never raises.
# Tail:  tail -f ~/Library/Logs/maya-mcp-turntable.log
_TRACE_PATH = _os.path.expanduser("~/Library/Logs/maya-mcp-turntable.log")


def _trace(msg):
    try:
        with open(_TRACE_PATH, "a", encoding="utf-8") as _fh:
            _fh.write(f"{_time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _engine_context():
    """Best-effort tk-maya context → (asset, task, version_code) or (None, …).

    ``version_code`` = ``{Asset}_{Task}`` so the Version is named after the task
    it was generated in. Silent/None when Maya is not engine'd.
    """
    try:
        import sgtk

        eng = sgtk.platform.current_engine()
        ctx = eng.context if eng else None
        if not ctx:
            return None, None, None
        asset = (ctx.entity or {}).get("name")
        task = (ctx.task or {}).get("name")
        if not task and ctx.step:
            task = (ctx.step or {}).get("name")
        code = f"{asset}_{task}" if (asset and task) else None
        return asset, task, code
    except Exception:
        return None, None, None


def review_turntable(
    out_path,
    start=1,
    end=100,
    fps=25,
    width=1920,
    height=1080,
    objects=None,
    focal=50.0,
):
    """Deterministic turntable playblast → ``out_path`` (.mov). See module docstring.

    ``out_path`` is REQUIRED (resolve it via fpt ``tk_resolve_path`` upstream).
    ``objects`` frames a specific selection; otherwise the current selection, else
    every top-level assembly (excluding the default cameras).
    """
    import os

    import maya.cmds as cmds

    out_path = str(out_path)
    parent = os.path.dirname(out_path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)

    # Keep ONLY renderable geometry — from the explicit ``objects``, else the
    # current selection, else the whole scene. A non-geometry node (an Arnold/USD
    # options node, a light, an empty group) returns the EMPTY world-bbox
    # sentinel [1e20…-1e20]; a camera built from that gets a NEGATIVE ``size``
    # and is placed at ~1e20 → the onscreen playblast CRASHES Maya (Chat 74: a
    # selected ``tmpArnoldMayaUsdOptions`` killed Maya mid-playblast). The Chat 72
    # mesh-preference only ran when there was no selection; now it always does.
    requested = objects or cmds.ls(sl=True, long=True) or []

    def _carries_mesh(node):
        if cmds.ls(node, type="mesh", long=True):
            return True
        return bool(cmds.listRelatives(node, allDescendents=True, type="mesh",
                                       fullPath=True))

    objs = [o for o in requested if _carries_mesh(o)]
    if not objs:
        # fall back to every renderable mesh in the scene
        mesh_parents = set()
        for shape in (cmds.ls(type="mesh", long=True, noIntermediate=True) or []):
            parent = cmds.listRelatives(shape, parent=True, fullPath=True)
            if parent:
                mesh_parents.add(parent[0])
        objs = sorted(mesh_parents)
    if not objs:
        return {"error": "nothing renderable to frame (no mesh geometry in the "
                         "selection or the scene)"}
    _trace(f"START out={out_path} frames=[{start},{end}] {width}x{height} fps={fps} "
           f"objs={len(objs)} sample={objs[:6]}")
    bb = cmds.exactWorldBoundingBox(objs)
    # Degenerate/empty-bbox guard (crash-proofing, Chat 74). The empty sentinel
    # has max < min; a camera built from it sits at ~1e20 and crashes the
    # onscreen playblast. Refuse cleanly instead of killing Maya.
    if (bb[3] < bb[0] or bb[4] < bb[1] or bb[5] < bb[2]
            or any(abs(v) > 1e17 for v in bb)):
        return {"error": f"degenerate bounding box {bb}; refusing to build the "
                         "turntable camera (it would crash the playblast)"}
    cx, cy, cz = (bb[0] + bb[3]) / 2.0, (bb[1] + bb[4]) / 2.0, (bb[2] + bb[5]) / 2.0
    size = max(bb[3] - bb[0], bb[4] - bb[1], bb[5] - bb[2]) or 1.0
    _trace(f"bbox={[round(v, 2) for v in bb]} center=({cx:.2f},{cy:.2f},{cz:.2f}) size={size:.3f}")

    prev_unit = cmds.currentUnit(query=True, time=True)
    piv = None
    panel = prev_cam = prev_rnm = prev_lights = prev_sel = None
    iso_on = False
    used = {}
    mov = None
    err = None
    try:
        unit = _FPS_TIME_UNIT.get(int(fps))
        if unit and unit != prev_unit:
            cmds.currentUnit(time=unit, updateAnimation=False)  # do NOT rescale keys
        cmds.playbackOptions(minTime=start, maxTime=end,
                             animationStartTime=start, animationEndTime=end)

        # turntable camera orbiting the bbox centre
        cam_t, cam_s = cmds.camera(name="reviewTurntableCam")
        piv = cmds.group(empty=True, name="reviewTurntablePivot")
        cmds.xform(piv, worldSpace=True, translation=(cx, cy, cz))
        cmds.parent(cam_t, piv)
        # Distance size*3.2 (not 2.4) leaves head + feet margin: the model is
        # ~as tall as `size`, and at 2.4 it filled the frame edge-to-edge so the
        # cap/feet clipped — worst at the front/back orbit angles where the arms
        # spread widest (Chat 74, fixed by inspecting actual frames). A gentle
        # -3° tilt (not -6°) keeps the aim near model centre so the head is not
        # cropped at the top.
        cmds.setAttr(cam_t + ".translate", 0.0, size * 0.12, size * 3.2, type="double3")
        cmds.setAttr(cam_t + ".rotateX", -3.0)
        cmds.setAttr(cam_s + ".focalLength", float(focal))
        hfa = cmds.getAttr(cam_s + ".horizontalFilmAperture")
        cmds.setAttr(cam_s + ".verticalFilmAperture", hfa / 1.778)  # 16:9
        cmds.setAttr(cam_s + ".lensSqueezeRatio", 1.0)              # square pixels
        cmds.setAttr(cam_s + ".filmFit", 3)                         # 3 = Overscan
        cmds.cutKey(piv + ".rotateY")
        cmds.setKeyframe(piv + ".rotateY", time=start, value=0.0)
        cmds.setKeyframe(piv + ".rotateY", time=end, value=360.0)
        cmds.keyTangent(piv + ".rotateY", inTangentType="linear", outTangentType="linear")

        # Pin the playblast to the panel we configure: playblast captures the
        # FOCUSED panel, so set VP2.0 + the turntable cam on THAT one and pass
        # editorPanelName. Without this it grabs the focused panel's renderer —
        # if that's Arnold/IPR it hangs Maya's main thread (the bug to prevent).
        mps = cmds.getPanel(type="modelPanel") or []
        panel = cmds.getPanel(withFocus=True)
        if panel not in mps:
            # Pin a VISIBLE viewport, not just the last model panel. Triggered
            # from the dockable console, focus is on the console (not a model
            # panel), so this fell back to mps[-1] (modelPanel4) — which may be
            # HIDDEN in the current layout (an inactive quad cell or a background
            # tab). An onscreen playblast of a panel that is not actually drawn
            # captures only the grey clear-colour → an EMPTY .mov (Chat 74).
            # Prefer a currently-visible model panel; fall back to the last only
            # if none is visible.
            visible = set(cmds.getPanel(visiblePanels=True) or [])
            vis_model = [p for p in mps if p in visible]
            panel = vis_model[0] if vis_model else (mps[-1] if mps else None)
        if panel:
            prev_cam = cmds.modelEditor(panel, query=True, camera=True)
            prev_rnm = cmds.modelEditor(panel, query=True, rendererName=True)
            cmds.modelPanel(panel, edit=True, camera=cam_s)
            cmds.modelEditor(panel, edit=True, rendererName="vp2Renderer",
                             displayAppearance="smoothShaded", displayTextures=True,
                             headsUpDisplay=False, grid=False)
            # Preview lighting INDEPENDENT of the scene: VP2's own default camera
            # headlight — consistent as the model orbits, built expressly for the
            # review and never added to / kept in the published scene (a Model
            # carries no lights of its own). Restored in `finally`.
            prev_lights = cmds.modelEditor(panel, query=True, displayLights=True)
            try:
                cmds.modelEditor(panel, edit=True, displayLights="default")
            except Exception:
                pass
            # Isolate the framed model so unrelated scene elements (other assets,
            # rigs, a stray light dome) never appear in the review.
            try:
                prev_sel = cmds.ls(selection=True, long=True)
                cmds.select(objs, replace=True)
                cmds.isolateSelect(panel, state=True)
                # addSelected, NOT loadSelected: on Maya 2027 `loadSelected`
                # leaves the isolate view set EMPTY, so the panel isolates
                # *nothing* → a fully grey/empty .mov. Proven in-vivo (Chat 74):
                # querying `viewObjects` after loadSelected returned "" with no
                # members; after addSelected the set holds the model and the
                # capture shows it.
                cmds.isolateSelect(panel, addSelected=True)
                cmds.select(clear=True)  # no selection highlight in the capture
                iso_on = True
            except Exception:
                iso_on = False
        _trace(f"panel={panel} mps={mps} prev_cam={prev_cam} prev_rnm={prev_rnm} "
               f"vis_meshes={len(cmds.ls(type='mesh', visible=True, long=True) or [])}")

        # choose an available movie encoder, else fall back (never raise out)
        fmts = cmds.playblast(query=True, format=True) or []
        pb = dict(filename=out_path, widthHeight=(int(width), int(height)),
                  percent=100, quality=95, forceOverwrite=True,
                  viewer=False, offScreen=False, showOrnaments=False, framePadding=4,
                  startTime=start, endTime=end)
        if panel:
            pb["editorPanelName"] = panel
        if "qt" in fmts:
            pb["format"] = "qt"
            if "H.264" in (cmds.playblast(query=True, compression=True) or []):
                pb["compression"] = "H.264"
            used = {"format": "qt", "compression": pb.get("compression", "default")}
        elif "avfoundation" in fmts:
            pb["format"] = "avfoundation"
            used = {"format": "avfoundation"}
        else:
            pb["format"] = "image"
            pb["compression"] = "png"
            pb["filename"] = os.path.splitext(out_path)[0]
            used = {"format": "image", "compression": "png",
                    "note": "qt/avfoundation unavailable — wrote a PNG sequence, not a .mov"}
        # ONSCREEN (visible) playblast — offScreen=False. The user SEES the
        # turntable render, and the on-screen viewport always has a valid render
        # context so it never comes back empty (the offScreen buffer drew nothing
        # when Maya was occluded). The captured panel is still pinned to VP2.0
        # above, so this never captures an Arnold/IPR panel (the Chat 71 hang).
        cmds.refresh(force=True)
        _trace(f"playblast START format={used} offScreen={pb.get('offScreen')}")
        try:
            mov = cmds.playblast(**pb)
            _trace(f"playblast DONE mov={mov}")
        except RuntimeError as exc:
            err = f"playblast failed ({used.get('format')}): {exc}"
            _trace(f"playblast ERROR {exc}")
    finally:
        # leave the deliverable scene exactly as found
        if panel and prev_cam:
            try:
                cmds.modelPanel(panel, edit=True, camera=prev_cam)
            except Exception:
                pass
        if panel and prev_rnm:
            try:
                cmds.modelEditor(panel, edit=True, rendererName=prev_rnm)
            except Exception:
                pass
        if panel and iso_on:
            try:
                cmds.isolateSelect(panel, state=False)
            except Exception:
                pass
        if panel and prev_lights:
            try:
                cmds.modelEditor(panel, edit=True, displayLights=prev_lights)
            except Exception:
                pass
        if prev_sel is not None:
            try:
                cmds.select(prev_sel, replace=True)
            except Exception:
                pass
        if piv and cmds.objExists(piv):
            try:
                cmds.delete(piv)  # deletes the parented turntable camera too
            except Exception:
                pass
        try:
            if cmds.currentUnit(query=True, time=True) != prev_unit:
                cmds.currentUnit(time=prev_unit, updateAnimation=False)
        except Exception:
            pass

    if err:
        return {"error": err, "format_attempted": used}

    asset, task, version_code = _engine_context()
    return {
        "mov": mov or out_path,
        "frames": [start, end],
        "fps": int(fps),
        "resolution": [int(width), int(height)],
        "format": used,
        "asset": asset,
        "task": task,
        "version_code": version_code,  # {Asset}_{Task} — name the Version after the task
        "note": "Name BOTH the .mov and the Version after the task: resolve the .mov "
                "path via tk_resolve_path(movie_asset_publish, name=<task>) → "
                "{Asset}_{Task}_v###.mov (NEVER 'turntable'); set the Version code to "
                "version_code, sg_path_to_movie to mov, sg_upload mov to sg_uploaded_movie.",
    }
