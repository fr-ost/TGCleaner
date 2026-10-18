"""Local JSON API for the Chrome extension.

Security model (local, the default):
  * Listens on 127.0.0.1 only.
  * Host header must be 127.0.0.1:<port> or localhost:<port> (blocks DNS rebinding).
  * Every /api request must come from the extension's own origin
    (chrome-extension://<id>) and carry the X-TGCleaner header. Web pages cannot
    forge Origin, and a custom header forces a CORS preflight that we refuse for
    any other origin, so other websites can neither read nor trigger anything.
  * Request bodies are small, JSON only, and strictly validated.
  * Actions (leave/remove, archive, mute) are two-step: /prepare validates the
    selection and returns the exact names plus a single-use token; /start consumes
    that token. The read-only engagement scan needs no token. Only one job (of any
    kind) runs at a time, which also keeps the request rate low.
  * Request bodies are never logged (they may contain login codes or passwords).

Cloud mode (serve --cloud, e.g. on Railway) is reachable from the internet, where
Origin can be forged by any non-browser client, and the Host check is dropped. If
TGC_ACCESS_TOKEN is set, every /api request must carry "Authorization: Bearer <token>"
(constant-time compared; repeated failures lock the API for a minute). Without it,
anyone who knows the URL can use the service.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import secrets
import time
import os
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from .config import Config
from .jobs import BatchJob, ItemResult
from .models import ACTIONS, Backend, ServiceError, ineligibility, iso

log = logging.getLogger("tgcleaner.server")

MAX_BODY = 64 * 1024
MAX_SELECTION = 5000
PREPARE_TTL = 600  # seconds a confirmation token stays valid

_ID_RE = re.compile(r"^-?[1-9][0-9]{0,19}$")
_PHONE_RE = re.compile(r"^\+?[0-9][0-9 ()\-.]{4,24}$")
_CODE_RE = re.compile(r"^[0-9]{3,10}$")

BACKEND = web.AppKey("backend", object)
STATE = web.AppKey("state", object)


@dataclass
class Prepared:
    action: str
    ids: list[str]
    expires: float


class State:
    def __init__(self, cfg: Config, demo: bool):
        self.cfg = cfg
        self.demo = demo
        self.prepared: dict[str, Prepared] = {}
        self.job: BatchJob | None = None
        self.job_task: asyncio.Task | None = None
        self.dialogs_loaded = False
        self.last_list = None
        self.retry_after: dict[str, float] = {}  # job kind -> time Telegram allows that kind again
        self.start_lock = asyncio.Lock()


def _error(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": code, "message": message}, status=status)


def _cors_headers(origin: str) -> dict:
    return {
        "Access-Control-Allow-Origin": origin,
        "Vary": "Origin",
        "Cache-Control": "no-store",
    }


AUTH_FAILURE_LIMIT = 20     # wrong tokens allowed per window ...
AUTH_FAILURE_WINDOW = 60.0  # ... seconds, before the API locks for a window


def make_security_middleware(cfg: Config, access_token: str | None = None, cloud: bool = False):
    allowed_hosts = {f"127.0.0.1:{cfg.port}", f"localhost:{cfg.port}"}
    allowed_origins = cfg.allowed_origins
    expected = f"Bearer {access_token}".encode() if access_token else None
    failures: deque[float] = deque()

    def locked(now: float) -> bool:
        while failures and failures[0] < now - AUTH_FAILURE_WINDOW:
            failures.popleft()
        return len(failures) >= AUTH_FAILURE_LIMIT

    @web.middleware
    async def security(request: web.Request, handler):
        if not cloud and request.host not in allowed_hosts:
            return _error(403, "bad_host", "Requests must address 127.0.0.1 directly.")
        if request.path == "/health":
            # Lets the start script / you check the service is up. Reveals nothing.
            return web.json_response({"ok": True, "app": "tgcleaner"})
        if not request.path.startswith("/api/"):
            return await handler(request)  # the web dashboard's static files
        origin = request.headers.get("Origin")
        # The dashboard served by this service itself is same-origin: browsers send either our own
        # origin or (for same-origin GETs) no Origin plus "Sec-Fetch-Site: same-origin".
        same_origin = (urlsplit(origin).netloc == request.host) if origin else (
            request.headers.get("Sec-Fetch-Site") == "same-origin")
        if origin not in allowed_origins and not same_origin:
            log.warning("rejected request from origin %r to %s", origin, request.path)
            return _error(403, "origin_not_allowed",
                          "Only the TG Cleaner extension may use this service.")
        if request.method == "OPTIONS":
            method = request.headers.get("Access-Control-Request-Method", "")
            if method not in ("GET", "POST"):
                return _error(405, "method_not_allowed", "Method not allowed.")
            headers = _cors_headers(origin) | {
                "Access-Control-Allow-Methods": "GET, POST",
                "Access-Control-Allow-Headers": "Content-Type, X-TGCleaner, Authorization",
                "Access-Control-Max-Age": "600",
            }
            if request.headers.get("Access-Control-Request-Private-Network") == "true":
                headers["Access-Control-Allow-Private-Network"] = "true"
            return web.Response(status=204, headers=headers)
        if request.headers.get("X-TGCleaner") != "1":
            return _error(403, "missing_header", "Missing X-TGCleaner header.")
        if expected is not None:
            now = time.monotonic()
            if locked(now):
                return _cors(_error(429, "auth_locked", "Too many wrong access tokens. Try again in a minute."), origin)
            given = request.headers.get("Authorization", "").encode()
            if not hmac.compare_digest(given, expected):
                failures.append(now)
                log.warning("rejected request with a missing or wrong access token")
                return _cors(_error(401, "bad_access_token",
                                    "The access token is missing or wrong. Set it in the extension's connection settings."), origin)
        if request.method == "POST" and request.content_type != "application/json":
            return _error(415, "json_required", "Send JSON.")
        try:
            response = await handler(request)
        except ServiceError as e:
            response = _error(e.status, e.code, e.message)
        except web.HTTPException as e:
            response = _error(e.status, "http_error", e.reason)
        except Exception:
            log.exception("unhandled error on %s", request.path)
            response = _error(500, "internal_error", "Unexpected error. See the service window for details.")
        return _cors(response, origin)

    return security


def _cors(response: web.Response, origin: str | None) -> web.Response:
    if origin:
        response.headers.update(_cors_headers(origin))
    else:
        response.headers["Cache-Control"] = "no-store"
    return response


async def _json_body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ServiceError("bad_json", "Body is not valid JSON.") from None
    if not isinstance(body, dict):
        raise ServiceError("bad_json", "Body must be a JSON object.")
    return body


def _string_field(body: dict, name: str, max_len: int) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > max_len:
        raise ServiceError("bad_request", f"'{name}' is missing or invalid.")
    return value


def _backend(request) -> Backend:
    return request.app[BACKEND]


def _state(request) -> State:
    return request.app[STATE]


# -- handlers -----------------------------------------------------------------

async def status(request):
    st = _state(request)
    info = await _backend(request).status()
    return web.json_response(info | {"demo": st.demo, "job_active": bool(st.job and st.job.active)})


async def login_phone(request):
    phone = _string_field(await _json_body(request), "phone", 32).strip()
    if not _PHONE_RE.match(phone):
        raise ServiceError("phone_invalid", "Enter your phone number in international format, e.g. +15551234567.")
    return web.json_response(await _backend(request).send_code(phone))


async def login_code(request):
    code = re.sub(r"[\s\-]", "", _string_field(await _json_body(request), "code", 32))
    if not _CODE_RE.match(code):
        raise ServiceError("code_invalid", "The code is the 5-6 digit number Telegram sent you.")
    return web.json_response(await _backend(request).submit_code(code))


async def login_password(request):
    password = _string_field(await _json_body(request), "password", 256)
    return web.json_response(await _backend(request).submit_password(password))


async def login_cancel(request):
    await _backend(request).cancel_login()
    return web.json_response({"ok": True})


async def logout(request):
    st = _state(request)
    if st.job and st.job.active:
        raise ServiceError("job_active", "Wait for the running job to finish (or cancel it) before disconnecting.", 409)
    result = await _backend(request).log_out()
    st.prepared.clear()
    st.job = None
    st.dialogs_loaded = False
    st.last_list = None
    return web.json_response({"ok": True} | result)


async def dialogs(request):
    st = _state(request)
    want_cached = request.query.get("cached") == "1"
    if (st.job and st.job.active) or (want_cached and st.last_list is not None):
        # No refetch mid-job (or when asked): hand back the list the jobs work on; it reflects their results.
        if st.last_list is None:
            raise ServiceError("job_active", "The list can't be reloaded while a job is running.", 409)
        result, cached = st.last_list, True
    else:
        result, cached = await _backend(request).load_dialogs(), False
        st.last_list = result
        st.dialogs_loaded = True
        st.prepared.clear()  # a token always refers to the list it was made from
    return web.json_response({
        "items": [it.to_json() for it in result.items],
        "excluded": result.excluded,
        "loaded_at": iso(result.loaded_at),
        "cached": cached,
    })


def _validate_ids(raw) -> list[str]:
    if not isinstance(raw, list) or not raw:
        raise ServiceError("bad_request", "Select at least one item.")
    if len(raw) > MAX_SELECTION:
        raise ServiceError("bad_request", f"At most {MAX_SELECTION} items per request.")
    ids, seen = [], set()
    for value in raw:
        if not isinstance(value, str) or not _ID_RE.match(value):
            raise ServiceError("bad_request", "Item ids must be numeric strings.")
        if value not in seen:
            seen.add(value)
            ids.append(value)
    return ids


def _check_can_start(st: State, kind: str) -> None:
    if st.job and st.job.active:
        raise ServiceError("job_active", "Another job is running. Wait for it or cancel it.", 409)
    until = st.retry_after.get(kind, 0)
    if until > time.time():
        wait = int(until - time.time()) + 1
        raise ServiceError("rate_limited", f"Telegram asked to pause this. Try again in {wait} s.", 429)
    if not st.dialogs_loaded:
        raise ServiceError("no_list", "Load the list first.", 409)


def _launch(st: State, kind: str, items, operation) -> BatchJob:
    delay = st.cfg.scan_delay_seconds if kind == "scan" else st.cfg.leave_delay_seconds
    job = BatchJob(kind=kind, items=[ItemResult(id=it.id, title=it.title, kind=it.kind) for it in items],
                   delay=delay, max_auto_wait=st.cfg.max_auto_flood_wait)
    st.job = job

    async def run():
        await job.run(operation)
        if job.retry_after:
            st.retry_after[kind] = job.retry_after

    st.job_task = asyncio.create_task(run())
    log.info("%s job %s started with %d items", kind, job.id, len(items))
    return job


async def actions_prepare(request):
    st = _state(request)
    backend = _backend(request)
    body = await _json_body(request)
    action = body.get("action")
    if action not in ACTIONS:
        raise ServiceError("bad_request", f"'action' must be one of: {', '.join(ACTIONS)}.")
    ids = _validate_ids(body.get("ids"))
    _check_can_start(st, action)

    accepted, rejected = [], []
    for item_id in ids:
        item = backend.cached_item(item_id)
        reason = "Not in the current list. Reload the list." if item is None else ineligibility(item, action)
        if reason:
            rejected.append({"id": item_id, "title": item.title if item else None, "reason": reason})
        else:
            accepted.append(item)

    now = time.time()
    st.prepared = {k: v for k, v in st.prepared.items() if v.expires > now}
    token = None
    if accepted:
        token = secrets.token_urlsafe(24)
        st.prepared[token] = Prepared(action=action, ids=[it.id for it in accepted], expires=now + PREPARE_TTL)
    return web.json_response({
        "action": action,
        "token": token,
        "expires_in": PREPARE_TTL,
        "items": [{"id": it.id, "title": it.title, "kind": it.kind} for it in accepted],
        "rejected": rejected,
    })


async def actions_start(request):
    st = _state(request)
    backend = _backend(request)
    body = await _json_body(request)
    token = _string_field(body, "token", 100)
    options = body.get("options") or {}
    if not isinstance(options, dict) or not isinstance(options.get("block_bots", False), bool):
        raise ServiceError("bad_request", "'options' is invalid.")
    options = {"block_bots": options.get("block_bots", False)}
    async with st.start_lock:  # two clicks can't start two jobs
        if st.job and st.job.active:
            raise ServiceError("job_active", "Another job is running.", 409)
        prepared = st.prepared.pop(token, None)  # single use
        if prepared is None or prepared.expires < time.time():
            raise ServiceError("bad_token", "This confirmation expired or was already used. Select again and confirm.", 409)
        items = [backend.cached_item(i) for i in prepared.ids]
        # Re-check: things may have changed since prepare.
        items = [it for it in items if it is not None and ineligibility(it, prepared.action) is None]
        if not items:
            raise ServiceError("nothing_to_do", "None of the confirmed items qualify any more.", 409)

        async def operation(item_id):
            return await backend.perform(prepared.action, item_id, options)

        job = _launch(st, prepared.action, items, operation)
    return web.json_response(job.to_json())


async def scan_start(request):
    """Read-only: fill in when you last read / posted. Scans the given ids, or everything that needs it."""
    st = _state(request)
    backend = _backend(request)
    body = await _json_body(request)
    async with st.start_lock:
        _check_can_start(st, "scan")
        if body.get("ids") is not None:
            items = [backend.cached_item(i) for i in _validate_ids(body["ids"])]
            items = [it for it in items if it is not None and it.accessible]
        else:
            items = [it for it in st.last_list.items if it.accessible and _needs_scan(it)]
        if not items:
            raise ServiceError("nothing_to_do", "Nothing needs scanning: your activity is already known for every chat.", 409)
        job = _launch(st, "scan", items, backend.scan)
    return web.json_response(job.to_json())


def _needs_scan(item) -> bool:
    if item.kind == "other" or item.scanned_at is not None:
        return False
    return item.last_read_at is None or (item.can_post and item.my_last_message_at is None)


async def protect(request):
    """Mark or unmark chats as protected. Protected chats are refused by every action."""
    body = await _json_body(request)
    value = body.get("protected")
    if not isinstance(value, bool):
        raise ServiceError("bad_request", "'protected' must be true or false.")
    if body.get("all") is True:
        # "Unprotect all": clears the whole saved list, including chats not in the current list.
        if value:
            raise ServiceError("bad_request", "Protecting everything at once isn't supported; select chats instead.")
        return web.json_response({"ok": True, "protected": False, "ids": _backend(request).clear_protected()})
    ids = _validate_ids(body.get("ids"))
    found = _backend(request).set_protected(ids, value)
    return web.json_response({"ok": True, "protected": value, "ids": found})


async def job_status(request):
    job = _state(request).job
    return web.json_response({"job": job.to_json() if job else None})


async def job_cancel(request):
    job = _state(request).job
    if not job or not job.active:
        raise ServiceError("no_job", "No job is running.", 409)
    job.cancel()
    return web.json_response(job.to_json())


# The web dashboard: the same files as the Chrome extension, served at "/" (e.g. on Railway).
WEB_FILES = {
    "": ("dashboard.html", "text/html"),
    "dashboard.html": ("dashboard.html", "text/html"),
    "dashboard.css": ("dashboard.css", "text/css"),
    "dashboard.js": ("dashboard.js", "text/javascript"),
    "api.js": ("api.js", "text/javascript"),
    "logic.js": ("logic.js", "text/javascript"),
    "icons/icon16.png": ("icons/icon16.png", "image/png"),
    "icons/icon32.png": ("icons/icon32.png", "image/png"),
    "icons/icon48.png": ("icons/icon48.png", "image/png"),
    "icons/icon128.png": ("icons/icon128.png", "image/png"),
}
WEB_HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
                                "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache",
}


def web_dir() -> Path | None:
    configured = os.environ.get("TGC_WEB_DIR")
    path = Path(configured) if configured else Path(__file__).resolve().parents[2] / "extension"
    return path if (path / "dashboard.html").is_file() else None


async def web_file(request):
    """Serve one of a fixed set of dashboard files (no directory listing, no path traversal)."""
    root = web_dir()
    entry = WEB_FILES.get(request.match_info["path"])
    if root is None or entry is None:
        raise web.HTTPNotFound()
    name, content_type = entry
    return web.Response(body=(root / name).read_bytes(), content_type=content_type,
                        charset="utf-8" if content_type.startswith("text/") else None, headers=WEB_HEADERS)


def create_app(cfg: Config, backend: Backend, *, demo: bool = False,
               access_token: str | None = None, cloud: bool = False) -> web.Application:
    middleware = make_security_middleware(cfg, access_token, cloud=cloud or access_token is not None)
    app = web.Application(middlewares=[middleware], client_max_size=MAX_BODY)
    app[BACKEND] = backend
    app[STATE] = State(cfg, demo)
    app.router.add_get("/health", lambda r: web.json_response({"ok": True}))
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/login/phone", login_phone)
    app.router.add_post("/api/login/code", login_code)
    app.router.add_post("/api/login/password", login_password)
    app.router.add_post("/api/login/cancel", login_cancel)
    app.router.add_post("/api/logout", logout)
    app.router.add_get("/api/dialogs", dialogs)
    app.router.add_post("/api/actions/prepare", actions_prepare)
    app.router.add_post("/api/actions/start", actions_start)
    app.router.add_post("/api/scan/start", scan_start)
    app.router.add_post("/api/protect", protect)
    app.router.add_get("/api/job", job_status)
    app.router.add_post("/api/job/cancel", job_cancel)
    app.router.add_get("/{path:(|dashboard\\.html|dashboard\\.css|dashboard\\.js|api\\.js|logic\\.js|icons/icon\\d+\\.png)}", web_file)
    # Preflight requests are answered by the middleware before routing.
    app.router.add_route("OPTIONS", "/{tail:.*}", lambda r: web.Response(status=204))

    async def on_startup(app):
        await backend.start()

    async def on_cleanup(app):
        st = app[STATE]
        if st.job and st.job.active:
            st.job.cancel()
            if st.job_task:
                await asyncio.wait([st.job_task], timeout=30)
        await backend.stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def run(cfg: Config, backend: Backend, *, demo: bool = False, access_token: str | None = None,
        cloud: bool = False) -> None:
    app = create_app(cfg, backend, demo=demo, access_token=access_token, cloud=cloud)
    demo_note = "  [DEMO MODE: no Telegram connection]" if demo else ""
    if cloud:
        if not access_token:
            print("WARNING: TGC_ACCESS_TOKEN is not set. Anyone who knows this service's URL can use your "
                  "logged-in Telegram session. Delete the deployment when you're done.", flush=True)
        # Cloud mode: reachable from outside (behind the platform's HTTPS proxy), token required.
        print(f"TG Cleaner service (cloud mode) listening on 0.0.0.0:{cfg.port}{demo_note}", flush=True)
        print("Open the service's public URL in a browser to use the dashboard.", flush=True)
        web.run_app(app, host="0.0.0.0", port=cfg.port, access_log=None, print=None)
        return
    print(f"TG Cleaner service on http://127.0.0.1:{cfg.port}{demo_note}")
    print(f"Dashboard: open http://127.0.0.1:{cfg.port} in your browser (or use the Chrome extension).")
    print("Keep this window open while you use the extension. Press Ctrl+C to stop.")
    web.run_app(app, host="127.0.0.1", port=cfg.port, access_log=None, print=None)
