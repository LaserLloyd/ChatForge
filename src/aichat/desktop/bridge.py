"""JS <-> Python bridge: the ``Api`` object pywebview exposes as ``js_api`` (PLAN 1.5).

Contract: ``dev-mock.js`` in ``web/static/js`` is the reference. ``Api`` implements every
method the mock implements, with the same argument order and reply shapes. pywebview walks
*public* attributes of ``js_api`` and turns every one into a JS function, so ``Api`` has
**no public attributes other than the contract methods**; every service lives on
``self._s`` (:class:`Services`).

Threading: pywebview calls these methods on its own worker threads. Anything owned by the
asyncio loop goes through ``self._s.loop.run(...)`` (blocking, with a timeout); long work
is submitted and reports through :class:`~aichat.desktop.events.EventSink`.

Replies are ``{"ok": true, ...}`` or ``{"ok": false, "error": {code, message, hint,
action}}``. API keys are used transiently (``save_api_key``, ``test_provider``) and never
returned, logged or stored anywhere but the keyring.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import functools
import itertools
import logging
import sys
import threading
import time
import webbrowser
from collections.abc import Callable, Coroutine
from typing import Any

from pydantic import ValidationError

from aichat import attachments, autostart, secrets
from aichat.config import (
    RECENT_MODELS_KEPT,
    AppConfig,
    ProviderSpec,
    seed_providers,
    update_config,
)
from aichat.errors import AppError, ConfigError
from aichat.logging_setup import RING_BUFFER
from aichat.paths import Paths

_log = logging.getLogger(__name__)

#: The public surface, in contract order (PLAN 1.5 and dev-mock.js).
CONTRACT_METHODS: tuple[str, ...] = (
    "get_state",
    "send_message",
    "regenerate",
    "attach_files",
    "attach_data",
    "remove_attachment",
    "open_document",
    "reveal_document",
    "stop_generation",
    "new_chat",
    "select_model",
    "load_model",
    "unload_model",
    "hide_popup",
    "set_pinned",
    "open_settings",
    "open_external",
    "save_api_key",
    "remove_api_key",
    "test_provider",
    "refresh_models",
    "get_settings",
    "update_settings",
    "list_providers",
    "upsert_provider",
    "remove_provider",
    "list_models",
    "delete_model",
    "clear_compile_cache",
    "search_models",
    "repo_details",
    "start_download",
    "cancel_download",
    "resume_download",
    "list_downloads",
    "runtime_install",
    "runtime_status",
    "get_logs",
    "open_logs_folder",
    "get_autostart",
    "set_autostart",
    "set_hotkey",
    "disk_usage",
)

LOCAL_ID = "local-npu"
SEED_IDS = frozenset(seed_providers())
DOWNLOAD_HEADROOM_BYTES = 2 * 1024**3
CALL_TIMEOUT_S = 30.0
PROBE_TIMEOUT_S = 90.0
NETWORK_TIMEOUT_S = 60.0
#: How many stopped request ids ``Api`` remembers (to skip ``on_reply_end`` for them).
STOPPED_IDS_KEPT = 64

_DOWNLOAD_STATUS = {
    "queued": "queued",
    "running": "downloading",
    "paused": "paused",
    "completed": "done",
    "failed": "error",
    "canceled": "cancelled",
}

_LOG_ORDER = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


# --------------------------------------------------------------------------- #
# Reply helpers (module level so app.py can reuse the shapes)
# --------------------------------------------------------------------------- #


def ok(**fields: Any) -> dict[str, Any]:
    return {"ok": True, **fields}


def fail(
    code: str, message: str, hint: str = "", action: str | None = None, **extra: Any
) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {"code": code, "message": message, "hint": hint, "action": action},
        **extra,
    }


def error_payload(exc: BaseException) -> dict[str, Any]:
    """``{code, message, hint, action}`` for any exception the services raise."""
    to_payload = getattr(exc, "to_payload", None)
    if callable(to_payload):
        payload = dict(to_payload())
        payload.setdefault("code", getattr(exc, "code", None) or "server")
        payload.setdefault("message", str(exc))
        payload.setdefault("hint", None)
        payload.setdefault("action", None)
        return payload
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        # WS1's OvmsError / InstallError: shaped like AppError but not subclasses of it.
        payload = {
            "code": code,
            "message": str(getattr(exc, "message", None) or exc),
            "hint": getattr(exc, "hint", None),
            "action": getattr(exc, "action", None),
        }
        details = getattr(exc, "details", None)
        if isinstance(details, dict) and details:
            payload["details"] = details
        return payload
    if isinstance(exc, ValidationError):
        errors = {".".join(str(p) for p in e["loc"]) or "value": e["msg"] for e in exc.errors()}
        first = next(iter(errors.items()))
        return {
            "code": "bad_request",
            "message": f"{first[0]}: {first[1]}",
            "hint": "",
            "action": None,
            "details": {"errors": errors},
        }
    if isinstance(exc, TimeoutError):
        return {
            "code": "timeout",
            "message": "The operation timed out.",
            "hint": "",
            "action": "retry",
        }
    if isinstance(exc, ValueError):
        return {"code": "bad_request", "message": str(exc), "hint": "", "action": None}
    return {
        "code": "server",
        "message": f"{type(exc).__name__}: {str(exc)[:200]}",
        "hint": "See Settings → Logs.",
        "action": None,
    }


def ui_config(cfg: AppConfig) -> dict[str, Any]:
    """The subset of the config both pages need (``get_state.config``, ``settings.changed``)."""
    return {
        "chat": {
            "provider": cfg.chat.provider,
            "model": cfg.chat.model,
            "show_reasoning": cfg.chat.show_reasoning,
            "max_prompt_chars": cfg.chat.max_prompt_chars,
            "recent_models": [r.model_dump() for r in cfg.chat.recent_models],
        },
        "ui": cfg.ui.model_dump(mode="json"),
        "local": {
            "device": cfg.local.device,
            "idle_unload_minutes": cfg.local.idle_unload_minutes,
            "autoload_on_open": cfg.local.autoload_on_open,
        },
    }


def download_view(summary: dict[str, Any]) -> dict[str, Any]:
    """Downloader group summary -> the ``download.progress`` / ``list_downloads`` shape."""
    gid = summary.get("group_id")
    status = _DOWNLOAD_STATUS.get(str(summary.get("status")), str(summary.get("status")))
    return {
        "group_id": gid,
        "download_id": gid,
        "repo_id": summary.get("repo_id"),
        "status": status,
        "downloaded_bytes": int(summary.get("downloaded_bytes") or 0),
        "total_bytes": int(summary.get("total_bytes") or 0),
        "percent": float(summary.get("percent") or 0.0),
        "speed_bps": float(summary.get("speed_bps") or 0.0),
        "eta_s": summary.get("eta_s"),
        "file": summary.get("current_file"),
        "files_done": int(summary.get("files_done") or 0),
        "files_total": int(summary.get("files_total") or 0),
        "error": summary.get("error"),
    }


def runtime_install_view(progress: dict[str, Any]) -> dict[str, Any]:
    """Normalise an ``ovms_install`` progress dict to the ``runtime.install`` event fields."""
    return {
        "status": progress.get("status", "downloading"),
        "downloaded_bytes": int(progress.get("downloaded_bytes") or 0),
        "total_bytes": int(progress.get("total_bytes") or 0),
        "speed_bps": float(progress.get("speed_bps") or 0.0),
        "eta_s": progress.get("eta_s"),
        "error": progress.get("error"),
    }


def format_log_line(record: dict[str, Any]) -> str:
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(record.get("ts") or 0)))
    return f"{ts} {str(record.get('level', 'INFO')):<7} {record.get('logger', '')}: {record.get('message', '')}"


# --------------------------------------------------------------------------- #
# Services
# --------------------------------------------------------------------------- #


class Services:
    """Everything ``Api`` needs, wired by ``app.py``. Unset services are ``None`` and the
    corresponding methods answer with a clean error, so partial wiring (tests, WS6 not yet
    landed) never crashes the UI."""

    def __init__(self, paths: Paths, config: AppConfig) -> None:
        self.paths = paths
        self.config = config
        self.config_lock = threading.RLock()
        self.loop: Any = None  # CoreLoop
        self.events: Any = None  # EventSink
        self.popup: Any = None
        self.settings_window: Any = None
        self.hotkey: Any = None
        self.providers: Any = None  # llm.providers.ProviderRegistry
        self.tools: Any = None
        self.registry: Any = None  # models.registry.Registry
        self.catalog: Any = None
        self.hf: Any = None  # models.hf_search.HfSearch
        self.downloader: Any = None
        self.manager: Any = None  # runtime.manager.LocalModelManager (WS6)
        self.engine: Any = None  # chat.engine.ChatEngine (WS6)
        #: Called with the new config after every persisted change.
        self.on_config_changed: Callable[[AppConfig], None] | None = None
        #: Called on the core loop (so it must not block) with the request id when a reply
        #: ends with ``chat.done`` or an error, unless it was stopped (``cancelled``, or
        #: ``stop_generation`` was called for it: the page abandoned it).
        self.on_reply_end: Callable[[str], None] | None = None
        self.install_task: Any = None
        self.installing = False
        #: Files attached in the composer, waiting for ``send_message``.
        self.attachments = attachments.AttachmentStore()


# --------------------------------------------------------------------------- #
# Api
# --------------------------------------------------------------------------- #


class Api:
    """The ``js_api`` object. Public attributes == :data:`CONTRACT_METHODS`, nothing else."""

    def __init__(self, services: Services) -> None:
        self._s = services
        self._request_ids = itertools.count(1)
        #: Request ids the page stopped or abandoned (``stop_generation``), newest last.
        self._stopped_ids: collections.deque[str] = collections.deque(maxlen=STOPPED_IDS_KEPT)

    # --- plumbing ---------------------------------------------------------------------

    @property
    def _cfg(self) -> AppConfig:
        return self._s.config

    def _guard(self, fn: Callable[[], dict[str, Any]], what: str) -> dict[str, Any]:
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - every bridge call answers with JSON
            payload = error_payload(exc)
            level = logging.WARNING if payload.get("code") != "server" else logging.ERROR
            _log.log(
                level, "bridge %s failed: %s: %s", what, type(exc).__name__, payload["message"]
            )
            if level == logging.ERROR:
                _log.debug("bridge %s traceback", what, exc_info=exc)
            return {"ok": False, "error": payload}
        return result if result is not None else ok()

    def _run(self, coro: Coroutine[Any, Any, Any], timeout: float = CALL_TIMEOUT_S) -> Any:
        loop = self._s.loop
        if loop is None:
            coro.close()
            raise AppError("The core loop is not running.", code="server")
        return loop.run(coro, timeout)

    def _submit(self, coro: Coroutine[Any, Any, Any], name: str) -> None:
        loop = self._s.loop
        if loop is None:
            coro.close()
            raise AppError("The core loop is not running.", code="server")
        loop.submit(coro, name=name)

    def _emit(self, evt: dict[str, Any]) -> None:
        if self._s.events is not None:
            self._s.events.emit(evt)

    def _apply_patch(self, patch: dict[str, Any]) -> tuple[AppConfig, list[str]]:
        """Persist a config patch and tell the services. Raises ``ConfigError``."""
        with self._s.config_lock:
            new_cfg, restart = update_config(self._s.config, patch, self._s.paths)
            self._s.config = new_cfg
        providers = self._s.providers
        if providers is not None:
            with contextlib.suppress(Exception):
                providers.replace_all(new_cfg.providers)
        if self._s.on_config_changed is not None:
            try:
                self._s.on_config_changed(new_cfg)
            except Exception:  # noqa: BLE001
                _log.exception("on_config_changed hook failed")
        return new_cfg, restart

    def _spec(self, provider_id: str) -> ProviderSpec:
        providers = self._s.providers
        if providers is not None:
            return providers.spec(provider_id)
        spec = self._cfg.providers.get(provider_id)
        if spec is None:
            raise AppError(f"Unknown provider {provider_id!r}.", code="not_found")
        return spec

    def _is_local(self, provider_id: str) -> bool:
        return self._spec(provider_id).kind == "ovms"

    def _local_models(self) -> list[str]:
        registry = self._s.registry
        if registry is None:
            return []
        return [r.id for r in registry.all() if r.complete]

    def _key_status(self, spec: ProviderSpec) -> dict[str, Any]:
        if spec.kind == "ovms":
            return {
                "source": "none",
                "env_name": None,
                "env_overrides_saved": False,
                "required": False,
            }
        providers = self._s.providers
        if providers is not None:
            with contextlib.suppress(Exception):
                return dict(providers.get(spec.id).key_status())
        status = dict(secrets.key_status(spec.id, spec.api_key_env))
        status["required"] = True
        return status

    def _provider_view(self, spec: ProviderSpec) -> dict[str, Any]:
        cfg = self._cfg
        if spec.kind == "ovms":
            models = self._local_models()
            default = (
                cfg.chat.model if cfg.chat.model in models else (models[0] if models else None)
            )
        else:
            models = list(spec.models)
            default = spec.default_model or (models[0] if models else None)
        return {
            "id": spec.id,
            "display_name": spec.display_name,
            "kind": spec.kind,
            "models": models,
            "default_model": default,
            "region": spec.region,
            "base_url": spec.base_url,
            # Seeded providers cannot be removed, so they show as built in too.
            "builtin": bool(spec.builtin) or spec.id in SEED_IDS,
            "docs_url": spec.docs_url,
            "key": self._key_status(spec),
        }

    def _provider_views(self) -> list[dict[str, Any]]:
        providers = self._s.providers
        specs = providers.list() if providers is not None else list(self._cfg.providers.values())
        return [self._provider_view(spec) for spec in specs]

    def _runtime_status(self) -> dict[str, Any]:
        cfg = self._cfg
        status: dict[str, Any] = {}
        manager = self._s.manager
        if manager is not None:
            with contextlib.suppress(Exception):
                status = dict(manager.status())
        if "state" not in status:
            installed = True
            with contextlib.suppress(Exception):
                from aichat.runtime import ovms_install

                installed = bool(ovms_install.runtime_status(self._s.paths, cfg.local)["installed"])
            status["state"] = "unloaded" if installed else "not_installed"
        status.setdefault("model_id", None)
        status.setdefault("device", cfg.local.device)
        status.setdefault("elapsed_s", 0)
        status.setdefault("expected_s", 0)
        status.setdefault("first_compile", False)
        status.setdefault("idle_timeout_s", cfg.local.idle_unload_minutes * 60)
        status.setdefault("unload_at", None)
        status.setdefault("error", None)
        return status

    def _conversation(self) -> list[dict[str, Any]]:
        engine = self._s.engine
        if engine is None:
            return []
        try:
            snap = engine.snapshot()
        except Exception:  # noqa: BLE001
            _log.exception("engine.snapshot failed")
            return []
        if isinstance(snap, list):
            return snap
        if isinstance(snap, dict):
            for key in ("conversation", "messages", "history"):
                if isinstance(snap.get(key), list):
                    return snap[key]
        return []

    def _model_loaded(self, model_id: str) -> bool:
        status = self._runtime_status()
        return status.get("model_id") == model_id and status.get("state") not in (
            "unloaded",
            "not_installed",
            None,
        )

    def _local_target_model(self) -> str | None:
        cfg = self._cfg
        models = self._local_models()
        if cfg.chat.model in models:
            return cfg.chat.model
        return models[0] if models else None

    def _warm_local_model(self) -> None:
        """Warm the local model in the background (``local.autoload_on_open``)."""
        manager = self._s.manager
        model = self._local_target_model()
        if manager is None or model is None or self._s.loop is None:
            return
        if self._model_loaded(model):
            return

        async def _warm() -> None:
            try:
                await manager.ensure_loaded(model)
            except Exception as exc:  # noqa: BLE001 - status events already carry the error
                _log.warning("warm-up of %s failed: %s", model, type(exc).__name__)

        self._submit(_warm(), "warm-local-model")

    # ======================================================================= #
    # Contract methods
    # ======================================================================= #

    # --- state and chat (popup) --------------------------------------------------------

    def get_state(self) -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            cfg = self._cfg
            return ok(
                config=ui_config(cfg),
                providers=self._provider_views(),
                selected={"provider": cfg.chat.provider, "model": cfg.chat.model},
                runtime=self._runtime_status(),
                conversation=self._conversation(),
                limits={"max_prompt_chars": cfg.chat.max_prompt_chars},
                theme=cfg.ui.theme,
            )

        return self._guard(impl, "get_state")

    def send_message(self, text: str, attachment_ids: list[str] | None = None) -> dict[str, Any]:
        """Send the typed text plus the attached files ``attachment_ids`` (ids from
        ``attach_files`` / ``attach_data``). A message may be files only. The ids stay
        valid until the reply finishes (``chat.done``) or is stopped, so a Retry after an
        error can send them again."""

        def impl() -> dict[str, Any]:
            message = str(text or "")
            ids = self._attachment_ids(attachment_ids)
            if not message.strip() and not ids:
                return fail("bad_request", "Empty message.")
            if len(ids) > attachments.MAX_FILES:
                return fail(
                    "bad_request",
                    f"Attach at most {attachments.MAX_FILES} files to one message.",
                    "Remove some files and send again.",
                )
            limit = self._cfg.chat.max_prompt_chars
            if len(message) > limit:
                return fail(
                    "context_overflow",
                    f"Message is longer than {limit} characters.",
                    "Shorten the message.",
                )
            engine = self._s.engine
            if engine is None:
                return fail(
                    "server",
                    "The chat engine is not available.",
                    "See Settings → Logs.",
                    "open_settings",
                )
            files = self._s.attachments.peek(ids)  # raises not_found for a stale id
            request_id = f"req_{next(self._request_ids)}"
            self._remember_current_model()  # before the reply's events start
            if files:
                emit = self._report_reply_end(request_id, self._emit_then_release(ids))
                coro = engine.send(message, request_id, emit, attachments=files)
            else:
                emit = self._report_reply_end(request_id, self._emit)
                coro = engine.send(message, request_id, emit)
            self._submit(coro, f"chat:{request_id}")
            return ok(request_id=request_id)

        return self._guard(impl, "send_message")

    def regenerate(self) -> dict[str, Any]:
        """Answer the latest message again (the Regenerate button): its reply is replaced
        by a new one, with the usual ``chat.*`` events under the returned ``request_id``.
        If the new reply fails or is stopped before it says anything, the old one is kept
        (``chat.error`` with ``restored: true``)."""

        def impl() -> dict[str, Any]:
            engine = self._s.engine
            if engine is None:
                return fail(
                    "server",
                    "The chat engine is not available.",
                    "See Settings → Logs.",
                    "open_settings",
                )
            request_id = f"req_{next(self._request_ids)}"
            self._remember_current_model()
            emit = self._report_reply_end(request_id, self._emit)
            self._submit(engine.regenerate(request_id, emit), f"chat:{request_id}")
            return ok(request_id=request_id)

        return self._guard(impl, "regenerate")

    @staticmethod
    def _recent_with(cfg: AppConfig, provider: str, model: str) -> list[dict[str, str]] | None:
        """``chat.recent_models`` with ``provider``/``model`` first, or ``None`` when it
        is first already."""
        recent = [r.model_dump() for r in cfg.chat.recent_models]
        entry = {"provider": provider, "model": model}
        if recent and recent[0] == entry:
            return None
        return [entry, *(r for r in recent if r != entry)][:RECENT_MODELS_KEPT]

    def _remember_current_model(self) -> None:
        """Put the model a message was just sent with at the top of the model menu's
        "Recently used" section. Never fails the send."""
        try:
            cfg = self._cfg
            if not cfg.chat.provider or not cfg.chat.model:
                return
            recent = self._recent_with(cfg, cfg.chat.provider, cfg.chat.model)
            if recent is None:
                return
            new_cfg, _restart = self._apply_patch({"chat": {"recent_models": recent}})
            self._emit({"type": "settings.changed", "config": ui_config(new_cfg)})
        except Exception:  # noqa: BLE001 - a settings write must not fail the message
            _log.exception("could not record the recently used model")

    def _emit_then_release(self, ids: list[str]) -> Callable[[dict[str, Any]], None]:
        """An event sink that forgets the attached files ``ids`` once the message they
        were sent with is answered or stopped (they are in the conversation then)."""

        def emit(evt: dict[str, Any]) -> None:
            etype = evt.get("type")
            if etype == "chat.done" or (etype == "chat.error" and evt.get("code") == "cancelled"):
                self._s.attachments.discard(ids)
            self._emit(evt)

        return emit

    def _report_reply_end(
        self, request_id: str, emit: Callable[[dict[str, Any]], None]
    ) -> Callable[[dict[str, Any]], None]:
        """Wrap ``emit`` so that, after a reply's last event (``chat.done``, or a
        ``chat.error`` that is not a stop) went to the page, ``on_reply_end`` hears of it.
        Not for a request the page stopped or abandoned (``stop_generation``)."""

        def wrapped(evt: dict[str, Any]) -> None:
            emit(evt)
            etype = evt.get("type")
            if etype == "chat.done" or (etype == "chat.error" and evt.get("code") != "cancelled"):
                hook = self._s.on_reply_end
                if hook is None or request_id in self._stopped_ids:
                    return
                try:
                    hook(request_id)
                except Exception:  # noqa: BLE001 - a broken hook must not break the turn
                    _log.exception("on_reply_end hook failed")

        return wrapped

    # --- attachments (popup) ---------------------------------------------------------------

    @staticmethod
    def _attachment_ids(value: Any) -> list[str]:
        """``attachment_ids`` as a list of unique strings (``None`` or one id accepted)."""
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list | tuple):
            raise AppError("attachment_ids must be a list of ids.", code="bad_request")
        return list(dict.fromkeys(str(v) for v in value if v))

    def _attach(self, loaders: list[tuple[str, Callable[[], Any]]]) -> dict[str, Any]:
        """Run each ``(name, load)`` (``load`` returns ``(Extracted, size)``) and store what
        reads. ``{ok, attachments: [{id, name, kind, chars, size, truncated, warning}],
        errors: [{name, message}]}``; a file that cannot be read is an error entry, not a
        failed call."""
        added: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for i, (name, load) in enumerate(loaders):
            if i >= attachments.MAX_FILES:
                errors.append(
                    {
                        "name": name,
                        "message": f"Only {attachments.MAX_FILES} files can be attached "
                        "to one message.",
                    }
                )
                continue
            try:
                file, size = load()
            except AppError as exc:
                errors.append({"name": name, "message": exc.message})
                continue
            except Exception as exc:  # noqa: BLE001 - one bad file must not fail the rest
                _log.warning("attachment %s failed: %s", i, type(exc).__name__)
                errors.append({"name": name, "message": "The file could not be read."})
                continue
            added.append(self._s.attachments.add(file, size).view())
        _log.info("attached %d file(s), %d refused", len(added), len(errors))
        return ok(attachments=added, errors=errors)

    def _dialog_window(self) -> Any:
        """The window whose page made this call (popup or settings), for the file dialog:
        found on pywebview's call stack, else the active window, else the popup."""
        frame = sys._getframe(1)
        while frame is not None:
            # webview/util.py ``js_bridge_call`` runs each call in a ``_call`` closure that
            # holds the calling ``window``.
            if frame.f_code.co_name == "_call" and frame.f_globals.get("__name__") == (
                "webview.util"
            ):
                window = frame.f_locals.get("window")
                if window is not None and hasattr(window, "create_file_dialog"):
                    return window
            frame = frame.f_back
        with contextlib.suppress(Exception):
            import webview

            window = webview.active_window()
            if window is not None:
                return window
        popup = self._s.popup
        return getattr(popup, "window", None) if popup is not None else None

    def attach_files(self) -> dict[str, Any]:
        """Open the native multi-select file dialog of the calling window and attach the
        chosen files. ``cancelled: true`` when the dialog was closed without a choice."""

        def impl() -> dict[str, Any]:
            import webview

            window = self._dialog_window()
            if window is None:
                return fail("server", "No window is open to show the file dialog.")
            popup = self._s.popup
            suspend = getattr(popup, "suspend_blur", None)
            on_popup = popup is not None and window is getattr(popup, "window", None)
            with suspend() if on_popup and callable(suspend) else contextlib.nullcontext():
                paths = window.create_file_dialog(
                    webview.FileDialog.OPEN,
                    allow_multiple=True,
                    file_types=attachments.DIALOG_FILE_TYPES,
                )
            if not paths:
                return ok(attachments=[], errors=[], cancelled=True)
            if isinstance(paths, str):
                paths = [paths]
            max_chars = int(self._cfg.tools.attachment_max_chars)
            loaders = [
                (
                    attachments.clean_name(p),
                    functools.partial(attachments.read_path, p, max_chars=max_chars),
                )
                for p in paths
            ]
            return self._attach(loaders)

        return self._guard(impl, "attach_files")

    def attach_data(self, name: str, base64_data: str) -> dict[str, Any]:
        """Attach a dropped or pasted file: ``base64_data`` is plain base64 or a ``data:``
        URL. The size is checked before it is decoded. Same reply as ``attach_files``."""

        def impl() -> dict[str, Any]:
            file_name = attachments.clean_name(name)
            max_chars = int(self._cfg.tools.attachment_max_chars)

            def load() -> tuple[Any, int]:
                data = attachments.decode_base64(str(base64_data or ""))
                return attachments.extract_text(file_name, data, max_chars=max_chars), len(data)

            return self._attach([(file_name, load)])

        return self._guard(impl, "attach_data")

    def remove_attachment(self, attachment_id: str) -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            return ok(removed=self._s.attachments.remove(str(attachment_id or "")))

        return self._guard(impl, "remove_attachment")

    # --- documents (popup) -----------------------------------------------------------------

    def _document_path(self, path: str) -> Any:
        from aichat.tools import documents

        folder = documents.documents_dir(self._cfg.tools.documents_dir)
        return documents.resolve_document(folder, path)

    def open_document(self, path: str) -> dict[str, Any]:
        """Open a file ``create_document`` saved (only files in the documents folder)."""

        def impl() -> dict[str, Any]:
            from aichat.tools import documents

            target = self._document_path(path)
            documents.open_document(target)
            return ok(path=str(target))

        return self._guard(impl, "open_document")

    def reveal_document(self, path: str) -> dict[str, Any]:
        """Show a saved file selected in Explorer (only files in the documents folder)."""

        def impl() -> dict[str, Any]:
            from aichat.tools import documents

            target = self._document_path(path)
            documents.reveal_document(target)
            return ok(path=str(target))

        return self._guard(impl, "reveal_document")

    def stop_generation(self, request_id: str) -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            engine = self._s.engine
            if request_id:
                self._stopped_ids.append(str(request_id))  # no on_reply_end for it
            if engine is not None and request_id:
                self._s.loop.call(engine.cancel, str(request_id))
            return ok()

        return self._guard(impl, "stop_generation")

    def new_chat(self) -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            engine = self._s.engine
            if engine is not None:
                self._s.loop.call(engine.new_chat)
            return ok()

        return self._guard(impl, "new_chat")

    def select_model(self, provider_id: str, model_id: str | None = None) -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            spec = self._spec(str(provider_id))
            view = self._provider_view(spec)
            wanted = str(model_id).strip() if model_id else ""
            if spec.kind == "ovms":
                models = view["models"]
                if wanted and wanted not in models:
                    return fail(
                        "not_found",
                        f"{wanted} is not installed.",
                        "Download it in Settings → Models.",
                    )
                model = wanted or view["default_model"] or self._cfg.chat.model
            else:
                model = wanted or view["default_model"] or ""
                if not model:
                    return fail("bad_request", f"{spec.display_name} has no model configured.")
            chat: dict[str, Any] = {"provider": spec.id, "model": model}
            recent = self._recent_with(self._cfg, spec.id, model)
            if recent is not None:
                chat["recent_models"] = recent
            new_cfg, _restart = self._apply_patch({"chat": chat})
            self._emit({"type": "settings.changed", "config": ui_config(new_cfg)})
            if spec.kind == "ovms" and new_cfg.local.autoload_on_open:
                self._warm_local_model()
            return ok(selected={"provider": spec.id, "model": model})

        return self._guard(impl, "select_model")

    def load_model(self) -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            manager = self._s.manager
            if manager is None:
                return fail("server", "The local runtime is not available.", "See Settings → Logs.")
            model = self._local_target_model()
            if model is None:
                return fail(
                    "not_found",
                    "No local model is installed.",
                    "Download one in Settings → Models.",
                )

            async def _load() -> None:
                try:
                    await manager.ensure_loaded(model)
                except Exception as exc:  # noqa: BLE001 - reported through runtime.status
                    _log.warning("load of %s failed: %s", model, type(exc).__name__)

            self._submit(_load(), "load-model")
            return ok(model=model)

        return self._guard(impl, "load_model")

    def unload_model(self) -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            manager = self._s.manager
            if manager is None:
                return fail("server", "The local runtime is not available.")
            self._submit(manager.unload("user"), "unload-model")
            return ok()

        return self._guard(impl, "unload_model")

    # --- window ops (popup) ---------------------------------------------------------------

    def hide_popup(self) -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            if self._s.popup is not None:
                self._s.popup.hide_from_js()
            return ok()

        return self._guard(impl, "hide_popup")

    def set_pinned(self, flag: bool) -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            pinned = bool(flag)
            if self._s.popup is not None:
                pinned = self._s.popup.set_pinned(pinned)
            return ok(pinned=pinned)

        return self._guard(impl, "set_pinned")

    def open_settings(self) -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            if self._s.popup is not None:
                self._s.popup.note_settings_opening()
            if self._s.settings_window is None:
                return fail("server", "The settings window is not available.")
            self._s.settings_window.open()
            return ok()

        return self._guard(impl, "open_settings")

    def open_external(self, url: str) -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            target = str(url or "").strip()
            if not target.lower().startswith(("http://", "https://")):
                return fail("bad_request", "Only http(s) links can be opened.")
            if any(ch in target for ch in "\r\n\x00") or len(target) > 4096:
                return fail("bad_request", "That link cannot be opened.")
            threading.Thread(
                target=webbrowser.open, args=(target,), name="aichat-open-url", daemon=True
            ).start()
            return ok()

        return self._guard(impl, "open_external")

    # --- keys -------------------------------------------------------------------------------

    def save_api_key(self, provider_id: str, key: str) -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            spec = self._spec(str(provider_id))
            if spec.kind == "ovms":
                return fail("bad_request", "The local runtime needs no API key.")
            secrets.set_api_key(spec.id, key)  # validates, registers for redaction, stores
            status = self._key_status(spec)
            self._emit(
                {
                    "type": "key.status",
                    "provider_id": spec.id,
                    "source": status["source"],
                    "env_name": status["env_name"],
                }
            )
            return ok(key=status)

        return self._guard(impl, "save_api_key")

    def remove_api_key(self, provider_id: str) -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            spec = self._spec(str(provider_id))
            removed = secrets.delete_api_key(spec.id)
            status = self._key_status(spec)
            self._emit(
                {
                    "type": "key.status",
                    "provider_id": spec.id,
                    "source": status["source"],
                    "env_name": status["env_name"],
                }
            )
            return ok(key=status, removed=removed)

        return self._guard(impl, "remove_api_key")

    def test_provider(
        self, provider_id: str, key_or_null: str | None = None, model_or_null: str | None = None
    ) -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            from aichat.llm.probe import test_provider as probe

            spec = self._spec(str(provider_id))
            typed = str(key_or_null).strip() if key_or_null else ""
            if typed:
                key: str | None = secrets.validate_key(typed)
            else:
                key, _source = secrets.get_api_key(spec.id, spec.api_key_env)
            model = str(model_or_null).strip() if model_or_null else None
            result = self._run(probe(spec, key, model), timeout=PROBE_TIMEOUT_S)
            result = dict(result)
            result.pop("api_key", None)
            result.pop("key", None)
            return result

        return self._guard(impl, "test_provider")

    def refresh_models(self, provider_id_or_null: str | None = None) -> dict[str, Any]:
        """Re-read the model list of one remote provider, or of every configured one, and
        store it. ``{ok, results: {id: {ok, models, added, removed, error, hint, code}},
        providers}``."""

        def impl() -> dict[str, Any]:
            pid = str(provider_id_or_null).strip() if provider_id_or_null else None
            if pid is not None:
                spec = self._spec(pid)
                if spec.kind == "ovms":
                    return fail("bad_request", "Local models are managed in Settings → Models.")
            results = self._run(self._refresh_models_async(pid), timeout=PROBE_TIMEOUT_S)
            return ok(results=results, providers=self._provider_views())

        return self._guard(impl, "refresh_models")

    async def _refresh_models_async(self, provider_id: str | None = None) -> dict[str, Any]:
        """Core-loop half of :meth:`refresh_models` (also run daily by the app). One
        provider failing never stops the others."""
        from aichat.llm import model_refresh as mr

        if provider_id is not None:
            specs = [self._spec(provider_id)]
        else:
            specs = [s for s in self._cfg.providers.values() if s.kind != "ovms"]
        results: dict[str, Any] = {}
        changed = False
        for spec in specs:
            key, _source = secrets.get_api_key(spec.id, spec.api_key_env)
            if provider_id is None and not mr.is_configured(spec, key):
                continue  # "refresh all" skips providers without a key
            fetched = await mr.fetch_models(spec, key)
            entry: dict[str, Any] = {
                "ok": fetched.ok,
                "error": fetched.error,
                "hint": fetched.hint,
                "code": fetched.code,
                "models": list(spec.models),
                "added": [],
                "removed": [],
            }
            if fetched.ok:
                models, default = mr.merge_models(spec, fetched.models)
                contexts = {m: c for m, c in fetched.contexts.items() if m in models}
                entry.update(models=models, contexts=contexts)
                entry.update(mr.change_summary(list(spec.models), models))
                if (
                    models != list(spec.models)
                    or default != spec.default_model
                    or any(spec.model_context.get(m) != c for m, c in contexts.items())
                ):
                    patch = {"models": models, "default_model": default, "model_context": contexts}
                    self._apply_patch({"providers": {spec.id: patch}})
                    changed = True
            results[spec.id] = entry
        if changed:
            self._emit({"type": "settings.changed", "config": ui_config(self._cfg)})
        _log.info(
            "model lists refreshed: %s",
            ", ".join(f"{k}={'ok' if v['ok'] else v['code']}" for k, v in results.items())
            or "none",
        )
        return results

    # --- settings --------------------------------------------------------------------------

    def _settings_reply(
        self, cfg: AppConfig, restart: list[str], errors: dict[str, str]
    ) -> dict[str, Any]:
        return {
            "ok": not errors,
            "config": cfg.model_dump(mode="json"),
            "restart_required": list(restart),
            "errors": dict(errors),
        }

    def get_settings(self) -> dict[str, Any]:
        return self._guard(lambda: self._settings_reply(self._cfg, [], {}), "get_settings")

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            if not isinstance(patch, dict):
                return fail("bad_request", "The settings patch must be an object.")
            try:
                new_cfg, restart = self._apply_patch(patch)
            except ConfigError as exc:
                errors = dict(exc.details.get("errors") or {"config": exc.message})
                reply = self._settings_reply(self._cfg, [], errors)
                reply["error"] = exc.to_payload()
                return reply
            self._emit({"type": "settings.changed", "config": ui_config(new_cfg)})
            return self._settings_reply(new_cfg, restart, {})

        return self._guard(impl, "update_settings")

    def list_providers(self) -> dict[str, Any]:
        return self._guard(lambda: ok(providers=self._provider_views()), "list_providers")

    def upsert_provider(self, spec: dict[str, Any]) -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            if not isinstance(spec, dict) or not str(spec.get("id") or "").strip():
                return fail("bad_request", "A provider id is required.")
            data = {k: v for k, v in spec.items() if k not in ("key", "api_key")}
            data.setdefault("kind", "openai")
            data.setdefault("display_name", str(data["id"]))
            parsed = ProviderSpec.model_validate(data)
            if parsed.id == LOCAL_ID and parsed.kind != "ovms":
                return fail("bad_request", "The local provider cannot be replaced.")
            providers = self._s.providers
            before = self._cfg.providers
            if providers is not None:
                parsed = providers.upsert(parsed)
            try:
                self._apply_patch({"providers": {parsed.id: parsed.model_dump(mode="json")}})
            except Exception:
                if providers is not None:
                    with contextlib.suppress(Exception):
                        providers.replace_all(before)
                raise
            return ok(provider=self._provider_view(self._spec(parsed.id)))

        return self._guard(impl, "upsert_provider")

    def remove_provider(self, provider_id: str) -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            pid = str(provider_id)
            spec = self._spec(pid)
            if spec.builtin or pid == LOCAL_ID:
                return fail("bad_request", "Built-in providers cannot be removed.")
            providers = self._s.providers
            if providers is not None:
                providers.remove(pid)
            patch: dict[str, Any] = {"providers": {pid: None}}
            if self._cfg.chat.provider == pid:
                local = self._local_target_model() or self._cfg.chat.model
                patch["chat"] = {"provider": LOCAL_ID, "model": local}
            if self._cfg.chat.fallback_provider == pid:
                patch.setdefault("chat", {}).update(fallback_provider="", fallback_model="")
            new_cfg, _restart = self._apply_patch(patch)
            with contextlib.suppress(Exception):
                secrets.delete_api_key(pid)
            self._emit({"type": "settings.changed", "config": ui_config(new_cfg)})
            return ok()

        return self._guard(impl, "remove_provider")

    # --- models --------------------------------------------------------------------------

    def list_models(self) -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            registry = self._s.registry
            if registry is None:
                return ok(models=[])
            records = registry.scan()
            runtime = self._runtime_status()
            selected = self._cfg.chat.model
            loaded_id = (
                runtime.get("model_id")
                if runtime.get("state") not in ("unloaded", "not_installed")
                else None
            )
            models = []
            for rec in records:
                item = rec.to_dict()
                item["loaded"] = rec.id == loaded_id
                item["selected"] = rec.id == selected
                models.append(item)
            return ok(models=models)

        return self._guard(impl, "list_models")

    def delete_model(self, model_id: str) -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            mid = str(model_id)
            registry = self._s.registry
            if registry is None:
                return fail("server", "The model library is not available.")
            if self._model_loaded(mid):
                return fail("bad_request", "The model is loaded. Unload it first.")
            downloader = self._s.downloader
            if downloader is not None and downloader.is_downloading(mid):
                return fail("bad_request", "The model is downloading. Cancel the download first.")
            removed = registry.delete(mid)
            return ok(removed=[str(p) for p in removed])

        return self._guard(impl, "delete_model")

    def clear_compile_cache(self, model_id: str) -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            from aichat.runtime import compile_cache

            mid = str(model_id)
            if self._model_loaded(mid):
                return fail("bad_request", "The model is loaded. Unload it first.")
            freed = compile_cache.clear(self._s.paths.cache_dir, mid)
            registry = self._s.registry
            if registry is not None:
                with contextlib.suppress(Exception):
                    registry.scan()
            return ok(freed_bytes=int(freed))

        return self._guard(impl, "clear_compile_cache")

    def search_models(self, query: str, author_or_null: str | None = None) -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            hf = self._s.hf
            if hf is None:
                return fail("server", "Model search is not available.")
            author = str(author_or_null).strip() if author_or_null else None
            results = self._run(
                hf.search(str(query or ""), author=author), timeout=NETWORK_TIMEOUT_S
            )
            catalog = self._s.catalog
            if catalog is not None:
                results = catalog.annotate(results)
            return ok(results=results)

        return self._guard(impl, "search_models")

    def repo_details(self, repo_id: str) -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            from aichat.models import diskspace

            hf = self._s.hf
            if hf is None:
                return fail("server", "Model search is not available.")
            files = self._run(hf.repo_files(str(repo_id)), timeout=NETWORK_TIMEOUT_S)
            total = sum(int(f.size) for f in files)
            free = diskspace.free_bytes(self._s.paths.models_dir)
            fits = free is not None and free >= total + DOWNLOAD_HEADROOM_BYTES
            return ok(
                repo_id=str(repo_id),
                files=[f.to_dict() for f in files],
                total_bytes=total,
                fits_disk=fits,
                free_bytes=free,
            )

        return self._guard(impl, "repo_details")

    # --- downloads -----------------------------------------------------------------------

    def _downloader(self) -> Any:
        downloader = self._s.downloader
        if downloader is None:
            raise AppError("Downloads are not available.", code="server")
        return downloader

    def start_download(self, repo_id: str) -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            gid = self._run(self._downloader().enqueue(str(repo_id)), timeout=NETWORK_TIMEOUT_S)
            return ok(download_id=gid)

        return self._guard(impl, "start_download")

    def cancel_download(self, download_id: str) -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            self._run(self._downloader().cancel(str(download_id), delete_partial=True))
            return ok()

        return self._guard(impl, "cancel_download")

    def resume_download(self, download_id: str) -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            self._run(self._downloader().resume(str(download_id)))
            return ok(download_id=str(download_id))

        return self._guard(impl, "resume_download")

    def list_downloads(self) -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            downloader = self._s.downloader
            if downloader is None:
                return ok(downloads=[])
            groups = (
                self._s.loop.call(downloader.all) if self._s.loop is not None else downloader.all()
            )
            return ok(downloads=[download_view(g) for g in groups])

        return self._guard(impl, "list_downloads")

    # --- runtime ---------------------------------------------------------------------------

    def runtime_install(self) -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            from aichat.runtime import ovms_install

            s = self._s
            if s.installing:
                return ok(already_running=True)
            s.installing = True

            def progress(p: dict[str, Any]) -> None:
                self._emit({"type": "runtime.install", **runtime_install_view(p)})

            async def _install() -> None:
                try:
                    await ovms_install.install(s.paths, s.config.local, progress, asyncio.Event())
                except Exception as exc:  # noqa: BLE001
                    _log.warning("runtime install failed: %s: %s", type(exc).__name__, exc)
                    self._emit(
                        {
                            "type": "runtime.install",
                            **runtime_install_view({"status": "error", "error": str(exc)[:300]}),
                        }
                    )
                finally:
                    s.installing = False

            self._submit(_install(), "runtime-install")
            return ok()

        return self._guard(impl, "runtime_install")

    def runtime_status(self) -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            from aichat.runtime import ovms_install

            return ok(**ovms_install.runtime_status(self._s.paths, self._cfg.local))

        return self._guard(impl, "runtime_status")

    # --- logs and misc -----------------------------------------------------------------------

    def get_logs(self, n: int = 200, level: str | None = "INFO") -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            count = max(1, min(int(n or 200), 5000))
            wanted = str(level or "INFO").upper()
            if wanted not in _LOG_ORDER:
                wanted = "INFO"
            records = RING_BUFFER.tail(count, wanted)
            return ok(lines=[format_log_line(r) for r in records])

        return self._guard(impl, "get_logs")

    def open_logs_folder(self) -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            from aichat.desktop import win32util

            logs = self._s.paths.logs_dir
            logs.mkdir(parents=True, exist_ok=True)
            win32util.open_path(logs)
            return ok(path=str(logs))

        return self._guard(impl, "open_logs_folder")

    def get_autostart(self) -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            st = autostart.status()
            return ok(
                enabled=st.enabled,
                mode=st.mode,  # "task-scheduler" | "startup-folder"
                mechanism=st.mechanism,
                path=str(st.path) if st.path else None,
                detail=st.detail,
                duplicate=str(st.duplicate) if st.duplicate else None,
                description=st.describe(),
            )

        return self._guard(impl, "get_autostart")

    def set_autostart(self, flag: bool) -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            want = bool(flag)
            home = self._s.paths.home
            st = autostart.enable(home=home) if want else autostart.disable(home=home)
            with contextlib.suppress(ConfigError):
                self._apply_patch({"startup": {"autostart": want}})
            return ok(
                enabled=st.enabled,
                mode=st.mode,
                mechanism=st.mechanism,
                path=str(st.path) if st.path else None,
                detail=st.detail,
                description=st.describe(),
            )

        return self._guard(impl, "set_autostart")

    def set_hotkey(self, spec: str) -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            from aichat.desktop.hotkey import canonical

            try:
                text = canonical(str(spec or ""))
            except ValueError as exc:
                return fail("bad_request", str(exc))
            hotkey = self._s.hotkey
            if hotkey is not None:
                error = hotkey.register(text)
                if error:
                    code = "conflict" if "in use" in error else "bad_request"
                    return fail(code, error)
            new_cfg, _restart = self._apply_patch({"ui": {"hotkey": text}})
            self._emit({"type": "settings.changed", "config": ui_config(new_cfg)})
            return ok(hotkey=text)

        return self._guard(impl, "set_hotkey")

    def disk_usage(self) -> dict[str, Any]:
        def impl() -> dict[str, Any]:
            from aichat.models import diskspace

            p = self._s.paths
            return ok(
                models=diskspace.folder_size(p.models_dir),
                cache=diskspace.folder_size(p.cache_dir),
                runtime=diskspace.folder_size(p.runtime_dir),
                logs=diskspace.folder_size(p.logs_dir),
                free=diskspace.free_bytes(p.home),
            )

        return self._guard(impl, "disk_usage")
