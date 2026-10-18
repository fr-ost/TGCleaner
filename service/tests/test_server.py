"""HTTP API: origin/host checks, validation, login, and full bulk-leave flows (FakeBackend, no Telegram)."""

import asyncio
import socket
import unittest

from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from tgcleaner.config import DEFAULT_EXTENSION_ID, Config
from tgcleaner.fake_backend import DEMO_CODE, DEMO_PASSWORD, FakeBackend, demo_items
from tgcleaner.server import create_app

ORIGIN = f"chrome-extension://{DEFAULT_EXTENSION_ID}"
HEADERS = {"Origin": ORIGIN, "X-TGCleaner": "1"}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ApiTestCase(unittest.IsolatedAsyncioTestCase):
    authorized = True

    async def asyncSetUp(self):
        self.port = free_port()
        self.cfg = Config(api_id=1, api_hash="0" * 32, port=self.port)
        self.cfg.leave_delay_seconds = 0  # tests only; real config enforces >= 0.5 s
        self.items = demo_items(120, seed=3)
        self.backend = FakeBackend(self.items, authorized=self.authorized, require_password=True)
        self.server = TestServer(create_app(self.cfg, self.backend, demo=True), host="127.0.0.1", port=self.port)
        await self.server.start_server()
        self.http = ClientSession()
        self.base = f"http://127.0.0.1:{self.port}"

    async def asyncTearDown(self):
        await self.http.close()
        await self.server.close()

    async def call(self, method, path, json=None, headers=HEADERS, **kw):
        async with self.http.request(method, self.base + path, json=json, headers=headers, **kw) as r:
            body = await r.json() if r.content_type == "application/json" else None
            return r.status, body, r.headers

    async def get(self, path, **kw):
        return await self.call("GET", path, **kw)

    async def post(self, path, json=None, **kw):
        return await self.call("POST", path, json=json if json is not None else {}, **kw)

    async def wait_job(self, timeout=20):
        for _ in range(int(timeout / 0.05)):
            _, body, _ = await self.get("/api/leave/job")
            if body["job"] and body["job"]["state"] not in ("running", "waiting"):
                return body["job"]
            await asyncio.sleep(0.05)
        self.fail("job did not finish")


class SecurityTests(ApiTestCase):
    async def test_health_needs_no_origin(self):
        status, body, _ = await self.get("/health", headers={})
        self.assertEqual((status, body["ok"]), (200, True))

    async def test_requests_without_or_with_foreign_origin_are_rejected(self):
        for headers in ({}, {"X-TGCleaner": "1"},
                        {"Origin": "https://evil.example", "X-TGCleaner": "1"},
                        {"Origin": "chrome-extension://" + "a" * 32, "X-TGCleaner": "1"},
                        {"Origin": "null", "X-TGCleaner": "1"}):
            status, body, resp_headers = await self.get("/api/status", headers=headers)
            self.assertEqual(status, 403, headers)
            self.assertEqual(body["error"], "origin_not_allowed")
            self.assertNotIn("Access-Control-Allow-Origin", resp_headers)

    async def test_custom_header_is_required(self):
        status, body, _ = await self.get("/api/status", headers={"Origin": ORIGIN})
        self.assertEqual((status, body["error"]), (403, "missing_header"))

    async def test_dns_rebinding_host_is_rejected(self):
        status, body, _ = await self.get("/api/status", headers=HEADERS | {"Host": f"evil.example:{self.port}"})
        self.assertEqual((status, body["error"]), (403, "bad_host"))

    async def test_preflight(self):
        ok = await self.call("OPTIONS", "/api/leave/prepare", headers={
            "Origin": ORIGIN, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-tgcleaner",
            "Access-Control-Request-Private-Network": "true"})
        self.assertEqual(ok[0], 204)
        self.assertEqual(ok[2]["Access-Control-Allow-Origin"], ORIGIN)
        self.assertEqual(ok[2]["Access-Control-Allow-Private-Network"], "true")
        evil = await self.call("OPTIONS", "/api/leave/prepare", headers={
            "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
        self.assertEqual(evil[0], 403)
        self.assertNotIn("Access-Control-Allow-Origin", evil[2])

    async def test_json_content_type_and_size_limits(self):
        status, _, _ = await self.call("POST", "/api/login/phone", data="phone=1",
                                       headers=HEADERS | {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 415)
        status, _, _ = await self.post("/api/leave/prepare", json={"ids": ["-1"] * 20000})
        self.assertEqual(status, 413)

    async def test_responses_carry_cors_for_extension_and_no_store(self):
        status, _, headers = await self.get("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Access-Control-Allow-Origin"], ORIGIN)
        self.assertEqual(headers["Cache-Control"], "no-store")


class LoginTests(ApiTestCase):
    authorized = False

    async def test_login_with_two_step_and_logout(self):
        status, body, _ = await self.get("/api/status")
        self.assertEqual((body["authorized"], body["demo"]), (False, True))
        status, _, _ = await self.get("/api/dialogs")
        self.assertEqual(status, 401)

        status, body, _ = await self.post("/api/login/phone", {"phone": "not a phone"})
        self.assertEqual((status, body["error"]), (400, "phone_invalid"))
        status, body, _ = await self.post("/api/login/phone", {"phone": "+1 555 0100"})
        self.assertEqual(body["step"], "code")

        status, body, _ = await self.post("/api/login/code", {"code": "abc"})
        self.assertEqual(body["error"], "code_invalid")
        status, body, _ = await self.post("/api/login/code", {"code": "00000"})
        self.assertEqual(body["error"], "code_invalid")
        status, body, _ = await self.post("/api/login/code", {"code": DEMO_CODE})
        self.assertEqual(body["step"], "password")
        status, body, _ = await self.post("/api/login/password", {"password": "wrong"})
        self.assertEqual(body["error"], "password_invalid")
        status, body, _ = await self.post("/api/login/password", {"password": DEMO_PASSWORD})
        self.assertEqual(body["step"], "done")

        _, body, _ = await self.get("/api/status")
        self.assertTrue(body["authorized"])
        self.assertEqual(body["account"]["name"], "Demo User")

        status, body, _ = await self.post("/api/logout")
        self.assertEqual((status, body["ok"]), (200, True))
        _, body, _ = await self.get("/api/status")
        self.assertFalse(body["authorized"])

    async def test_bad_field_types(self):
        for payload in ({}, {"phone": 5}, {"phone": ""}, {"phone": "+1" * 40}):
            status, body, _ = await self.post("/api/login/phone", payload)
            self.assertEqual(status, 400, payload)
        status, body, _ = await self.call("POST", "/api/login/phone", data="[1,2]",
                                          headers=HEADERS | {"Content-Type": "application/json"})
        self.assertEqual((status, body["error"]), (400, "bad_json"))


class LeaveFlowTests(ApiTestCase):
    async def load(self):
        status, body, _ = await self.get("/api/dialogs")
        self.assertEqual(status, 200)
        return body

    async def test_dialog_list_shape(self):
        body = await self.load()
        self.assertEqual(len(body["items"]), 120)
        self.assertEqual(body["excluded"]["saved_messages"], 1)
        item = body["items"][0]
        for key in ("id", "title", "kind", "username", "link", "last_message_at", "role", "leavable", "reason"):
            self.assertIn(key, item)
        self.assertTrue(any(it["last_message_at"] is None for it in body["items"]))

    async def test_prepare_requires_loaded_list_and_valid_ids(self):
        status, body, _ = await self.post("/api/leave/prepare", {"ids": ["-1001000000001"]})
        self.assertEqual((status, body["error"]), (409, "no_list"))
        await self.load()
        for bad in ([], "x", [123], ["abc"], ["--1"], ["1;drop"], None):
            status, body, _ = await self.post("/api/leave/prepare", {"ids": bad})
            self.assertEqual(status, 400, bad)

    async def test_prepare_separates_ineligible_items_and_dedupes(self):
        body = await self.load()
        leavable = [it["id"] for it in body["items"] if it["leavable"]][:3]
        owned = next(it for it in body["items"] if it["role"] == "owner")
        status, prep, _ = await self.post("/api/leave/prepare",
                                          {"ids": leavable + [leavable[0], owned["id"], "-1009999999999"]})
        self.assertEqual(status, 200)
        self.assertEqual([it["id"] for it in prep["items"]], leavable)
        self.assertEqual({r["id"] for r in prep["rejected"]}, {owned["id"], "-1009999999999"})
        self.assertIn("own", next(r for r in prep["rejected"] if r["id"] == owned["id"])["reason"])
        self.assertEqual(self.backend.leave_calls, [])  # preparing never leaves anything

    async def test_nothing_leavable_gives_no_token(self):
        body = await self.load()
        owned = next(it for it in body["items"] if not it["leavable"])
        _, prep, _ = await self.post("/api/leave/prepare", {"ids": [owned["id"]]})
        self.assertIsNone(prep["token"])

    async def test_bulk_leave_60_with_partial_failures_and_flood_wait(self):
        body = await self.load()
        ids = [it["id"] for it in body["items"] if it["leavable"]][:60]
        self.assertEqual(len(ids), 60)
        self.backend.script = {
            ids[5]: [("fail", "channel_private", False)],
            ids[10]: ["network"],
            ids[20]: [("flood", 1), "ok"],
            ids[30]: ["not_member"],
        }
        _, prep, _ = await self.post("/api/leave/prepare", {"ids": ids})
        self.assertEqual(len(prep["items"]), 60)
        status, job, _ = await self.post("/api/leave/start", {"token": prep["token"]})
        self.assertEqual((status, job["total"]), (200, 60))

        # While running: no second job, no reload, no reusing the token, no logout.
        status, body2, _ = await self.post("/api/leave/start", {"token": prep["token"]})
        self.assertEqual(status, 409)
        status, _, _ = await self.post("/api/leave/prepare", {"ids": ids})
        self.assertEqual(status, 409)
        status, during, _ = await self.get("/api/dialogs")  # cached copy, no refetch
        self.assertEqual((status, during["cached"], len(during["items"])), (200, True, 120))
        status, _, _ = await self.post("/api/logout")
        self.assertEqual(status, 409)

        job = await self.wait_job()
        self.assertEqual(job["state"], "finished")
        by_id = {it["id"]: it for it in job["items"]}
        self.assertEqual(by_id[ids[5]]["status"], "failed")
        self.assertFalse(by_id[ids[5]]["retryable"])
        self.assertEqual(by_id[ids[10]]["status"], "failed")
        self.assertTrue(by_id[ids[10]]["retryable"])
        self.assertEqual(by_id[ids[20]]["status"], "left")
        self.assertEqual(by_id[ids[30]]["status"], "not_member")
        self.assertEqual(job["counts"], {"left": 57, "failed": 2, "not_member": 1})
        self.assertEqual(self.backend.leave_calls.count(ids[20]), 2)

        # Retry: only the retryable failure is eligible; left items are rejected.
        retry = [it["id"] for it in job["items"] if it["status"] == "failed" and it["retryable"]]
        _, prep2, _ = await self.post("/api/leave/prepare", {"ids": retry + [ids[0]]})
        self.assertEqual([it["id"] for it in prep2["items"]], [ids[10]])
        self.assertEqual([r["id"] for r in prep2["rejected"]], [ids[0]])
        await self.post("/api/leave/start", {"token": prep2["token"]})
        job2 = await self.wait_job()
        self.assertEqual(job2["counts"], {"left": 1})

    async def test_cancel(self):
        body = await self.load()
        ids = [it["id"] for it in body["items"] if it["leavable"]][:50]
        self.cfg.leave_delay_seconds = 0.05
        _, prep, _ = await self.post("/api/leave/prepare", {"ids": ids})
        await self.post("/api/leave/start", {"token": prep["token"]})
        await asyncio.sleep(0.2)
        status, _, _ = await self.post("/api/leave/cancel")
        self.assertEqual(status, 200)
        job = await self.wait_job()
        self.assertEqual(job["state"], "cancelled")
        self.assertGreater(job["counts"].get("cancelled", 0), 0)
        self.assertEqual(len(self.backend.leave_calls), job["counts"]["left"])

    async def test_long_flood_blocks_new_batches_until_it_expires(self):
        body = await self.load()
        ids = [it["id"] for it in body["items"] if it["leavable"]][:4]
        self.backend.script = {ids[1]: [("flood", 5000)]}
        _, prep, _ = await self.post("/api/leave/prepare", {"ids": ids})
        await self.post("/api/leave/start", {"token": prep["token"]})
        job = await self.wait_job()
        self.assertEqual(job["state"], "stopped_rate_limited")
        self.assertEqual(job["counts"], {"left": 1, "rate_limited": 3})
        status, body, _ = await self.post("/api/leave/prepare", {"ids": ids[1:]})
        self.assertEqual((status, body["error"]), (429, "rate_limited"))

    async def test_reload_invalidates_tokens(self):
        body = await self.load()
        ids = [it["id"] for it in body["items"] if it["leavable"]][:2]
        _, prep, _ = await self.post("/api/leave/prepare", {"ids": ids})
        await self.load()
        status, body, _ = await self.post("/api/leave/start", {"token": prep["token"]})
        self.assertEqual((status, body["error"]), (409, "bad_token"))
        self.assertEqual(self.backend.leave_calls, [])


if __name__ == "__main__":
    unittest.main()
