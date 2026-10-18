"""Telethon integration, tested with real Telethon TL types and a fake client.

No network access: the fake client records every request instead of sending it.
"""

import asyncio
import json
import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from telethon import errors, functions, types

from tgcleaner import telegram_backend
from tgcleaner.config import Config
from tgcleaner.models import ARCHIVE, LEAVE, MUTE, ActionFailed, RateLimited, ServiceError
from tgcleaner.telegram_backend import MUTE_FOREVER, TelethonBackend, classify, leave_request

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
PHOTO = types.ChatPhotoEmpty()


def channel(cid=111, **kw):
    kw.setdefault("access_hash", 999)
    return types.Channel(id=cid, title=kw.pop("title", f"Channel {cid}"), photo=PHOTO, date=NOW, **kw)


def chat(cid=222, **kw):
    return types.Chat(id=cid, title=kw.pop("title", f"Group {cid}"), photo=PHOTO,
                      participants_count=kw.pop("participants_count", 5), date=NOW, version=1, **kw)


def user(uid=333, **kw):
    return types.User(id=uid, access_hash=1, first_name=kw.pop("first_name", "Someone"), **kw)


def c(entity, last=NOW, **kw):
    kw.setdefault("unread_count", 3)
    return classify(entity, last_date=last, archived=False, **kw)


class ClassifyTests(unittest.TestCase):
    def test_human_private_chats_and_saved_messages_are_never_listed(self):
        self.assertEqual(c(user()), "private_chat")
        self.assertEqual(c(user(is_self=True)), "saved_messages")
        self.assertEqual(c(user(deleted=True)), "private_chat")

    def test_bots_are_listed_and_removable(self):
        item = c(user(444, bot=True, first_name="Weather", username="weatherbot"))
        self.assertEqual((item.kind, item.id, item.title, item.leavable, item.can_post), ("bot", "444", "Weather", True, True))
        self.assertEqual(item.link, "https://t.me/weatherbot")

    def test_broadcast_channel(self):
        item = c(channel(broadcast=True, username="news"))
        self.assertEqual((item.kind, item.role, item.leavable, item.can_post), ("channel", "subscriber", True, False))
        self.assertEqual(item.id, "-1000000000111")
        self.assertEqual(item.link, "https://t.me/news")
        poster = c(channel(broadcast=True, admin_rights=types.ChatAdminRights(post_messages=True)))
        self.assertTrue(poster.can_post)

    def test_supergroup_admin_and_gigagroup(self):
        admin = c(channel(megagroup=True, admin_rights=types.ChatAdminRights()))
        self.assertEqual((admin.kind, admin.role, admin.leavable, admin.can_post), ("supergroup", "admin", True, True))
        self.assertEqual(c(channel(megagroup=True, gigagroup=True)).kind, "gigagroup")

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
        gone = c(types.ChatForbidden(id=5, title="Gone"))
        self.assertEqual((gone.leavable, gone.accessible), (False, False))
        self.assertFalse(c(types.ChannelForbidden(id=6, access_hash=1, title="Banned")).accessible)
        community = types.Community(id=7, title="Community", photo=PHOTO, date=NOW, access_hash=1)
        item = c(community)
        self.assertEqual(item.kind, "other")
        self.assertFalse(item.leavable)

    def test_missing_activity_stays_unknown(self):
        self.assertIsNone(c(channel(), last=None).last_message_at)
        self.assertIsNone(c(channel(), last=datetime(1970, 1, 1, tzinfo=timezone.utc)).last_message_at)
        naive = c(channel(), last=datetime(2026, 1, 1)).last_message_at
        self.assertEqual(naive.tzinfo, timezone.utc)

    def test_free_engagement_signals(self):
        behind = c(channel(megagroup=True), unread_count=50)
        self.assertIsNone(behind.last_read_at)       # needs a scan
        self.assertIsNone(behind.my_last_message_at)
        caught_up = c(channel(megagroup=True), unread_count=0, last_is_mine=True, muted=True)
        self.assertEqual(caught_up.last_read_at, NOW)  # you've read the newest message
        self.assertEqual(caught_up.my_last_message_at, NOW)
        self.assertTrue(caught_up.muted)


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
        self.responses = []  # queued return values for the next requests
        self.fail_on = {}    # request class -> exception raised whenever it is sent
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
        if type(request) in self.fail_on:
            raise self.fail_on[type(request)]
        if self.raise_next:
            raise self.raise_next.pop(0)
        if self.responses:
            return self.responses.pop(0)
        if isinstance(request, functions.auth.SendCodeRequest):
            return types.auth.SentCode(type=types.auth.SentCodeTypeApp(length=5), phone_code_hash="HASH")
        if isinstance(request, functions.account.GetPasswordRequest):
            return SimpleNamespace(hint="my hint")
        if isinstance(request, functions.messages.DeleteHistoryRequest):
            return types.messages.AffectedHistory(pts=1, pts_count=1, offset=0)
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


def dialog(entity, input_entity, date=NOW, unread=7, read_max=500, out=False, mute_until=None):
    raw = SimpleNamespace(read_inbox_max_id=read_max, notify_settings=types.PeerNotifySettings(mute_until=mute_until))
    return SimpleNamespace(entity=entity, input_entity=input_entity, date=date, archived=False, unread_count=unread,
                           dialog=raw, message=SimpleNamespace(out=out, date=date))


def messages(*dates):
    return types.messages.Messages(
        messages=[types.Message(id=i + 1, peer_id=types.PeerUser(1), date=d, message="x") for i, d in enumerate(dates)],
        topics=[], chats=[], users=[])


CH, GR, OWNED, BOT_ID = "-1000000000111", "-222", "-1000000000333", "555"


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cfg = Config(api_id=12345, api_hash="a" * 32)
        self.client = FakeClient(dialogs=[
            dialog(channel(111, broadcast=True), types.InputPeerChannel(111, 999)),
            dialog(chat(222), types.InputPeerChat(222), unread=0, out=True),
            dialog(channel(333, megagroup=True, creator=True), types.InputPeerChannel(333, 1),
                   mute_until=NOW + timedelta(days=36500)),
            dialog(user(444), types.InputPeerUser(444, 1)),
            dialog(user(555, bot=True), types.InputPeerUser(555, 1)),
            dialog(user(42, is_self=True), types.InputPeerSelf()),
        ])
        self.backend = TelethonBackend(self.cfg, self.dir, client_factory=lambda: self.client)
        await self.backend.start()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_load_dialogs_lists_groups_channels_and_bots_only(self):
        result = await self.backend.load_dialogs()
        self.assertEqual([it.id for it in result.items], [CH, GR, OWNED, BOT_ID])
        self.assertEqual(result.excluded, {"private_chat": 1, "saved_messages": 1})
        self.assertEqual(self.client.iter_kwargs, {"ignore_migrated": True})
        by_id = {it.id: it for it in result.items}
        self.assertTrue(by_id[OWNED].muted)
        self.assertFalse(by_id[CH].muted)
        self.assertEqual(by_id[GR].my_last_message_at, NOW)

    async def test_load_requires_login(self):
        self.client.authorized = False
        with self.assertRaises(ServiceError) as ctx:
            await self.backend.load_dialogs()
        self.assertEqual(ctx.exception.status, 401)

    async def test_leave_channel_and_group_send_the_right_requests(self):
        await self.backend.load_dialogs()
        self.assertEqual((await self.backend.perform(LEAVE, CH, {}))[0], "left")
        self.assertEqual((await self.backend.perform(LEAVE, GR, {}))[0], "left")
        self.assertIsInstance(self.client.requests[0], functions.channels.LeaveChannelRequest)
        self.assertIsInstance(self.client.requests[1], functions.messages.DeleteChatUserRequest)
        with self.assertRaises(ActionFailed):  # already left: never sent twice
            await self.backend.perform(LEAVE, CH, {})
        self.assertEqual(len(self.client.requests), 2)

    async def test_bot_chat_is_deleted_on_your_side_only_and_optionally_blocked(self):
        await self.backend.load_dialogs()
        self.client.responses = [types.messages.AffectedHistory(pts=1, pts_count=100, offset=5),
                                 types.messages.AffectedHistory(pts=2, pts_count=3, offset=0)]
        status, message = await self.backend.perform(LEAVE, BOT_ID, {"block_bots": True})
        self.assertEqual(status, "removed")
        self.assertIn("Bot blocked", message)
        deletes = [r for r in self.client.requests if isinstance(r, functions.messages.DeleteHistoryRequest)]
        self.assertEqual(len(deletes), 2)  # repeated while Telegram reports more to delete
        self.assertTrue(all(d.revoke is False and d.just_clear is False and d.max_id == 0 for d in deletes))
        self.assertIsInstance(self.client.requests[-1], functions.contacts.BlockRequest)

    async def test_bot_chat_deleted_without_blocking_by_default(self):
        await self.backend.load_dialogs()
        await self.backend.perform(LEAVE, BOT_ID, {})
        self.assertFalse(any(isinstance(r, functions.contacts.BlockRequest) for r in self.client.requests))

    async def test_bot_block_failure_does_not_hide_the_deletion(self):
        await self.backend.load_dialogs()
        self.client.fail_on = {functions.contacts.BlockRequest: errors.RPCError(request=None, message="SOMETHING", code=400)}
        status, message = await self.backend.perform(LEAVE, BOT_ID, {"block_bots": True})
        self.assertEqual(status, "removed")
        self.assertIn("Blocking failed", message)

    async def test_private_chats_and_unlisted_ids_never_reach_telegram(self):
        await self.backend.load_dialogs()
        for action in (LEAVE, ARCHIVE, MUTE):
            for item_id in ("444", "42", "-1000000999999"):
                with self.assertRaises(ActionFailed) as ctx:
                    await self.backend.perform(action, item_id, {})
                self.assertFalse(ctx.exception.retryable)
        with self.assertRaises(ActionFailed):  # owned: can't leave
            await self.backend.perform(LEAVE, OWNED, {})
        with self.assertRaises(ActionFailed):  # already muted
            await self.backend.perform(MUTE, OWNED, {})
        self.assertEqual(self.client.requests, [])

    async def test_archive_and_mute_requests(self):
        await self.backend.load_dialogs()
        self.assertEqual(await self.backend.perform(ARCHIVE, OWNED, {}), ("archived", "Moved to Archive."))
        self.assertEqual((await self.backend.perform(MUTE, CH, {}))[0], "muted")
        archive, mute = self.client.requests
        self.assertIsInstance(archive, functions.folders.EditPeerFoldersRequest)
        self.assertEqual(archive.folder_peers[0].folder_id, 1)
        self.assertEqual(archive.folder_peers[0].peer, types.InputPeerChannel(333, 1))
        self.assertIsInstance(mute, functions.account.UpdateNotifySettingsRequest)
        self.assertEqual(mute.settings.mute_until, MUTE_FOREVER)
        self.assertTrue(self.backend.cached_item(OWNED).archived)
        self.assertTrue(self.backend.cached_item(CH).muted)
        with self.assertRaises(ActionFailed):  # duplicates are refused locally
            await self.backend.perform(ARCHIVE, OWNED, {})
        self.assertEqual(len(self.client.requests), 2)

    async def test_error_mapping(self):
        await self.backend.load_dialogs()
        cases = [
            (errors.FloodWaitError(request=None, capture=42), RateLimited, None),
            (errors.FloodPremiumWaitError(request=None, capture=7), RateLimited, None),
            (errors.UserCreatorError(request=None), ActionFailed, False),
            (errors.ChannelPrivateError(request=None), ActionFailed, False),
            (errors.ChannelInvalidError(request=None), ActionFailed, False),
            (errors.RPCError(request=None, message="SOMETHING_NEW", code=400), ActionFailed, True),
            (ConnectionError("reset"), ActionFailed, True),
            (asyncio.TimeoutError(), ActionFailed, True),
        ]
        for exc, expected, retryable in cases:
            self.client.raise_next = [exc]
            with self.assertRaises(expected, msg=repr(exc)) as ctx:
                await self.backend.perform(LEAVE, CH, {})
            if retryable is not None:
                self.assertEqual(ctx.exception.retryable, retryable, repr(exc))
        self.client.raise_next = [errors.FloodWaitError(request=None, capture=42)]
        with self.assertRaises(RateLimited) as ctx:
            await self.backend.perform(LEAVE, CH, {})
        self.assertEqual(ctx.exception.seconds, 42)
        self.client.raise_next = [errors.UserNotParticipantError(request=None)]
        self.assertEqual((await self.backend.perform(LEAVE, CH, {}))[0], "not_member")

    async def test_scan_reads_position_and_own_last_message_and_caches_it(self):
        await self.backend.load_dialogs()
        read_date, my_date = NOW - timedelta(days=200), NOW - timedelta(days=400)
        # Channel (can't post): only the read position is looked up.
        self.client.responses = [messages(read_date)]
        await self.backend.scan(CH)
        (history,) = self.client.requests
        self.assertIsInstance(history, functions.messages.GetHistoryRequest)
        self.assertEqual((history.offset_id, history.limit), (501, 1))
        self.assertEqual(self.backend.cached_item(CH).last_read_at, read_date)

        # Bot: read position + search for your own last message.
        self.client.requests.clear()
        self.client.responses = [messages(read_date), messages(my_date)]
        status, _ = await self.backend.scan(BOT_ID)
        self.assertEqual(status, "scanned")
        search = self.client.requests[1]
        self.assertIsInstance(search, functions.messages.SearchRequest)
        self.assertIsInstance(search.from_id, types.InputPeerSelf)
        self.assertEqual(search.limit, 1)
        self.assertEqual(self.backend.cached_item(BOT_ID).my_last_message_at, my_date)

        # Caught-up group where the newest message is yours: nothing to ask Telegram.
        self.client.requests.clear()
        await self.backend.scan(GR)
        self.assertEqual(self.client.requests, [])

        # The result survives a reload (same read position) ...
        cache = json.loads((self.dir / "scan_cache.json").read_text())
        self.assertIn(BOT_ID, cache)
        await self.backend.load_dialogs()
        self.assertEqual(self.backend.cached_item(BOT_ID).my_last_message_at, my_date)
        self.assertIsNotNone(self.backend.cached_item(BOT_ID).scanned_at)
        # ... but is dropped once you have read further (the dates could be stale).
        self.client.dialogs[4] = dialog(user(555, bot=True), types.InputPeerUser(555, 1), read_max=900)
        await self.backend.load_dialogs()
        self.assertIsNone(self.backend.cached_item(BOT_ID).my_last_message_at)
        self.assertIsNone(self.backend.cached_item(BOT_ID).scanned_at)

    async def test_scan_with_no_messages_found(self):
        await self.backend.load_dialogs()
        self.client.responses = [messages(), messages()]
        await self.backend.scan(BOT_ID)
        item = self.backend.cached_item(BOT_ID)
        self.assertIsNone(item.last_read_at)
        self.assertIsNone(item.my_last_message_at)
        self.assertIsNotNone(item.scanned_at)

    async def test_scan_flood_wait_is_reported(self):
        await self.backend.load_dialogs()
        self.client.raise_next = [errors.FloodWaitError(request=None, capture=30)]
        with self.assertRaises(RateLimited):
            await self.backend.scan(CH)

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

    async def test_logout_deletes_local_session_and_scan_cache_even_if_telegram_is_unreachable(self):
        session_file = self.dir / "telegram.session"
        session_file.write_text("x")
        (self.dir / "scan_cache.json").write_text("{}")

        async def broken_logout():
            raise ConnectionError("offline")

        self.client.log_out = broken_logout
        result = await self.backend.log_out()
        self.assertFalse(result["telegram_confirmed"])
        self.assertFalse(session_file.exists())
        self.assertFalse((self.dir / "scan_cache.json").exists())

    async def test_status_never_blocks_on_a_stalled_connection(self):
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
