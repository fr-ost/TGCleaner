"""Telethon integration, tested with real Telethon TL types and a fake client.

No network access: the fake client records every request instead of sending it.
"""

import asyncio
import logging
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from telethon import errors, functions, types

from tgcleaner.config import Config
from tgcleaner.models import LeaveFailed, NotMember, RateLimited, ServiceError
from tgcleaner.telegram_backend import TelethonBackend, classify, leave_request

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
PHOTO = types.ChatPhotoEmpty()


def channel(cid=111, **kw):
    kw.setdefault("access_hash", 999)
    return types.Channel(id=cid, title=kw.pop("title", f"Channel {cid}"), photo=PHOTO, date=NOW, **kw)


def chat(cid=222, **kw):
    return types.Chat(id=cid, title=kw.pop("title", f"Group {cid}"), photo=PHOTO,
                      participants_count=kw.pop("participants_count", 5), date=NOW, version=1, **kw)


def user(uid=333, **kw):
    return types.User(id=uid, access_hash=1, first_name="Someone", **kw)


def c(entity, last=NOW):
    return classify(entity, last_date=last, archived=False, unread_count=0)


class ClassifyTests(unittest.TestCase):
    def test_private_chats_bots_and_saved_messages_are_never_listed(self):
        self.assertEqual(c(user()), "private_chat")
        self.assertEqual(c(user(bot=True)), "bot")
        self.assertEqual(c(user(is_self=True)), "saved_messages")

    def test_broadcast_channel(self):
        item = c(channel(broadcast=True, username="news"))
        self.assertEqual((item.kind, item.role, item.leavable), ("channel", "subscriber", True))
        self.assertEqual(item.id, "-1000000000111")
        self.assertEqual(item.link, "https://t.me/news")

    def test_supergroup_admin_and_gigagroup(self):
        admin = c(channel(megagroup=True, admin_rights=types.ChatAdminRights()))
        self.assertEqual((admin.kind, admin.role, admin.leavable), ("supergroup", "admin", True))
        giga = c(channel(megagroup=True, gigagroup=True))
        self.assertEqual(giga.kind, "gigagroup")

    def test_secondary_usernames(self):
        item = c(channel(megagroup=True, usernames=[types.Username("old", active=False),
                                                     types.Username("current", active=True)]))
        self.assertEqual(item.username, "current")

    def test_owned_left_monoforum_and_min_channels_are_not_leavable(self):
        for entity in (channel(megagroup=True, creator=True), channel(left=True),
                       channel(monoforum=True), channel(min=True), channel(access_hash=None)):
            item = c(entity)
            self.assertFalse(item.leavable, entity)
            self.assertTrue(item.reason)
        self.assertEqual(c(channel(creator=True)).role, "owner")

    def test_basic_groups(self):
        item = c(chat())
        self.assertEqual((item.kind, item.id, item.leavable, item.role), ("group", "-222", True, "member"))
        for entity in (chat(deactivated=True), chat(left=True), chat(creator=True),
                       chat(migrated_to=types.InputChannel(5, 6))):
            self.assertFalse(c(entity).leavable)

    def test_forbidden_and_unknown_entity_types(self):
        self.assertFalse(c(types.ChatForbidden(id=5, title="Gone")).leavable)
        self.assertFalse(c(types.ChannelForbidden(id=6, access_hash=1, title="Banned")).leavable)
        community = types.Community(id=7, title="Community", photo=PHOTO, date=NOW, access_hash=1)
        item = c(community)
        self.assertEqual(item.kind, "other")
        self.assertFalse(item.leavable)

    def test_missing_activity_stays_unknown(self):
        self.assertIsNone(c(channel(), last=None).last_message_at)
        self.assertIsNone(c(channel(), last=datetime(1970, 1, 1, tzinfo=timezone.utc)).last_message_at)
        naive = c(channel(), last=datetime(2026, 1, 1)).last_message_at
        self.assertEqual(naive.tzinfo, timezone.utc)


class LeaveRequestTests(unittest.TestCase):
    def test_channel_uses_leave_channel(self):
        req = leave_request(types.InputPeerChannel(111, 999))
        self.assertIsInstance(req, functions.channels.LeaveChannelRequest)
        self.assertEqual((req.channel.channel_id, req.channel.access_hash), (111, 999))

    def test_basic_group_uses_delete_chat_user_self_without_revoking_history(self):
        req = leave_request(types.InputPeerChat(222))
        self.assertIsInstance(req, functions.messages.DeleteChatUserRequest)
        self.assertEqual(req.chat_id, 222)
        self.assertIsInstance(req.user_id, types.InputUserSelf)
        self.assertFalse(req.revoke_history)

    def test_users_and_self_are_never_left(self):
        self.assertIsNone(leave_request(types.InputPeerUser(1, 2)))
        self.assertIsNone(leave_request(types.InputPeerSelf()))


class FakeClient:
    """Mimics the parts of TelegramClient the backend uses."""

    def __init__(self, dialogs=(), authorized=True):
        self.dialogs = list(dialogs)
        self.authorized = authorized
        self.requests = []
        self.raise_next = []
        self.connected = False
        self.iter_kwargs = None
        self._phone_code_hash = {}
        self.sign_in_calls = []
        self.sign_in_raises = []
        self.logged_out = False

    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def is_user_authorized(self):
        return self.authorized

    async def get_me(self):
        return types.User(id=42, first_name="Me", username="me", phone="15551234567", is_self=True)

    async def __call__(self, request):
        self.requests.append(request)
        if self.raise_next:
            raise self.raise_next.pop(0)
        if isinstance(request, functions.auth.SendCodeRequest):
            return types.auth.SentCode(type=types.auth.SentCodeTypeApp(length=5), phone_code_hash="HASH")
        if isinstance(request, functions.account.GetPasswordRequest):
            return SimpleNamespace(hint="my hint")
        return types.Updates(updates=[], users=[], chats=[], date=NOW, seq=0)

    async def iter_dialogs(self, **kwargs):
        self.iter_kwargs = kwargs
        for d in self.dialogs:
            yield d

    async def sign_in(self, **kwargs):
        self.sign_in_calls.append(kwargs)
        if self.sign_in_raises:
            raise self.sign_in_raises.pop(0)
        self.authorized = True
        return await self.get_me()

    async def log_out(self):
        self.logged_out = True
        return True


def dialog(entity, input_entity, date=NOW):
    return SimpleNamespace(entity=entity, input_entity=input_entity, date=date, archived=False, unread_count=0)


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cfg = Config(api_id=12345, api_hash="a" * 32)
        self.client = FakeClient(dialogs=[
            dialog(channel(111, broadcast=True), types.InputPeerChannel(111, 999)),
            dialog(chat(222), types.InputPeerChat(222)),
            dialog(channel(333, megagroup=True, creator=True), types.InputPeerChannel(333, 1)),
            dialog(user(444), types.InputPeerUser(444, 1)),
            dialog(user(555, bot=True), types.InputPeerUser(555, 1)),
            dialog(user(42, is_self=True), types.InputPeerSelf()),
        ])
        self.backend = TelethonBackend(self.cfg, self.dir, client_factory=lambda: self.client)
        await self.backend.start()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_load_dialogs_lists_only_groups_and_channels(self):
        result = await self.backend.load_dialogs()
        self.assertEqual([it.id for it in result.items], ["-1000000000111", "-222", "-1000000000333"])
        self.assertEqual(result.excluded, {"private_chat": 1, "bot": 1, "saved_messages": 1})
        self.assertEqual(self.client.iter_kwargs, {"ignore_migrated": True})

    async def test_load_requires_login(self):
        self.client.authorized = False
        with self.assertRaises(ServiceError) as ctx:
            await self.backend.load_dialogs()
        self.assertEqual(ctx.exception.status, 401)

    async def test_leave_channel_and_group_send_the_right_requests(self):
        await self.backend.load_dialogs()
        await self.backend.leave("-1000000000111")
        await self.backend.leave("-222")
        self.assertIsInstance(self.client.requests[0], functions.channels.LeaveChannelRequest)
        self.assertIsInstance(self.client.requests[1], functions.messages.DeleteChatUserRequest)

    async def test_ineligible_or_unknown_ids_never_reach_telegram(self):
        await self.backend.load_dialogs()
        for item_id in ("-1000000000333", "444", "42", "-1000000999999"):
            with self.assertRaises(LeaveFailed) as ctx:
                await self.backend.leave(item_id)
            self.assertFalse(ctx.exception.retryable)
        self.assertEqual(self.client.requests, [])

    async def test_error_mapping(self):
        await self.backend.load_dialogs()
        cases = [
            (errors.FloodWaitError(request=None, capture=42), RateLimited, None),
            (errors.FloodPremiumWaitError(request=None, capture=7), RateLimited, None),
            (errors.UserNotParticipantError(request=None), NotMember, None),
            (errors.UserCreatorError(request=None), LeaveFailed, False),
            (errors.ChannelPrivateError(request=None), LeaveFailed, False),
            (errors.ChannelInvalidError(request=None), LeaveFailed, False),
            (errors.RPCError(request=None, message="SOMETHING_NEW", code=400), LeaveFailed, True),
            (ConnectionError("reset"), LeaveFailed, True),
            (asyncio.TimeoutError(), LeaveFailed, True),
        ]
        for exc, expected, retryable in cases:
            self.client.raise_next = [exc]
            with self.assertRaises(expected, msg=repr(exc)) as ctx:
                await self.backend.leave("-1000000000111")
            if retryable is not None:
                self.assertEqual(ctx.exception.retryable, retryable, repr(exc))
        self.client.raise_next = [errors.FloodWaitError(request=None, capture=42)]
        with self.assertRaises(RateLimited) as ctx:
            await self.backend.leave("-1000000000111")
        self.assertEqual(ctx.exception.seconds, 42)

    async def test_login_with_two_step_verification_never_logs_secrets(self):
        self.client.authorized = False
        self.client.sign_in_raises = [errors.SessionPasswordNeededError(request=None)]
        with self.assertLogs("tgcleaner", level=logging.DEBUG) as logs:
            logging.getLogger("tgcleaner").info("start")
            sent = await self.backend.send_code("+1 555 123 4567")
            self.assertEqual(sent["step"], "code")
            self.assertIsInstance(self.client.requests[0], functions.auth.SendCodeRequest)
            self.assertEqual(self.client.requests[0].phone_number, "15551234567")

            step = await self.backend.submit_code("98765")
            self.assertEqual(step, {"step": "password", "hint": "my hint"})
            self.assertEqual(self.client.sign_in_calls[0],
                             {"phone": "15551234567", "code": "98765", "phone_code_hash": "HASH"})
            status = await self.backend.status()
            self.assertEqual(status["login_step"], "password")

            self.assertEqual(await self.backend.submit_password("hunter2-secret"), {"step": "done"})
        joined = "\n".join(logs.output)
        self.assertNotIn("98765", joined)
        self.assertNotIn("hunter2-secret", joined)
        status = await self.backend.status()
        self.assertTrue(status["authorized"])
        self.assertEqual(status["account"]["phone"], "+15•••••••67")

    async def test_wrong_code_and_expired_code(self):
        self.client.authorized = False
        await self.backend.send_code("+15551234567")
        self.client.sign_in_raises = [errors.PhoneCodeInvalidError(request=None)]
        with self.assertRaises(ServiceError) as ctx:
            await self.backend.submit_code("11111")
        self.assertEqual(ctx.exception.code, "code_invalid")
        self.client.sign_in_raises = [errors.PhoneCodeExpiredError(request=None)]
        with self.assertRaises(ServiceError) as ctx:
            await self.backend.submit_code("11111")
        self.assertEqual(ctx.exception.code, "code_expired")
        with self.assertRaises(ServiceError) as ctx:  # login state was reset
            await self.backend.submit_code("11111")
        self.assertEqual(ctx.exception.code, "no_login")

    async def test_send_code_rate_limit_is_reported(self):
        self.client.authorized = False
        self.client.raise_next = [errors.FloodWaitError(request=None, capture=300)]
        with self.assertRaises(ServiceError) as ctx:
            await self.backend.send_code("+15551234567")
        self.assertEqual(ctx.exception.status, 429)
        self.assertIn("300", ctx.exception.message)

    async def test_logout_deletes_local_session_even_if_telegram_is_unreachable(self):
        session_file = self.dir / "telegram.session"
        session_file.write_text("x")

        async def broken_logout():
            raise ConnectionError("offline")

        self.client.log_out = broken_logout
        result = await self.backend.log_out()
        self.assertFalse(result["telegram_confirmed"])
        self.assertFalse(session_file.exists())

    async def test_status_never_blocks_on_a_stalled_connection(self):
        from tgcleaner import telegram_backend

        stalled = FakeClient()

        async def hang():
            await asyncio.sleep(3600)

        stalled.connect = hang
        backend = TelethonBackend(self.cfg, self.dir, client_factory=lambda: stalled)
        original = telegram_backend.CONNECT_TIMEOUT
        telegram_backend.CONNECT_TIMEOUT = 0.2
        try:
            await backend.start()
            status = await asyncio.wait_for(backend.status(), 1)
            self.assertFalse(status["telegram_reachable"])
            self.assertEqual(status["error"], "Connecting to Telegram…")
            with self.assertRaises(ServiceError) as ctx:  # operations needing Telegram fail cleanly
                await asyncio.wait_for(backend.load_dialogs(), 2)
            self.assertEqual(ctx.exception.status, 503)
            status = await backend.status()
            self.assertIn("Cannot reach Telegram", status["error"])
        finally:
            telegram_backend.CONNECT_TIMEOUT = original
            await backend.stop()

    async def test_logout_confirmed(self):
        result = await self.backend.log_out()
        self.assertTrue(result["telegram_confirmed"])
        self.assertTrue(self.client.logged_out)


if __name__ == "__main__":
    unittest.main()
