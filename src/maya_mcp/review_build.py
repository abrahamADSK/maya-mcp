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
   throwaway turntable nodes (so the orbit keyframes — which live only on that
   pivot — exist solely during the playblast and are then removed), and restores
   the time unit, the playback range AND the current frame. Restoring the clock
   is load-bearing: leaving the scene on the end frame re-evaluated a model's own
   keyframes and displaced an unkeyed manual pose (Chat 79). The deliverable
   scene is left exactly as found.
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


def _apply_review_color_management(cmds, view_transform):
    """Best-effort: enable colour management and pin the review view transform.

    The captured VP2.0 playblast bakes whatever view transform colour management
    is displaying; pinning it makes the .mov deterministic instead of riding
    session state, so a version preview is never dark/mismatched (Chat 79).
    Returns the previous view transform name to restore later (or ``None`` when
    nothing was changed). No-op — degrades to the pre-Chat-79 behaviour, never
    raises — when colour management or the view name is unavailable in the active
    OCIO config. Flags mirror ``maya_mcp.color_policy`` (this module ships
    standalone over the Command Port and cannot import it inside Maya; the two
    are kept in sync by ``tests/test_color_policy.py``).
    """
    try:
        cmds.colorManagementPrefs(edit=True, cmEnabled=True)
        views = cmds.colorManagementPrefs(query=True, viewTransformNames=True) or []
        if view_transform in views:
            prev = cmds.colorManagementPrefs(query=True, viewTransformName=True)
            if prev != view_transform:
                cmds.colorManagementPrefs(edit=True, viewTransformName=view_transform)
            return prev
    except Exception:
        pass
    return None


def review_turntable(
    out_path,
    start=1,
    end=100,
    fps=25,
    width=1920,
    height=1080,
    objects=None,
    focal=50.0,
    view_transform="Un-tone-mapped (sRGB)",
):
    """Deterministic turntable playblast → ``out_path`` (.mov). See module docstring.

    ``out_path`` is REQUIRED (resolve it via fpt ``tk_resolve_path`` upstream).
    ``objects`` frames a specific selection; otherwise the current selection, else
    every top-level assembly (excluding the default cameras).
    ``view_transform`` is the colour-management view pinned for the capture so the
    .mov matches the viewport (default = the Maya-default-config review view;
    the server passes ``config.json -> review_view_transform``). Chat 79.
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
    # Snapshot the playback clock so the recipe restores it in `finally`. Without
    # this the scene was left on the END frame, and advancing the time
    # re-evaluated a model's own keyframes — an unkeyed manual pose (feet on the
    # ground) silently reverted to its keyed value and the asset appeared
    # DISPLACED after every turntable (Chat 79). The orbit keys themselves live
    # only on the throw-away pivot, which is deleted below, so the recipe leaves
    # NO keyframes behind; restoring the clock leaves the model exactly as found.
    prev_time = cmds.currentTime(query=True)
    prev_range = (cmds.playbackOptions(query=True, minTime=True),
                  cmds.playbackOptions(query=True, maxTime=True),
                  cmds.playbackOptions(query=True, animationStartTime=True),
                  cmds.playbackOptions(query=True, animationEndTime=True))
    prev_view = None
    piv = None
    panel = prev_sel = win = None
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
        # Orbit a full 360deg, but START/END with the model's BACK to camera so
        # the MIDDLE frame shows the FRONT: Flow Production Tracking thumbnails the
        # CENTRE frame of the review, and a front-facing thumbnail is wanted. With
        # the camera at +Z (front) at rotateY=0, keying the pivot 180->540 puts the
        # back at the ends (first/last frame) and the front at the centre
        # (rotateY == 360 == 0). Chat 77.
        cmds.cutKey(piv + ".rotateY")
        cmds.setKeyframe(piv + ".rotateY", time=start, value=180.0)
        cmds.setKeyframe(piv + ".rotateY", time=end, value=540.0)
        cmds.keyTangent(piv + ".rotateY", inTangentType="linear", outTangentType="linear")

        # Capture from a DEDICATED temporary Viewport-2.0 window — NEVER the
        # user's docked/focused viewport. Why (Chat 77): when an Arnold IPR /
        # render-override is active on the focused panel, the playblast captures
        # the Arnold render (its HUD burned into the buffer) even after the code
        # sets rendererName="vp2Renderer", because IPR overlays the panel; and the
        # docked panel's on-screen pixel size (e.g. 1540x1266) is what gets
        # captured then stretched to widthHeight -> wrong aspect ratio. A brand-new
        # modelPanel in a throw-away 16:9 window has NO IPR attached (guaranteed
        # VP2.0) and a clean 16:9 source (no stretch). It stays visible
        # (offScreen=False) so it keeps the valid render context the offScreen
        # buffer lacked when Maya was occluded (Chat 74). The window is deleted in
        # `finally`; the user's panels / renderer / selection are never modified.
        prev_sel = cmds.ls(selection=True, long=True)
        if cmds.window("reviewTurntableWin", exists=True):
            cmds.deleteUI("reviewTurntableWin")
        win = cmds.window("reviewTurntableWin", title="Review Turntable",
                          widthHeight=(1280, 720))
        cmds.paneLayout()
        panel = cmds.modelPanel(menuBarVisible=False)
        cmds.modelPanel(panel, edit=True, camera=cam_s)
        # VP2's own default camera headlight — preview lighting independent of the
        # scene, consistent as the model orbits; lives only in this throw-away
        # window, never in the published scene (a Model carries no lights).
        cmds.modelEditor(panel, edit=True, rendererName="vp2Renderer",
                         displayAppearance="smoothShaded", displayTextures=True,
                         headsUpDisplay=False, grid=False, displayLights="default")
        cmds.showWindow(win)
        # Isolate the framed model so unrelated scene elements (other assets, a
        # stray light dome) never appear. addSelected, NOT loadSelected: on Maya
        # 2027 loadSelected leaves the isolate set EMPTY -> a grey/empty .mov
        # (proven in-vivo, Chat 74).
        try:
            cmds.isolateSelect(panel, state=True)
            cmds.select(objs, replace=True)
            cmds.isolateSelect(panel, addSelected=True)
            cmds.select(clear=True)  # no selection highlight in the capture
        except Exception:
            pass
        _trace(f"temp-window panel={panel} win={win} "
               f"vis_meshes={len(cmds.ls(type='mesh', visible=True, long=True) or [])}")

        # Pin the review view transform so the VP2.0 capture is colour-correct
        # and deterministic (not dark / riding session state). Restored in
        # `finally`. Chat 79 — see maya_mcp.color_policy.
        prev_view = _apply_review_color_management(cmds, view_transform)
        _trace(f"colour-mgmt view={view_transform!r} prev={prev_view!r}")

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
        # ONSCREEN (visible) playblast — offScreen=False. The temp window is
        # visible so the viewport always has a valid render context and never
        # comes back empty (the offScreen buffer drew nothing when Maya was
        # occluded, Chat 74). The captured panel is the fresh VP2.0 window above,
        # so this never captures an Arnold/IPR panel (Chat 71 hang / Chat 77).
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
        if win and cmds.window(win, exists=True):
            try:
                cmds.deleteUI(win)  # removes the temp panel + its editor too
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
        try:
            cmds.playbackOptions(minTime=prev_range[0], maxTime=prev_range[1],
                                 animationStartTime=prev_range[2],
                                 animationEndTime=prev_range[3])
        except Exception:
            pass
        try:
            # Back to the original frame LAST — this re-evaluates the model's own
            # keyframes to their pre-render state, so an unkeyed manual pose is no
            # longer left reverted/displaced (Chat 79).
            cmds.currentTime(prev_time)
        except Exception:
            pass
        if prev_view:
            try:
                cmds.colorManagementPrefs(edit=True, viewTransformName=prev_view)
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
        "view_transform": view_transform,
        "format": used,
        "asset": asset,
        "task": task,
        "version_code": version_code,  # {Asset}_{Task} — name the Version after the task
        "note": "Name BOTH the .mov and the Version after the task: resolve the .mov "
                "path via tk_resolve_path(movie_asset_publish, name=<task>) → "
                "{Asset}_{Task}_v###.mov (NEVER 'turntable'); set the Version code to "
                "version_code, sg_path_to_movie to mov, sg_upload mov to sg_uploaded_movie.",
    }
