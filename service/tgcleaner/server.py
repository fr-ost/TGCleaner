"""Local JSON API for the Chrome extension.

Security model:
  * Listens on 127.0.0.1 only.
  * Host header must be 127.0.0.1:<port> or localhost:<port> (blocks DNS rebinding).
  * Every /api request must come from the extension's own origin
    (chrome-extension://<id>) and carry the X-TGCleaner header. Web pages cannot
    forge Origin, and a custom header forces a CORS preflight that we refuse for
    any other origin, so other websites can neither read nor trigger anything.
  * Request bodies are small, JSON only, and strictly validated.
  * Leaving is two-step: /prepare validates the selection and returns the exact
    names plus a single-use token; /start consumes that token. Only one leave
    job can run at a time.
  * Request bodies are never logged (they may contain login codes or passwords).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass

from aiohttp import web

from .config import Config
from .leave_job import DONE_STATUSES, ItemResult, LeaveJob
from .models import Backend, ServiceError, iso

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
    ids: list[str]
    expires: float


class State:
    def __init__(self, cfg: Config, demo: bool):
        self.cfg = cfg
        self.demo = demo
        self.prepared: dict[str, Prepared] = {}
        self.job: LeaveJob | None = None
        self.job_task: asyncio.Task | None = None
        self.dialogs_loaded = False
        self.last_list = None
        self.start_lock = asyncio.Lock()


def _error(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": code, "message": message}, status=status)


def _cors_headers(origin: str) -> dict:
    return {
        "Access-Control-Allow-Origin": origin,
        "Vary": "Origin",
        "Cache-Control": "no-store",
    }


def make_security_middleware(cfg: Config):
    allowed_hosts = {f"127.0.0.1:{cfg.port}", f"localhost:{cfg.port}"}
    allowed_origins = cfg.allowed_origins

    @web.middleware
    async def security(request: web.Request, handler):
        if request.host not in allowed_hosts:
            return _error(403, "bad_host", "Requests must address 127.0.0.1 directly.")
        if request.path == "/health":
            # Lets the start script / you check the service is up. Reveals nothing.
            return web.json_response({"ok": True, "app": "tgcleaner"})
        origin = request.headers.get("Origin")
        if origin not in allowed_origins:
            log.warning("rejected request from origin %r to %s", origin, request.path)
            return _error(403, "origin_not_allowed",
                          "Only the TG Group Cleaner extension may use this service.")
        if request.method == "OPTIONS":
            method = request.headers.get("Access-Control-Request-Method", "")
            if method not in ("GET", "POST"):
                return _error(405, "method_not_allowed", "Method not allowed.")
            headers = _cors_headers(origin) | {
                "Access-Control-Allow-Methods": "GET, POST",
                "Access-Control-Allow-Headers": "Content-Type, X-TGCleaner",
                "Access-Control-Max-Age": "600",
            }
            if request.headers.get("Access-Control-Request-Private-Network") == "true":
                headers["Access-Control-Allow-Private-Network"] = "true"
            return web.Response(status=204, headers=headers)
        if request.headers.get("X-TGCleaner") != "1":
            return _error(403, "missing_header", "Missing X-TGCleaner header.")
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
        response.headers.update(_cors_headers(origin))
        return response

    return security


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
        raise ServiceError("job_active", "Wait for the leave job to finish (or cancel it) before disconnecting.", 409)
    result = await _backend(request).log_out()
    st.prepared.clear()
    st.job = None
    st.dialogs_loaded = False
    st.last_list = None
    return web.json_response({"ok": True} | result)


async def dialogs(request):
    st = _state(request)
    if st.job and st.job.active:
        # Don't refetch mid-job; hand back the list the job works from (it reflects confirmed leaves).
        if st.last_list is None:
            raise ServiceError("job_active", "The list can't be reloaded while leaving is in progress.", 409)
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


async def leave_prepare(request):
    st = _state(request)
    backend = _backend(request)
    ids = _validate_ids((await _json_body(request)).get("ids"))
    if st.job and st.job.active:
        raise ServiceError("job_active", "A leave job is already running.", 409)
    if st.job and st.job.retry_after and st.job.retry_after > time.time():
        wait = int(st.job.retry_after - time.time()) + 1
        raise ServiceError("rate_limited", f"Telegram asked to pause leaving. Try again in {wait} s.", 429)
    if not st.dialogs_loaded:
        raise ServiceError("no_list", "Load the list first.", 409)

    accepted, rejected = [], []
    for item_id in ids:
        item = backend.cached_item(item_id)
        if item is None:
            rejected.append({"id": item_id, "title": None, "reason": "Not in the current list. Reload the list."})
        elif not item.leavable:
            rejected.append({"id": item_id, "title": item.title, "reason": item.reason})
        else:
            accepted.append(item)

    now = time.time()
    st.prepared = {k: v for k, v in st.prepared.items() if v.expires > now}
    token = None
    if accepted:
        token = secrets.token_urlsafe(24)
        st.prepared[token] = Prepared(ids=[it.id for it in accepted], expires=now + PREPARE_TTL)
    return web.json_response({
        "token": token,
        "expires_in": PREPARE_TTL,
        "items": [{"id": it.id, "title": it.title, "kind": it.kind} for it in accepted],
        "rejected": rejected,
    })


async def leave_start(request):
    st = _state(request)
    backend = _backend(request)
    token = _string_field(await _json_body(request), "token", 100)
    async with st.start_lock:  # two clicks can't start two jobs
        if st.job and st.job.active:
            raise ServiceError("job_active", "A leave job is already running.", 409)
        prepared = st.prepared.pop(token, None)  # single use
        if prepared is None or prepared.expires < time.time():
            raise ServiceError("bad_token", "This confirmation expired or was already used. Select again and confirm.", 409)
        results = []
        for item_id in prepared.ids:
            item = backend.cached_item(item_id)
            if item is not None and item.leavable:  # re-check: may have changed since prepare
                results.append(ItemResult(id=item.id, title=item.title, kind=item.kind))
        if not results:
            raise ServiceError("nothing_to_leave", "None of the confirmed items can be left any more.", 409)
        job = LeaveJob(items=results, delay=st.cfg.leave_delay_seconds, max_auto_wait=st.cfg.max_auto_flood_wait)
        st.job = job
        st.job_task = asyncio.create_task(job.run(backend))
        log.info("leave job %s started with %d items", job.id, len(results))
    return web.json_response(job.to_json())


async def leave_job(request):
    job = _state(request).job
    return web.json_response({"job": job.to_json() if job else None})


async def leave_cancel(request):
    job = _state(request).job
    if not job or not job.active:
        raise ServiceError("no_job", "No leave job is running.", 409)
    job.cancel()
    return web.json_response(job.to_json())


def create_app(cfg: Config, backend: Backend, *, demo: bool = False) -> web.Application:
    app = web.Application(middlewares=[make_security_middleware(cfg)], client_max_size=MAX_BODY)
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
    app.router.add_post("/api/leave/prepare", leave_prepare)
    app.router.add_post("/api/leave/start", leave_start)
    app.router.add_get("/api/leave/job", leave_job)
    app.router.add_post("/api/leave/cancel", leave_cancel)
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


def run(cfg: Config, backend: Backend, *, demo: bool = False) -> None:
    app = create_app(cfg, backend, demo=demo)
    print(f"TG Group Cleaner service on http://127.0.0.1:{cfg.port}"
          f"{'  [DEMO MODE: no Telegram connection]' if demo else ''}")
    print("Keep this window open while you use the extension. Press Ctrl+C to stop.")
    web.run_app(app, host="127.0.0.1", port=cfg.port, access_log=None, print=None)
