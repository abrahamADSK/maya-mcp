"""
color_policy.py
===============
Single source of truth for maya-mcp's **review/preview colour management**.

Background (Chat 79): the Maya projects in this pipeline inherit Maya's
**built-in default OCIO config** — nothing sets an ``OCIO`` env var,
``colorManagementPrefs``, or an ACES config anywhere in maya-mcp, the fpt-mcp
launcher, or the Toolkit config. Its default view transform is
``"Un-tone-mapped (sRGB)"`` over a scene-linear Rec.709/sRGB working space.

Two DISTINCT knobs decide whether a preview matches the viewport — do not
conflate them:

* **VP2.0 playblasts** (``review_turntable``, ``maya_viewport_capture``) bake
  whatever **view transform** Maya's colour management is displaying. If colour
  management is off, or set to a different view, the capture comes out dark or
  mismatched. → :func:`view_transform_apply_code` pins it (best-effort) and
  :func:`view_transform_restore_code` puts it back. The Arnold driver setting
  does NOT affect these — they never pass through an Arnold driver.

* **Arnold file renders** bake the **output transform** the driver is told to
  apply. A *preview* (8-bit, display-referred PNG/JPG) MUST apply the output
  transform; a *production EXR* (scene-linear, scene-referred) MUST NOT — baking
  a display transform into an EXR destroys the linear data the comp/Flame stage
  needs. → :func:`arnold_output_transform_code` enables it for ``preview`` and
  force-DISABLES it for ``exr`` (the guardrail). The single shared
  ``defaultArnoldDriver`` is exactly why the guardrail matters: one driver feeds
  both preview and EXR output.

Every block this module emits is **best-effort and guarded**: on a Maya version
or OCIO config where a flag is absent it degrades to a no-op (the pre-Chat-79
behaviour), never raising. The Arnold driver's output-transform attribute name
is DISCOVERED at runtime via ``listAttr`` rather than hardcoded, so nothing here
depends on an unverified attribute name (the exact name was UNCONFIRMED in the
repo as of Chat 79 — it must be confirmed in a live mtoa Maya with
``cmds.listAttr('defaultArnoldDriver', string='*ransform*')`` to ever replace
discovery with a direct set).
"""

from __future__ import annotations

# Maya built-in default-config review view (Chat 79). Override per project via
# config.json -> "review_view_transform" if the project ever moves to ACES
# (then this would be e.g. "sRGB (ACES)" / "ACES 1.0 SDR-video"). This constant
# is the canonical default; review_build.py hardcodes the same literal because
# it ships standalone over the Command Port and cannot import this module inside
# Maya — tests/test_color_policy.py pins the two together so they cannot drift.
DEFAULT_REVIEW_VIEW = "Un-tone-mapped (sRGB)"


def view_transform_apply_code(
    view_transform: str = DEFAULT_REVIEW_VIEW,
    prev_var: str = "_mcp_cp_prev_view",
) -> str:
    """Code string: ensure colour management is on and the preview won't be dark.

    **Respects a deliberately-set display view.** If colour management is already
    on and the current view is a real display view (``sRGB`` / ``ACES 1.0
    SDR-video`` / ``Un-tone-mapped`` …), that view is LEFT as the user/scene chose
    it — the configured review view is NOT imposed (Chat 79: v1.22.0 wrongly
    overrode a scene's ACES SDR-video view with ``Un-tone-mapped``). The
    configured ``view_transform`` is only pinned when it would otherwise render
    dark/wrong: colour management OFF, no current view, or a scene-linear/log
    ``Raw``/``Log`` view.

    Captures the previous view into ``prev_var`` for :func:`view_transform_restore_code`
    ONLY when this code changed it; otherwise ``prev_var`` stays ``None`` (a clean
    no-op restore).
    """
    return f"""
{prev_var} = None
try:
    import maya.cmds as _mcp_cp_cmds
    _mcp_cp_was_on = bool(_mcp_cp_cmds.colorManagementPrefs(query=True, cmEnabled=True))
    _mcp_cp_cur = (_mcp_cp_cmds.colorManagementPrefs(query=True, viewTransformName=True)
                   if _mcp_cp_was_on else None)
    _mcp_cp_cmds.colorManagementPrefs(edit=True, cmEnabled=True)
    _mcp_cp_views = _mcp_cp_cmds.colorManagementPrefs(query=True, viewTransformNames=True) or []
    # Only pin the configured review view when the preview would be dark/wrong:
    # colour management was OFF, no current view, or a 'Raw'/'Log' scene-linear
    # view. A real display view already chosen is respected.
    _mcp_cp_dark = ((not _mcp_cp_was_on) or (not _mcp_cp_cur)
                    or str(_mcp_cp_cur).split(' (')[0] in ('Raw', 'Log'))
    if _mcp_cp_dark and {view_transform!r} in _mcp_cp_views:
        {prev_var} = _mcp_cp_cur
        if _mcp_cp_cur != {view_transform!r}:
            _mcp_cp_cmds.colorManagementPrefs(edit=True, viewTransformName={view_transform!r})
except Exception:
    {prev_var} = None
"""


def view_transform_restore_code(prev_var: str = "_mcp_cp_prev_view") -> str:
    """Code string: restore the view transform captured by :func:`view_transform_apply_code`.

    No-op when ``prev_var`` is falsy (nothing was changed). Place inside the
    caller's ``finally`` so the user's colour-management state is left as found.
    """
    return f"""
try:
    import maya.cmds as _mcp_cp_cmds
    if {prev_var}:
        _mcp_cp_cmds.colorManagementPrefs(edit=True, viewTransformName={prev_var})
except Exception:
    pass
"""


def arnold_output_transform_code(
    mode: str = "preview",
    view_transform: str = DEFAULT_REVIEW_VIEW,
    driver: str = "defaultArnoldDriver",
) -> str:
    """Code string: gate the Arnold **output transform** by output kind.

    ``mode="preview"`` (display-referred 8-bit PNG/JPG): enable Maya's output
    transform set to ``view_transform`` and, if the driver exposes an
    output-transform toggle, switch it on so the file matches the viewport.

    ``mode="exr"`` / ``"linear"`` (scene-linear EXR): force the driver to
    ``"Raw"`` — the guardrail that stops a display transform being baked into the
    EXR the comp/Flame stage consumes. EVERY non-preview mode forces Raw.

    The driver is steered via its ``colorManagement`` ENUM
    (``Raw:Use View Transform:Use Output Transform`` on mtoa / Maya 2027,
    confirmed in-vivo Chat 79), mapped **by name** — the enum index varies by
    mtoa version, so this never hardcodes a magic number nor an unverified
    attribute. Best-effort; never raises. Assigns a report dict to ``result``.
    """
    preview = mode == "preview"
    return f"""
_mcp_cp_report = {{"mode": {mode!r}, "global_output_transform": None, "driver_color_management": None}}
try:
    import maya.cmds as _mcp_cp_cmds
    _mcp_cp_preview = {preview!r}
    # 1) PREVIEW only: enable Maya's GLOBAL output transform, set to the review
    #    view. (For EXR we leave the global alone and force the driver to Raw —
    #    the driver=Raw is the definitive, side-effect-free guardrail.)
    if _mcp_cp_preview:
        try:
            _mcp_cp_cmds.colorManagementPrefs(edit=True, cmEnabled=True)
            _mcp_cp_cmds.colorManagementPrefs(edit=True, outputTransformEnabled=True)
            if {view_transform!r} in (_mcp_cp_cmds.colorManagementPrefs(
                    query=True, outputTransformNames=True) or []):
                _mcp_cp_cmds.colorManagementPrefs(edit=True, outputTransformName={view_transform!r})
            _mcp_cp_report["global_output_transform"] = True
        except Exception:
            pass
    # 2) Steer the Arnold driver via its colorManagement ENUM, mapped BY NAME
    #    (indices vary by mtoa version). preview -> "Use Output Transform";
    #    EXR -> "Raw" (the guardrail: a Raw driver writes scene-linear, never bakes).
    if (_mcp_cp_cmds.objExists({driver!r})
            and _mcp_cp_cmds.attributeQuery('colorManagement', node={driver!r}, exists=True)):
        _mcp_cp_enum = (_mcp_cp_cmds.attributeQuery(
            'colorManagement', node={driver!r}, listEnum=True) or [''])[0]
        _mcp_cp_opts = _mcp_cp_enum.split(':')
        _mcp_cp_target = 'Use Output Transform' if _mcp_cp_preview else 'Raw'
        if _mcp_cp_target in _mcp_cp_opts:
            _mcp_cp_cmds.setAttr({driver!r} + '.colorManagement', _mcp_cp_opts.index(_mcp_cp_target))
            _mcp_cp_report["driver_color_management"] = _mcp_cp_target
except Exception:
    pass
result = _mcp_cp_report
"""
