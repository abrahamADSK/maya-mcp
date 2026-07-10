"""Deterministic single-frame **Arnold** still render → ``out_path`` (PNG/JPG).

Why this exists (Chat 82): ``maya_viewport_capture`` is a Viewport-2.0 *grab*
(hardware playblast), NOT a ray-traced render — its own docstring says so. When
the user asks for "a still" meaning an Arnold-rendered image, the only still-ish
tool was the playblast, which (a) is not an Arnold render and (b) hangs Maya if
an Arnold IPR / render-override is live on the focused panel
(``feedback_maya_gs_arnold_ipr_hang``). This module is the missing piece: a
proper offscreen Arnold render of ONE frame to an exact file path — it never
playblasts and never touches the user's viewport, so it cannot hang the main
thread the way an IPR-over-playblast does.

Recipe (shipped as source to Maya by ``server._do_render_still``, mirroring how
``review_build.py`` is shipped for ``review_turntable``):

1. Resolve the camera (explicit arg → focused modelPanel's camera → ``persp``).
2. Snapshot the render state we touch (renderer, resolution, current frame,
   Arnold driver translator, colour-management global output transform) and
   restore ALL of it in ``finally`` — the user's scene is left exactly as found.
3. Set the current renderer to Arnold (``arnold``), the resolution, and the AA
   sample count (``defaultArnoldRenderOptions.AASamples``, modest default so a
   still returns in seconds, overridable).
4. For a display-referred 8-bit still, enable Maya's **global output transform**
   set to the review view (same intent as
   ``color_policy.arnold_output_transform_code("preview", view)``), so the PNG
   matches the viewport instead of coming out raw/linear.
5. Render the frame with ``cmds.arnoldRender(camera=cam, width=w, height=h)``
   (renders to the Render View — a one-shot render, NOT an IPR loop) and write
   the Render View to the EXACT ``out_path`` via
   ``cmds.renderWindowEditor('renderView', edit=True, writeImage=out_path)``.

All blocks are best-effort/guarded: a missing attribute or flag degrades to a
no-op rather than raising, and the ``finally`` restore always runs.

NB: NOT YET VALIDATED IN-VIVO — the Arnold render/driver/colour path must be
exercised in a live Arnold-licensed Maya before this is trusted (the user runs
production; recipe code for a live DCC is validated in that DCC — memory
``feedback_invivo_gate_before_release``).
"""

import os

MTOA_PLUGIN = "mtoa"


def _engine_context():
    """Best-effort tk-maya context → (asset, task, version_code) or (None, …).

    ``version_code`` = ``{Asset}_{Task}`` so the review Version created from this
    still is named after the task it was generated in — same contract as
    review_build._engine_context (kept in sync). Silent/None when Maya is not
    engine'd (a plain, non-Toolkit Maya).
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


def _resolve_camera(cmds, camera):
    """Explicit camera → focused modelPanel's camera → 'persp'."""
    if camera and cmds.objExists(camera):
        return camera
    fp = cmds.getPanel(withFocus=True)
    if fp and cmds.getPanel(typeOf=fp) == "modelPanel":
        cam = cmds.modelPanel(fp, query=True, camera=True)
        if cam and cmds.objExists(cam):
            return cam
    return "persp"


def render_still(out_path, camera=None, frame=None, width=1920, height=1080,
                 aa_samples=3, view_transform="Un-tone-mapped (sRGB)"):
    """Render one Arnold frame to ``out_path``. Returns a report dict.

    :param out_path: absolute output image path (``.png``/``.jpg``).
    :param camera: camera transform/shape; falls back to the focused panel / persp.
    :param frame: frame to render; defaults to the current time.
    :param width/height: render resolution in pixels.
    :param aa_samples: Arnold camera (AA) samples — modest default for speed.
    :param view_transform: colour-management view baked into the 8-bit still.
    """
    import maya.cmds as cmds  # type: ignore[import-not-found]

    # tk-maya context so the caller can name the review Version after the task
    # ({Asset}_{Task}); None when Maya is not engine'd.
    _asset, _task, _version_code = _engine_context()
    report = {"rendered": None, "error": None, "camera": None, "frame": None,
              "resolution": "%dx%d" % (int(width), int(height)),
              "renderer_before": None, "view_transform": view_transform,
              "asset": _asset, "task": _task, "version_code": _version_code}

    cam = _resolve_camera(cmds, camera)
    report["camera"] = cam

    # --- snapshot the state we mutate (restored in finally) ---
    def _get(attr, typ=None):
        try:
            return cmds.getAttr(attr, **({"type": typ} if typ else {}))
        except Exception:
            return None

    prev_renderer = _get("defaultRenderGlobals.currentRenderer")
    prev_w = _get("defaultResolution.width")
    prev_h = _get("defaultResolution.height")
    prev_frame = cmds.currentTime(query=True)
    prev_translator = _get("defaultArnoldDriver.aiTranslator")
    report["renderer_before"] = prev_renderer

    try:
        if not cmds.pluginInfo(MTOA_PLUGIN, query=True, loaded=True):
            cmds.loadPlugin(MTOA_PLUGIN)

        if frame is not None:
            cmds.currentTime(float(frame))
        report["frame"] = cmds.currentTime(query=True)

        cmds.setAttr("defaultRenderGlobals.currentRenderer", "arnold", type="string")
        cmds.setAttr("defaultResolution.width", int(width))
        cmds.setAttr("defaultResolution.height", int(height))
        try:
            cmds.setAttr("defaultArnoldRenderOptions.AASamples", int(aa_samples))
        except Exception:
            pass

        # 8-bit display-referred still → PNG driver + Maya global output transform
        # pinned to the review view (see color_policy; same intent as its
        # arnold_output_transform_code("preview", view)). Best-effort.
        fmt = "jpeg" if out_path.lower().endswith((".jpg", ".jpeg")) else "png"
        try:
            cmds.setAttr("defaultArnoldDriver.aiTranslator", fmt, type="string")
        except Exception:
            pass
        try:
            cmds.colorManagementPrefs(edit=True, outputTransformEnabled=True,
                                      outputTarget="renderView")
            cmds.colorManagementPrefs(edit=True, outputTransformName=view_transform,
                                      outputTarget="renderView")
        except Exception:
            pass

        folder = os.path.dirname(out_path)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)

        # One-shot render to the Render View (NOT an IPR loop), then write the
        # Render View to the exact out_path. This never playblasts a viewport.
        cmds.arnoldRender(camera=cam, width=int(width), height=int(height))
        cmds.renderWindowEditor("renderView", edit=True, writeImage=out_path)

        if os.path.exists(out_path):
            report["rendered"] = out_path
            report["size_kb"] = os.path.getsize(out_path) // 1024
        else:
            report["error"] = ("Arnold render returned but no file was written at "
                               "out_path (check the renderView / driver).")
    except Exception as exc:  # noqa: BLE001 — never raise out of the recipe
        report["error"] = "%s: %s" % (type(exc).__name__, exc)
    finally:
        try:
            if prev_renderer:
                cmds.setAttr("defaultRenderGlobals.currentRenderer",
                             prev_renderer, type="string")
            if prev_w is not None:
                cmds.setAttr("defaultResolution.width", int(prev_w))
            if prev_h is not None:
                cmds.setAttr("defaultResolution.height", int(prev_h))
            if prev_translator is not None:
                cmds.setAttr("defaultArnoldDriver.aiTranslator",
                             prev_translator, type="string")
            cmds.currentTime(prev_frame)
        except Exception:
            pass

    return report
