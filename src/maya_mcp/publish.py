"""Native Toolkit publish payloads + intent interpretation for maya-mcp.

Pure data/logic for ``maya_session(action="publish")`` (server.py keeps the
decorator + handler, per the server.py refactor recipe). Two payloads run the
NATIVE ``tk-multi-publish2`` ``PublishManager`` inside an engine'd Maya
(launched via ``tank``):

- ``PUBLISH_PREVIEW_CODE`` — collect the session and return the publish tree as
  JSON (read-only, no side effects).
- ``PUBLISH_EXECUTE_CODE`` — activate tasks per an include/exclude filter over
  the LIVE tree, then ``validate -> publish -> finalize``, returning per-item
  status as JSON. The handler prepends three injected literals (``_INCLUDE``,
  ``_EXCLUDE``, ``_COMMENT``) via ``json.dumps`` before this body.

Both payloads assign a JSON-native dict to ``result`` and never throw — a
missing engine / unconfigured publisher returns ``{"ok": false, "error": ...}``
so the LLM can tell the user to relaunch Maya through ``tank``.

API names (``create_publish_manager``, ``collect_session``, ``manager.tree``,
``item.active``/``tasks``, ``manager.validate``/``publish``/``finalize``) were
verified against the real ``tk-multi-publish2`` v2.11.0 source on disk; they are
identical across v2.10.1 / v2.10.6 / v2.11.0. Dependency capture
(``upstream_published_files``) happens automatically inside the publish plugins.
The in-Maya flow is integration-tested in-vivo; this module's host-side logic
(``expand_tokens``) is unit-tested in ``tests/test_publish.py``.
"""

from __future__ import annotations

from typing import List, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Intent synonyms (host-side). The LIVE publish tree is the source of truth;
# this map only expands natural-language intent into substring tokens that are
# matched against the real tree inside Maya — never a duplicated static map.
# ─────────────────────────────────────────────────────────────────────────────
PUBLISH_SYNONYMS = {
    # step / level intents
    "rig": ["rig"], "model": ["model"],
    "anim": ["animation", "anim"], "animation": ["animation", "anim"],
    "layout": ["layout"], "lighting": ["lighting", "light"],
    "camera": ["camera", "cam"], "comp": ["comp", "composit"],
    # output / plugin intents
    "usd": ["usd"], "fbx": ["fbx"],
    "abc": ["alembic", "abc"], "alembic": ["alembic", "abc"],
    "texture": ["texture"], "textura": ["texture"],
    "render": ["render", "rendered image", "exr"],
    "exr": ["render", "rendered image", "exr"],
    "session": ["session", "maya scene"], "scene": ["session", "maya scene"],
    "shotgrid": ["publish to shotgrid", "shotgrid"],
    "sg": ["publish to shotgrid", "shotgrid"],
    "review": ["upload", "review"],
    # NL exclude phrases (already lowercased before lookup)
    "sin render": ["render", "rendered image", "exr"],
    "no render": ["render", "rendered image", "exr"],
    "sin textura": ["texture"], "no texture": ["texture"],
}


def expand_tokens(tokens: Optional[List[str]]) -> List[str]:
    """Lowercase, expand via synonyms (keeping the literal), de-dupe in order."""
    out: List[str] = []
    for raw in tokens or []:
        t = (raw or "").strip().lower()
        if not t:
            continue
        out.append(t)
        out.extend(PUBLISH_SYNONYMS.get(t, []))
    seen, res = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            res.append(x)
    return res


# ─────────────────────────────────────────────────────────────────────────────
# PAYLOAD #1 — PREVIEW: collect + walk tree -> JSON description (read-only).
# ─────────────────────────────────────────────────────────────────────────────
PUBLISH_PREVIEW_CODE = r'''
import sgtk
import traceback


def _ctx_summary(ctx):
    """Serialize an sgtk.Context to JSON-native primitives (best-effort)."""
    def _e(entity):
        if not entity:
            return None
        if isinstance(entity, dict):
            return {"type": entity.get("type"), "id": entity.get("id"),
                    "name": entity.get("name")}
        return str(entity)
    try:
        return {
            "project": _e(getattr(ctx, "project", None)),
            "entity": _e(getattr(ctx, "entity", None)),
            "step": _e(getattr(ctx, "step", None)),
            "task": _e(getattr(ctx, "task", None)),
            "as_string": str(ctx),
        }
    except Exception:
        return {"as_string": str(ctx)}


def _safe_props(item):
    """Read ONLY global item.properties. Never touch local_properties /
    get_property() outside a hook -- they walk the stack for a Hook and raise."""
    out = {}
    try:
        props = item.properties  # PublishData == MutableMapping (global)
        for key in ("path", "publish_name", "publish_type",
                    "publish_version", "publish_path"):
            try:
                if key in props:
                    out[key] = str(props[key])
            except Exception:
                pass
    except Exception:
        pass
    return out


_payload = {}
try:
    engine = sgtk.platform.current_engine()
    if engine is None:
        _payload = {
            "ok": False, "error": "no_engine",
            "hint": "No Toolkit engine in this Maya. Launch Maya via 'tank' / "
                    "Toolkit into a Task/Step context so a tk-maya engine is "
                    "loaded and tk-multi-publish2 is available.",
        }
    else:
        app = engine.apps.get("tk-multi-publish2")
        if app is None:
            _payload = {
                "ok": False, "error": "publisher_not_configured",
                "engine": engine.name,
                "available_apps": sorted(engine.apps.keys()),
                "hint": "tk-multi-publish2 is not configured for the current "
                        "context. Open a Task/Step whose environment includes "
                        "the publisher.",
            }
        else:
            manager = app.create_publish_manager()
            manager.collect_session()

            items = []
            for item in manager.tree:          # depth-first over all descendants
                tasks = []
                for task in item.tasks:
                    plugin = task.plugin
                    # keep it lean: drop per-plugin description + item_filters
                    # (verbose, rarely needed — they bloat the preview payload).
                    tasks.append({
                        "task_name": task.name,
                        "plugin_name": plugin.name,
                        "active": bool(task.active),
                        "visible": bool(task.visible),
                        "enabled": bool(task.enabled),
                    })
                parent = item.parent
                top_level = (parent is not None and parent.is_root)
                items.append({
                    "name": item.name,
                    "type_spec": item.type_spec,
                    "type_display": item.type_display,
                    "active": bool(item.active),
                    "enabled": bool(item.enabled),
                    "persistent": bool(item.persistent),
                    "is_top_level": top_level,
                    "parent": (None if (parent is None or parent.is_root)
                               else parent.name),
                    "context": _ctx_summary(item.context),
                    "properties": _safe_props(item),
                    "tasks": tasks,
                })

            _payload = {
                "ok": True,
                "mode": "preview",
                "engine": engine.name,
                "publisher_version": getattr(app, "version", None),
                "session_context": _ctx_summary(manager.context),
                "item_count": len(items),
                "items": items,
            }
except Exception as _e:
    _payload = {
        "ok": False, "error": "preview_failed",
        "exception": "%s: %s" % (type(_e).__name__, _e),
        "traceback": traceback.format_exc()[-1500:],
    }

result = _payload
'''


# ─────────────────────────────────────────────────────────────────────────────
# PAYLOAD #2 — PUBLISH: filter -> validate -> publish -> finalize -> JSON.
# Handler prepends `_INCLUDE`, `_EXCLUDE`, `_COMMENT` literals (json.dumps).
# ─────────────────────────────────────────────────────────────────────────────
PUBLISH_EXECUTE_CODE = r'''
import sgtk
import traceback


def _ctx_summary(ctx):
    def _e(entity):
        if not entity:
            return None
        if isinstance(entity, dict):
            return {"type": entity.get("type"), "id": entity.get("id"),
                    "name": entity.get("name")}
        return str(entity)
    try:
        return {"project": _e(getattr(ctx, "project", None)),
                "entity": _e(getattr(ctx, "entity", None)),
                "step": _e(getattr(ctx, "step", None)),
                "task": _e(getattr(ctx, "task", None)),
                "as_string": str(ctx)}
    except Exception:
        return {"as_string": str(ctx)}


def _match_parts(item, task):
    """Lowercased haystack that include/exclude tokens are substring-matched
    against: plugin name, item type_spec/type_display/name, and step name."""
    parts = []
    for getter in (
        lambda: task.plugin.name,
        lambda: getattr(task.plugin, "plugin_name", ""),
        lambda: item.type_spec,
        lambda: item.type_display,
        lambda: item.name,
    ):
        try:
            parts.append((getter() or "").lower())
        except Exception:
            pass
    try:
        step = getattr(item.context, "step", None)
        if isinstance(step, dict):
            parts.append((step.get("name") or "").lower())
    except Exception:
        pass
    return parts


def _hit(tokens, parts):
    for tok in tokens:
        t = (tok or "").strip().lower()
        if not t:
            continue
        for h in parts:
            if t in h:
                return tok
    return None


_payload = {}
try:
    engine = sgtk.platform.current_engine()
    if engine is None:
        _payload = {"ok": False, "error": "no_engine",
                    "hint": "No Toolkit engine in this Maya. Launch via 'tank' "
                            "into a Task/Step context."}
    else:
        app = engine.apps.get("tk-multi-publish2")
        if app is None:
            _payload = {"ok": False, "error": "publisher_not_configured",
                        "engine": engine.name,
                        "available_apps": sorted(engine.apps.keys()),
                        "hint": "tk-multi-publish2 not configured for this context."}
        else:
            manager = app.create_publish_manager()
            manager.collect_session()

            all_tasks = [(item, task)
                         for item in manager.tree
                         for task in item.tasks]

            if not all_tasks:
                _payload = {"ok": False, "error": "empty_tree",
                            "hint": "collect_session produced no publishable "
                                    "tasks for this context."}
            else:
                # ---- activation over the LIVE tree ----------------------------
                # NOTE: keep the response LEAN. The LLM only needs what got
                # published (+ failures); the full per-(item,task) tree and the
                # all-passed validation list are debug noise that bloats the
                # context and can stall the console's next request (Chat 72).
                activated = 0
                for item, task in all_tasks:
                    parts = _match_parts(item, task)
                    inc_hit = _hit(_INCLUDE, parts) if _INCLUDE else None
                    exc_hit = _hit(_EXCLUDE, parts)
                    if _INCLUDE:
                        # whitelist: only include-matched, minus excludes
                        want = (inc_hit is not None) and (exc_hit is None)
                    else:
                        # blacklist: collector defaults minus excludes
                        want = exc_hit is None
                    task.active = bool(want)
                    if want:
                        activated += 1

                # keep item.active in sync; stamp the publish comment
                for item in manager.tree:
                    if item.tasks:
                        has_active = any(t.active for t in item.tasks)
                        item.active = bool(has_active)
                        if has_active and _COMMENT:
                            try:
                                item.description = _COMMENT
                            except Exception:
                                pass

                _activation = {"total_tasks": len(all_tasks),
                               "activated": activated,
                               "include": _INCLUDE, "exclude": _EXCLUDE}
                active_tasks = [(i, t) for (i, t) in all_tasks if t.active]
                if not active_tasks:
                    _payload = {"ok": False, "error": "nothing_selected",
                                "hint": "include/exclude deactivated every task.",
                                "activation": _activation}
                else:
                    # ---- VALIDATE (default generator = active items/tasks) ----
                    failed = manager.validate()  # [(task, exc|None), ...] FAILED
                    failed_ids = set(id(t) for (t, _x) in failed)
                    # keep ONLY the failures (on success this is empty -> omitted)
                    failures = []
                    for item, task in active_tasks:
                        if id(task) not in failed_ids:
                            continue
                        err = None
                        for (ft, fe) in failed:
                            if ft is task:
                                err = (("%s: %s" % (type(fe).__name__, fe))
                                       if fe else "validation returned False")
                                break
                        failures.append({
                            "item": item.name, "task": task.name,
                            "plugin": task.plugin.name, "error": err,
                        })

                    if failed:
                        _payload = {"ok": False, "mode": "publish",
                                    "phase": "validate",
                                    "error": "validation_failed",
                                    "comment": _COMMENT,
                                    "session_context": _ctx_summary(manager.context),
                                    "activation": _activation,
                                    "failures": failures}
                    else:
                        phase = "publish"
                        pub_error = None
                        try:
                            manager.publish()
                            phase = "finalize"
                            manager.finalize()
                            phase = "done"
                        except Exception as _pe:
                            pub_error = "%s: %s" % (type(_pe).__name__, _pe)

                        published = []
                        for item, task in active_tasks:
                            sg = None
                            try:
                                data = item.properties.get("sg_publish_data")
                                if isinstance(data, dict):
                                    path = data.get("path")
                                    local = (path.get("local_path")
                                             if isinstance(path, dict) else None)
                                    sg = {"id": data.get("id"),
                                          "type": data.get("type"),
                                          "name": data.get("code") or data.get("name"),
                                          "path": local,
                                          "version_number": data.get("version_number")}
                            except Exception:
                                sg = None
                            published.append({
                                "item": item.name, "task": task.name,
                                "plugin": task.plugin.name,
                                "type_spec": item.type_spec, "sg_publish": sg})

                        _payload = {
                            "ok": pub_error is None, "mode": "publish",
                            "phase": phase, "error": pub_error,
                            "comment": _COMMENT,
                            "session_context": _ctx_summary(manager.context),
                            "activation": _activation,
                            "published": published,
                            "published_count": len(published),
                        }
except Exception as _e:
    _payload = {"ok": False, "error": "publish_failed",
                "exception": "%s: %s" % (type(_e).__name__, _e),
                "traceback": traceback.format_exc()[-1500:]}

result = _payload
'''
