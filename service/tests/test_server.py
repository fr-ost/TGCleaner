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
        self.cfg.scan_delay_seconds = 0
        self.items, truth = demo_items(120, seed=3, bots=10)
        self.backend = FakeBackend(self.items, truth, authorized=self.authorized, require_password=True)
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
            _, body, _ = await self.get("/api/job")
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
        ok = await self.call("OPTIONS", "/api/actions/prepare", headers={
            "Origin": ORIGIN, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-tgcleaner",
            "Access-Control-Request-Private-Network": "true"})
        self.assertEqual(ok[0], 204)
        self.assertEqual(ok[2]["Access-Control-Allow-Origin"], ORIGIN)
        self.assertEqual(ok[2]["Access-Control-Allow-Private-Network"], "true")
        evil = await self.call("OPTIONS", "/api/actions/prepare", headers={
            "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
        self.assertEqual(evil[0], 403)
        self.assertNotIn("Access-Control-Allow-Origin", evil[2])

    async def test_json_content_type_and_size_limits(self):
        status, _, _ = await self.call("POST", "/api/login/phone", data="phone=1",
                                       headers=HEADERS | {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 415)
        status, _, _ = await self.post("/api/actions/prepare", json={"ids": ["-1"] * 20000})
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
        self.assertEqual(len(body["items"]), 130)
        self.assertEqual(sum(it["kind"] == "bot" for it in body["items"]), 10)
        self.assertEqual(body["excluded"]["saved_messages"], 1)
        item = body["items"][0]
        for key in ("id", "title", "kind", "username", "link", "last_message_at", "role", "leavable", "reason",
                    "muted", "archived", "last_read_at", "my_last_message_at", "scanned_at", "can_post"):
            self.assertIn(key, item)
        self.assertTrue(any(it["last_message_at"] is None for it in body["items"]))

    async def test_prepare_requires_loaded_list_and_valid_ids(self):
        status, body, _ = await self.post("/api/actions/prepare", {"action": "leave", "ids": ["-1001000000001"]})
        self.assertEqual((status, body["error"]), (409, "no_list"))
        await self.load()
        for bad in ([], "x", [123], ["abc"], ["--1"], ["1;drop"], None):
            status, body, _ = await self.post("/api/actions/prepare", {"action": "leave", "ids": bad})
            self.assertEqual(status, 400, bad)

    async def test_prepare_separates_ineligible_items_and_dedupes(self):
        body = await self.load()
        leavable = [it["id"] for it in body["items"] if it["leavable"]][:3]
        owned = next(it for it in body["items"] if it["role"] == "owner")
        status, prep, _ = await self.post("/api/actions/prepare",
                                          {"action": "leave", "ids": leavable + [leavable[0], owned["id"], "-1009999999999"]})
        self.assertEqual(status, 200)
        self.assertEqual([it["id"] for it in prep["items"]], leavable)
        self.assertEqual({r["id"] for r in prep["rejected"]}, {owned["id"], "-1009999999999"})
        self.assertIn("own", next(r for r in prep["rejected"] if r["id"] == owned["id"])["reason"])
        self.assertEqual(self.backend.leave_calls, [])  # preparing never leaves anything

    async def test_nothing_leavable_gives_no_token(self):
        body = await self.load()
        owned = next(it for it in body["items"] if not it["leavable"])
        _, prep, _ = await self.post("/api/actions/prepare", {"action": "leave", "ids": [owned["id"]]})
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
        _, prep, _ = await self.post("/api/actions/prepare", {"action": "leave", "ids": ids})
        self.assertEqual(len(prep["items"]), 60)
        status, job, _ = await self.post("/api/actions/start", {"token": prep["token"]})
        self.assertEqual((status, job["total"]), (200, 60))

        # While running: no second job, no reload, no reusing the token, no logout.
        status, body2, _ = await self.post("/api/actions/start", {"token": prep["token"]})
        self.assertEqual(status, 409)
        status, _, _ = await self.post("/api/actions/prepare", {"action": "leave", "ids": ids})
        self.assertEqual(status, 409)
        status, during, _ = await self.get("/api/dialogs")  # cached copy, no refetch
        self.assertEqual((status, during["cached"], len(during["items"])), (200, True, 130))
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
        _, prep2, _ = await self.post("/api/actions/prepare", {"action": "leave", "ids": retry + [ids[0]]})
        self.assertEqual([it["id"] for it in prep2["items"]], [ids[10]])
        self.assertEqual([r["id"] for r in prep2["rejected"]], [ids[0]])
        await self.post("/api/actions/start", {"token": prep2["token"]})
        job2 = await self.wait_job()
        self.assertEqual(job2["counts"], {"left": 1})

    async def test_cancel(self):
        body = await self.load()
        ids = [it["id"] for it in body["items"] if it["leavable"]][:50]
        self.cfg.leave_delay_seconds = 0.05
        _, prep, _ = await self.post("/api/actions/prepare", {"action": "leave", "ids": ids})
        await self.post("/api/actions/start", {"token": prep["token"]})
        await asyncio.sleep(0.2)
        status, _, _ = await self.post("/api/job/cancel")
        self.assertEqual(status, 200)
        job = await self.wait_job()
        self.assertEqual(job["state"], "cancelled")
        self.assertGreater(job["counts"].get("cancelled", 0), 0)
        self.assertEqual(len(self.backend.leave_calls), job["counts"]["left"])

    async def test_long_flood_blocks_new_batches_until_it_expires(self):
        body = await self.load()
        ids = [it["id"] for it in body["items"] if it["leavable"]][:4]
        self.backend.script = {ids[1]: [("flood", 5000)]}
        _, prep, _ = await self.post("/api/actions/prepare", {"action": "leave", "ids": ids})
        await self.post("/api/actions/start", {"token": prep["token"]})
        job = await self.wait_job()
        self.assertEqual(job["state"], "stopped_rate_limited")
        self.assertEqual(job["counts"], {"left": 1, "rate_limited": 3})
        status, body, _ = await self.post("/api/actions/prepare", {"action": "leave", "ids": ids[1:]})
        self.assertEqual((status, body["error"]), (429, "rate_limited"))

    async def test_reload_invalidates_tokens(self):
        body = await self.load()
        ids = [it["id"] for it in body["items"] if it["leavable"]][:2]
        _, prep, _ = await self.post("/api/actions/prepare", {"action": "leave", "ids": ids})
        await self.load()
        status, body, _ = await self.post("/api/actions/start", {"token": prep["token"]})
        self.assertEqual((status, body["error"]), (409, "bad_token"))
        self.assertEqual(self.backend.leave_calls, [])


class CleanupActionTests(ApiTestCase):
    async def load(self):
        status, body, _ = await self.get("/api/dialogs")
        self.assertEqual(status, 200)
        return body["items"]

    async def run_action(self, action, ids, options=None):
        status, prep, _ = await self.post("/api/actions/prepare", {"action": action, "ids": ids})
        self.assertEqual(status, 200, prep)
        if prep["token"]:
            status, job, _ = await self.post("/api/actions/start", {"token": prep["token"], "options": options or {}})
            self.assertEqual(status, 200, job)
            self.assertEqual(job["kind"], action)
            return prep, await self.wait_job()
        return prep, None

    async def test_unknown_action_and_bad_options(self):
        items = await self.load()
        status, body, _ = await self.post("/api/actions/prepare", {"action": "delete_everything", "ids": [items[0]["id"]]})
        self.assertEqual(status, 400)
        leavable = [it["id"] for it in items if it["leavable"]][:1]
        _, prep, _ = await self.post("/api/actions/prepare", {"action": "leave", "ids": leavable})
        for options in ("yes", {"block_bots": "true"}):
            status, _, _ = await self.post("/api/actions/start", {"token": prep["token"], "options": options})
            self.assertEqual(status, 400, options)
        self.assertEqual(self.backend.calls, [])

    async def test_archive_allows_owned_chats_and_skips_already_archived(self):
        items = await self.load()
        owned = next(it for it in items if it["role"] == "owner" and not it["archived"])
        archived = next(it for it in items if it["archived"])
        prep, job = await self.run_action("archive", [owned["id"], archived["id"]])
        self.assertEqual([r["id"] for r in prep["rejected"]], [archived["id"]])
        self.assertEqual(job["counts"], {"archived": 1})
        items = await self.load()
        self.assertTrue(next(it for it in items if it["id"] == owned["id"])["archived"])
        prep, job = await self.run_action("archive", [owned["id"]])  # second time: nothing to do
        self.assertIsNone(prep["token"])

    async def test_mute(self):
        items = await self.load()
        unmuted = [it["id"] for it in items if not it["muted"] and it["accessible"]][:5]
        _, job = await self.run_action("mute", unmuted)
        self.assertEqual(job["counts"], {"muted": 5})
        self.assertEqual({a for a, _ in self.backend.calls}, {"mute"})

    async def test_remove_bots_with_and_without_blocking(self):
        items = await self.load()
        bots = [it["id"] for it in items if it["kind"] == "bot"]
        _, job = await self.run_action("leave", bots[:2], {"block_bots": True})
        self.assertEqual(job["counts"], {"removed": 2})
        self.assertEqual(self.backend.blocked, bots[:2])
        self.assertIn("Bot blocked", job["items"][0]["message"])
        _, job = await self.run_action("leave", bots[2:4])
        self.assertEqual(job["counts"], {"removed": 2})
        self.assertEqual(self.backend.blocked, bots[:2])  # not blocked without the option
        ids_after = {it["id"] for it in await self.load()}
        self.assertFalse(ids_after & set(bots[:4]))  # deleted chats are gone after reload

    async def test_scan_fills_engagement_and_is_read_only(self):
        items = await self.load()
        needing = [it for it in items if it["accessible"] and it["kind"] != "other" and
                   (it["last_read_at"] is None or (it["can_post"] and it["my_last_message_at"] is None))]
        status, job, _ = await self.post("/api/scan/start", {})
        self.assertEqual((status, job["kind"], job["total"]), (200, "scan", len(needing)))
        job = await self.wait_job()
        self.assertEqual(job["counts"], {"scanned": len(needing)})
        self.assertEqual({a for a, _ in self.backend.calls}, {"scan"})  # nothing but reads
        items = await self.load()
        self.assertTrue(all(it["scanned_at"] for it in items if it["id"] in {n["id"] for n in needing}))
        ignored = [it for it in items if it["unread_count"] > 0 and it["last_read_at"]]
        self.assertTrue(ignored)  # busy chats you stopped reading now have a known read position
        status, body, _ = await self.post("/api/scan/start", {})
        self.assertEqual((status, body["error"]), (409, "nothing_to_do"))
        calls = len(self.backend.calls)
        status, cached, _ = await self.get("/api/dialogs?cached=1")
        self.assertEqual((status, cached["cached"]), (200, True))
        self.assertEqual(len(self.backend.calls), calls)
        status, job, _ = await self.post("/api/scan/start", {"ids": [items[0]["id"]]})  # explicit rescan
        self.assertEqual(status, 200)
        await self.wait_job()

    async def test_rate_limit_on_scan_does_not_block_actions(self):
        items = await self.load()
        first = items[0]["id"]
        self.backend.scan_script = {first: [("flood", 5000)]}
        await self.post("/api/scan/start", {"ids": [first]})
        job = await self.wait_job()
        self.assertEqual(job["state"], "stopped_rate_limited")
        status, body, _ = await self.post("/api/scan/start", {"ids": [first]})
        self.assertEqual((status, body["error"]), (429, "rate_limited"))
        unmuted = [it["id"] for it in items if not it["muted"] and it["accessible"]][:1]
        _, job = await self.run_action("mute", unmuted)
        self.assertEqual(job["counts"], {"muted": 1})

    async def test_only_one_job_at_a_time(self):
        await self.load()
        self.cfg.scan_delay_seconds = 0.2
        status, _, _ = await self.post("/api/scan/start", {})
        self.assertEqual(status, 200)
        items = (await self.get("/api/dialogs"))[1]["items"]
        status, body, _ = await self.post("/api/actions/prepare", {"action": "mute", "ids": [items[0]["id"]]})
        self.assertEqual((status, body["error"]), (409, "job_active"))
        await self.post("/api/job/cancel")
        job = await self.wait_job()
        self.assertEqual(job["state"], "cancelled")


TOKEN = "t" * 40


class CloudModeTests(unittest.IsolatedAsyncioTestCase):
    """Hosted mode: reachable from the internet, so an access token is required on every API call."""

    async def asyncSetUp(self):
        self.port = free_port()
        self.cfg = Config(api_id=1, api_hash="0" * 32, port=self.port)
        items, truth = demo_items(20, seed=5)
        self.backend = FakeBackend(items, truth, authorized=True)
        self.server = TestServer(create_app(self.cfg, self.backend, access_token=TOKEN), host="127.0.0.1", port=self.port)
        await self.server.start_server()
        self.http = ClientSession()
        self.url = f"http://127.0.0.1:{self.port}"
        # Requests arrive through the platform's proxy with the public host name.
        self.public = {"Host": "tgcleaner-production.up.railway.app"}

    async def asyncTearDown(self):
        await self.http.close()
        await self.server.close()

    async def get(self, path, headers):
        async with self.http.get(self.url + path, headers=headers) as r:
            return r.status, (await r.json()), r.headers

    async def test_health_is_open_for_the_platform_healthcheck(self):
        status, body, _ = await self.get("/health", {"Host": "healthcheck.railway.app"})
        self.assertEqual((status, body["ok"]), (200, True))

    async def test_token_required_even_with_a_valid_looking_origin(self):
        forged = HEADERS | self.public  # curl can send any Origin; the token is what protects the account
        for auth in (None, "Bearer wrong", f"Bearer {TOKEN}x", TOKEN, f"bearer {TOKEN}"):
            headers = forged | ({"Authorization": auth} if auth else {})
            status, body, resp_headers = await self.get("/api/status", headers)
            self.assertEqual((status, body["error"]), (401, "bad_access_token"), auth)
            self.assertEqual(resp_headers["Access-Control-Allow-Origin"], ORIGIN)  # extension can read the reason
        status, body, _ = await self.get("/api/dialogs", forged | {"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["items"]), 20)

    async def test_origin_still_checked_in_cloud_mode(self):
        status, body, _ = await self.get("/api/status", {"Origin": "https://evil.example", "X-TGCleaner": "1",
                                                         "Authorization": f"Bearer {TOKEN}"} | self.public)
        self.assertEqual((status, body["error"]), (403, "origin_not_allowed"))

    async def test_preflight_allows_authorization_header(self):
        async with self.http.options(self.url + "/api/status", headers={
                "Origin": ORIGIN, "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization,x-tgcleaner"} | self.public) as r:
            self.assertEqual(r.status, 204)
            self.assertIn("Authorization", r.headers["Access-Control-Allow-Headers"])

    async def test_repeated_wrong_tokens_lock_the_api(self):
        bad = HEADERS | self.public | {"Authorization": "Bearer nope"}
        for _ in range(20):
            await self.get("/api/status", bad)
        status, body, _ = await self.get("/api/status", HEADERS | self.public | {"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual((status, body["error"]), (429, "auth_locked"))

    async def test_cloud_mode_without_token_accepts_public_host(self):
        app = TestServer(create_app(self.cfg, self.backend, cloud=True), host="127.0.0.1", port=free_port())
        await app.start_server()
        try:
            async with self.http.get(f"http://127.0.0.1:{app.port}/api/dialogs", headers=HEADERS | self.public) as r:
                self.assertEqual(r.status, 200)
        finally:
            await app.close()

    async def test_local_mode_still_rejects_foreign_hosts(self):
        local = TestServer(create_app(self.cfg, self.backend), host="127.0.0.1", port=free_port())
        await local.start_server()
        try:
            async with self.http.get(f"http://127.0.0.1:{local.port}/api/status", headers=HEADERS | self.public) as r:
                self.assertEqual(r.status, 403)
        finally:
            await local.close()


class CloudSettingsTests(unittest.TestCase):
    def test_reads_environment(self):
        from tgcleaner.config import cloud_settings_from_env

        s = cloud_settings_from_env({"TG_API_ID": "12345", "TG_API_HASH": "a" * 32, "TGC_ACCESS_TOKEN": TOKEN,
                                     "PORT": "8080", "TGC_LEAVE_DELAY": "3"})
        self.assertEqual((s.config.api_id, s.config.port, s.access_token, s.demo), (12345, 8080, TOKEN, False))
        self.assertEqual(s.config.leave_delay_seconds, 3.0)
        self.assertEqual(s.config.extension_ids, [DEFAULT_EXTENSION_ID])

    def test_token_is_optional(self):
        from tgcleaner.config import cloud_settings_from_env

        s = cloud_settings_from_env({"TG_API_ID": "12345", "TG_API_HASH": "a" * 32})
        self.assertIsNone(s.access_token)

    def test_demo_needs_no_telegram_credentials(self):
        from tgcleaner.config import cloud_settings_from_env

        s = cloud_settings_from_env({"TGC_ACCESS_TOKEN": TOKEN, "TGC_DEMO": "1"})
        self.assertTrue(s.demo)

    def test_rejects_missing_or_weak_settings(self):
        from tgcleaner.config import cloud_settings_from_env

        for env in ({"TGC_ACCESS_TOKEN": "short", "TG_API_ID": "1", "TG_API_HASH": "a" * 32},
                    {"TGC_ACCESS_TOKEN": TOKEN}, {"TGC_ACCESS_TOKEN": TOKEN, "TG_API_ID": "1", "TG_API_HASH": "nothex"},
                    {"TGC_ACCESS_TOKEN": TOKEN, "TG_API_ID": "1", "TG_API_HASH": "a" * 32, "TGC_EXTENSION_IDS": "bad"}):
            with self.assertRaises(ValueError, msg=env):
                cloud_settings_from_env(env)


if __name__ == "__main__":
    unittest.main()
