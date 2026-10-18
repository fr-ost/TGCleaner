"""Telegram access through Telethon (MTProto, user account).

Telegram operations used (verified against Telethon 1.45 / API layer 229):
  login     auth.sendCode -> auth.signIn -> (2FA) account.getPassword + auth.checkPassword
  list      messages.getDialogs, page by page, for the main list (folder 0) and Archive (folder 1)
  activity  date of each dialog's top (most recent) message; absent when there is no history
  read      dialog unread count / read_inbox_max_id; when behind, messages.getHistory(limit=1)
            just below the read position gives the date of the newest message you have read
  posted    messages.search(from_id=inputPeerSelf, limit=1): your newest message in the chat
  leave     channels.leaveChannel for channels/supergroups,
            messages.deleteChatUser(chat_id, inputUserSelf) for basic groups
  bots      messages.deleteHistory(revoke=False) deletes the chat on your side only;
            contacts.block optionally blocks the bot
  archive   folders.editPeerFolders(folder_id=1)
  mute      account.updateNotifySettings(mute_until=forever)
  logout    auth.logOut, then the local session file is deleted

We deliberately do not use TelegramClient.delete_dialog(): given a user it would
delete a private conversation. Human private chats and Saved Messages are never
listed, so no action can reach them; deleting a chat is only done for bots.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient, errors, functions, types, utils

from . import __version__
from .config import Config, make_private, session_path
from .protect import load_protected, save_protected
from .models import (
    ARCHIVE, BOT, BROADCAST_GROUP, CHANNEL, GROUP, LEAVE, MUTE, OTHER, SUPERGROUP,
    ActionFailed, DialogItem, DialogList, RateLimited, ServiceError, ineligibility, iso, parse_iso,
)

log = logging.getLogger("tgcleaner.telegram")

# Before Telegram existed: a date like this means "no real date".
_MIN_VALID_DATE = datetime(2013, 1, 1, tzinfo=timezone.utc)

OWNER_REASON = ("You own this. Transfer ownership or delete it in the Telegram app; "
                "this tool does not leave chats you created.")

CONNECT_TIMEOUT = 45  # seconds for one complete connect attempt
MUTE_FOREVER = datetime.fromtimestamp(2**31 - 1, tz=timezone.utc)  # Telegram's "muted forever"
SCAN_CACHE = "scan_cache.json"
DIALOG_PAGE = 100          # dialogs per getDialogs request (Telegram's maximum)
DIALOG_PAGE_PAUSE = 0.3    # seconds between pages

# Errors that carry a wait time (FLOOD_WAIT_X, FLOOD_PREMIUM_WAIT_X, ...).
_FLOOD_ERRORS = (errors.FloodWaitError, errors.FloodPremiumWaitError)


# ---------------------------------------------------------------------------
# Classification (pure: unit tested with real Telethon types, no network)
# ---------------------------------------------------------------------------

def classify(entity, *, last_date, archived: bool, unread_count: int,
             muted: bool = False, last_is_mine: bool = False):
    """Turn one dialog's entity into a DialogItem.

    Returns a string category ("private_chat", "saved_messages") for dialogs
    that must never be listed.
    """
    if isinstance(entity, types.User) and (entity.is_self or not entity.bot):
        return "saved_messages" if entity.is_self else "private_chat"

    peer_id = str(utils.get_peer_id(entity))
    last = _valid_date(last_date)
    common = dict(
        id=peer_id, last_message_at=last, archived=archived, unread_count=unread_count or 0, muted=muted,
        # Free engagement signals from the dialog list itself:
        last_read_at=last if (unread_count or 0) == 0 else None,   # caught up: you've read the newest message
        my_last_message_at=last if last_is_mine else None,         # the newest message is yours
    )

    if isinstance(entity, types.User):  # a bot
        name = utils.get_display_name(entity) or "(deleted bot)"
        return DialogItem(title=name, kind=BOT, username=_username(entity), role="none",
                          participants_count=None, leavable=True, can_post=True, **common)

    title = getattr(entity, "title", None) or "(untitled)"

    if isinstance(entity, types.Channel):
        if entity.megagroup:
            kind = BROADCAST_GROUP if entity.gigagroup else SUPERGROUP
        else:
            kind = CHANNEL
        role = _role(entity, broadcast=kind == CHANNEL)
        reason = None
        if entity.monoforum:
            reason = "This is a channel's direct-messages inbox, not a membership."
        elif entity.left:
            reason = "You already left this."
        elif entity.creator:
            reason = OWNER_REASON
        elif entity.min or entity.access_hash is None:
            reason = "Telegram did not provide enough information to leave this safely. Reload and try again."
        rights = entity.admin_rights
        can_post = kind != CHANNEL or bool(entity.creator or (rights and rights.post_messages))
        return DialogItem(title=title, kind=kind, username=_username(entity), role=role,
                          participants_count=entity.participants_count,
                          leavable=reason is None, reason=reason, can_post=can_post, **common)

    if isinstance(entity, types.ChannelForbidden):
        kind = SUPERGROUP if entity.megagroup else CHANNEL
        return DialogItem(title=title, kind=kind, username=None, role="none", participants_count=None,
                          leavable=False, reason="You were banned or this is no longer accessible.",
                          accessible=False, can_post=False, **common)

    if isinstance(entity, types.Chat):
        reason = None
        if entity.deactivated or entity.migrated_to is not None:
            reason = "This group was upgraded to a supergroup. Leave the supergroup instead."
        elif entity.left:
            reason = "You already left this group."
        elif entity.creator:
            reason = OWNER_REASON
        return DialogItem(title=title, kind=GROUP, username=None, role=_role(entity, broadcast=False),
                          participants_count=entity.participants_count,
                          leavable=reason is None, reason=reason, can_post=reason is None or entity.creator, **common)

    if isinstance(entity, types.ChatForbidden):
        return DialogItem(title=title, kind=GROUP, username=None, role="none", participants_count=None,
                          leavable=False, reason="You were removed from this group.",
                          accessible=False, can_post=False, **common)

    # Communities and any entity type added by a future Telegram layer.
    return DialogItem(title=title, kind=OTHER, username=None, role="unknown", participants_count=None,
                      leavable=False, can_post=False,
                      reason=f"{type(entity).__name__} is not supported by this tool. Use the Telegram app.",
                      **common)


def _is_muted(notify_settings) -> bool:
    until = getattr(notify_settings, "mute_until", None)
    return isinstance(until, datetime) and until > datetime.now(timezone.utc)


def _valid_date(value):
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value if value >= _MIN_VALID_DATE else None


def _role(entity, *, broadcast: bool) -> str:
    if getattr(entity, "creator", False):
        return "owner"
    if getattr(entity, "admin_rights", None) is not None:
        return "admin"
    return "subscriber" if broadcast else "member"


def _username(entity) -> str | None:
    if getattr(entity, "username", None):
        return entity.username
    for u in getattr(entity, "usernames", None) or []:
        if getattr(u, "active", False):
            return u.username
    return None


def leave_request(input_peer):
    """The single Telegram request that leaves `input_peer`, or None if it is not a group/channel."""
    if isinstance(input_peer, types.InputPeerChannel):
        return functions.channels.LeaveChannelRequest(
            types.InputChannel(input_peer.channel_id, input_peer.access_hash))
    if isinstance(input_peer, types.InputPeerChat):
        return functions.messages.DeleteChatUserRequest(
            chat_id=input_peer.chat_id, user_id=types.InputUserSelf())
    return None


def _mask_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = phone.lstrip("+")
    return "+" + digits[:2] + "•" * max(0, len(digits) - 4) + digits[-2:]


def _code_delivery(sent) -> str:
    kind = type(getattr(sent, "type", None)).__name__
    return {
        "SentCodeTypeApp": "Telegram app (check your other logged-in device)",
        "SentCodeTypeSms": "SMS",
        "SentCodeTypeSmsWord": "SMS",
        "SentCodeTypeSmsPhrase": "SMS",
        "SentCodeTypeFirebaseSms": "SMS",
        "SentCodeTypeCall": "phone call",
        "SentCodeTypeFlashCall": "phone call",
        "SentCodeTypeMissedCall": "missed call (the code is the last digits of the calling number)",
        "SentCodeTypeEmailCode": "email",
        "SentCodeTypeFragmentSms": "Fragment",
    }.get(kind, "Telegram")


@dataclass
class _Entry:
    item: DialogItem
    peer: object          # InputPeer* used for every request
    read_max_id: int      # read_inbox_max_id when the list was loaded
    is_bot: bool


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------

class TelethonBackend:
    demo = False

    def __init__(self, cfg: Config, data_dir: Path, client_factory=None):
        self._cfg = cfg
        self._data_dir = data_dir
        self._client_factory = client_factory or self._make_client
        self._client: TelegramClient | None = None
        self._lock = asyncio.Lock()     # serializes login / logout / dialog loading
        self._connect_lock = asyncio.Lock()
        self._connect_task: asyncio.Task | None = None
        self._login: dict | None = None  # {"phone", "hash", "step"} while logging in
        self._me = None
        self._connect_error: str | None = None
        self._entries: dict[str, _Entry] = {}
        self._protected: set[str] = load_protected(data_dir)

    def _make_client(self) -> TelegramClient:
        return TelegramClient(
            str(session_path(self._data_dir)), self._cfg.api_id, self._cfg.api_hash,
            # Never sleep silently: every flood wait surfaces to us so the
            # dashboard can show it and the leave job can decide what to do.
            flood_sleep_threshold=0,
            raise_last_call_error=True,
            connection_retries=2,
            receive_updates=False,
            device_model="TG Cleaner (personal)",
            app_version=__version__,
        )

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        self._client = self._client_factory()
        # Connect in the background so the dashboard gets answers even while Telegram is slow or unreachable.
        self._kick_connect()

    async def stop(self) -> None:
        if self._connect_task is not None and not self._connect_task.done():
            self._connect_task.cancel()
        if self._client is not None:
            await self._client.disconnect()

    def _kick_connect(self) -> None:
        if self._client.is_connected():
            return
        if self._connect_task is None or self._connect_task.done():
            self._connect_task = asyncio.create_task(self._ensure_connected())

    async def _ensure_connected(self) -> bool:
        async with self._connect_lock:
            client = self._client
            if client.is_connected():
                return True
            try:
                # Hard cap: a broken network or proxy can otherwise stall a connect indefinitely.
                await asyncio.wait_for(client.connect(), CONNECT_TIMEOUT)
                self._connect_error = None
                make_private(Path(str(session_path(self._data_dir)) + ".session"))
                return True
            except (OSError, asyncio.TimeoutError) as e:
                self._connect_error = f"Cannot reach Telegram ({type(e).__name__}). Check your internet connection."
                log.warning("connect failed: %s", type(e).__name__)
                try:
                    await client.disconnect()
                except Exception:  # cleanup only
                    pass
                return False

    async def _require_connection(self):
        if not await self._ensure_connected():
            raise ServiceError("telegram_unreachable", self._connect_error, 503)

    async def _require_authorized(self):
        await self._require_connection()
        if not await self._client.is_user_authorized():
            raise ServiceError("not_authorized", "Connect your Telegram account first.", 401)

    # -- status --------------------------------------------------------------

    async def status(self) -> dict:
        # Never block on connecting: report "connecting" and let the dashboard poll again.
        connected = self._client.is_connected()
        if not connected:
            self._kick_connect()
        authorized = False
        if connected:
            try:
                authorized = await self._client.is_user_authorized()
                if authorized and self._me is None:
                    self._me = await self._client.get_me()
            except (OSError, asyncio.TimeoutError):
                connected = False
            except errors.RPCError as e:
                log.warning("status check failed: %s", type(e).__name__)
        account = None
        if authorized and self._me is not None:
            me = self._me
            account = {
                "id": me.id,
                "name": utils.get_display_name(me),
                "username": me.username,
                "phone": _mask_phone(me.phone),
            }
        return {
            "telegram_reachable": connected,
            # The last real failure while we retry; "Connecting…" only before any attempt failed.
            "error": None if connected else (self._connect_error or "Connecting to Telegram…"),
            "authorized": authorized,
            "account": account,
            "login_step": self._login["step"] if (self._login and not authorized) else None,
            "login_phone": _mask_phone(self._login["phone"]) if (self._login and not authorized) else None,
        }

    # -- login ---------------------------------------------------------------

    async def send_code(self, phone: str) -> dict:
        async with self._lock:
            await self._require_connection()
            if await self._client.is_user_authorized():
                raise ServiceError("already_authorized", "Already connected. Disconnect first.", 409)
            phone = utils.parse_phone(phone)
            if not phone:
                raise ServiceError("phone_invalid", "Enter your phone number in international format, e.g. +15551234567.")
            try:
                sent = await self._client(functions.auth.SendCodeRequest(
                    phone, self._cfg.api_id, self._cfg.api_hash, types.CodeSettings()))
            except errors.PhoneNumberInvalidError:
                raise ServiceError("phone_invalid", "Telegram says this phone number is invalid.") from None
            except errors.PhoneNumberBannedError:
                raise ServiceError("phone_banned", "Telegram says this phone number is banned.") from None
            except errors.PhoneNumberFloodError:
                raise ServiceError("rate_limited", "Too many code requests. Wait a while (often hours) and try again.", 429) from None
            except _FLOOD_ERRORS as e:
                raise ServiceError("rate_limited", f"Telegram asks you to wait {e.seconds} seconds before requesting a code.", 429) from None
            except errors.ApiIdInvalidError:
                raise ServiceError("api_id_invalid", "Telegram rejected the API ID / hash. Run setup again with the values from my.telegram.org.") from None
            except errors.RPCError as e:
                raise ServiceError("telegram_error", f"Telegram error: {e.message}") from None

            if isinstance(sent, types.auth.SentCodePaymentRequired):
                raise ServiceError("payment_required", "Telegram requires a paid login for this number. Log in with an official app first.")
            if isinstance(sent, types.auth.SentCodeSuccess):
                self._login = None
                self._me = await self._client.get_me()
                return {"step": "done"}
            if isinstance(sent.type, types.auth.SentCodeTypeSetUpEmailRequired):
                raise ServiceError("email_setup_required", "Telegram requires a login email for this account. Set one up in the official app, then try again.")
            self._login = {"phone": phone, "hash": sent.phone_code_hash, "step": "code"}
            return {"step": "code", "delivery": _code_delivery(sent)}

    async def submit_code(self, code: str) -> dict:
        async with self._lock:
            login = self._login
            if not login or login["step"] != "code":
                raise ServiceError("no_login", "Request a login code first.", 409)
            await self._require_connection()
            try:
                self._me = await self._client.sign_in(phone=login["phone"], code=code, phone_code_hash=login["hash"])
            except errors.SessionPasswordNeededError:
                login["step"] = "password"
                return {"step": "password", "hint": await self._password_hint()}
            except (errors.PhoneCodeInvalidError, errors.PhoneCodeEmptyError):
                raise ServiceError("code_invalid", "That code is not correct. Check it and try again.") from None
            except errors.PhoneCodeExpiredError:
                self._login = None
                raise ServiceError("code_expired", "The code expired. Request a new one.") from None
            except errors.PhoneNumberUnoccupiedError:
                self._login = None
                raise ServiceError("no_account", "There is no Telegram account for this number. Sign up in the official app first.") from None
            except _FLOOD_ERRORS as e:
                raise ServiceError("rate_limited", f"Too many attempts. Wait {e.seconds} seconds.", 429) from None
            except errors.RPCError as e:
                raise ServiceError("telegram_error", f"Telegram error: {e.message}") from None
            self._finish_login()
            return {"step": "done"}

    async def submit_password(self, password: str) -> dict:
        async with self._lock:
            login = self._login
            if not login or login["step"] != "password":
                raise ServiceError("no_login", "Two-step verification is not pending.", 409)
            await self._require_connection()
            try:
                self._me = await self._client.sign_in(password=password)
            except errors.PasswordHashInvalidError:
                raise ServiceError("password_invalid", "Wrong two-step verification password.") from None
            except errors.PhonePasswordFloodError:
                raise ServiceError("rate_limited", "Too many password attempts. Wait a while and try again.", 429) from None
            except _FLOOD_ERRORS as e:
                raise ServiceError("rate_limited", f"Too many attempts. Wait {e.seconds} seconds.", 429) from None
            except errors.RPCError as e:
                raise ServiceError("telegram_error", f"Telegram error: {e.message}") from None
            self._finish_login()
            return {"step": "done"}

    async def _password_hint(self) -> str | None:
        try:
            pwd = await self._client(functions.account.GetPasswordRequest())
            return pwd.hint or None
        except errors.RPCError:
            return None

    def _finish_login(self):
        self._login = None
        self._client._phone_code_hash.clear()
        make_private(Path(str(session_path(self._data_dir)) + ".session"))
        log.info("logged in")

    async def cancel_login(self) -> None:
        async with self._lock:
            self._login = None
            self._client._phone_code_hash.clear()

    async def log_out(self) -> dict:
        async with self._lock:
            remote_ok = False
            try:
                if await self._ensure_connected() and await self._client.is_user_authorized():
                    remote_ok = await self._client.log_out()  # also deletes the session file
            except (OSError, asyncio.TimeoutError, errors.RPCError) as e:
                log.warning("remote logout failed: %s", type(e).__name__)
            finally:
                await self._client.disconnect()
                # Whatever Telegram answered, the local session must be gone.
                Path(str(session_path(self._data_dir)) + ".session").unlink(missing_ok=True)
                Path(str(session_path(self._data_dir)) + ".session-journal").unlink(missing_ok=True)
                self._me = None
                self._login = None
                self._entries.clear()
                (self._data_dir / SCAN_CACHE).unlink(missing_ok=True)
                self._client = self._client_factory()
                self._kick_connect()
            log.info("logged out (telegram confirmed: %s)", remote_ok)
            return {"telegram_confirmed": bool(remote_ok)}

    # -- dialogs -------------------------------------------------------------

    async def load_dialogs(self) -> DialogList:
        async with self._lock:
            await self._require_authorized()
            try:
                return await self._fetch_dialogs()
            except ServiceError:
                raise
            except _FLOOD_ERRORS as e:
                raise ServiceError("rate_limited",
                                   f"Telegram asks to wait {e.seconds} seconds before loading the list again.", 429) from None
            except (OSError, asyncio.TimeoutError):
                raise ServiceError("telegram_unreachable", "Lost the connection to Telegram while loading. Try again.", 503) from None
            except Exception as e:
                log.exception("loading the chat list failed")
                raise ServiceError("load_failed", f"Couldn't load your chats ({type(e).__name__}: {e}). "
                                   "The service window has the details.", 500) from None

    async def _fetch_dialogs(self) -> DialogList:
        items: list[DialogItem] = []
        entries: dict[str, _Entry] = {}
        excluded = {"private_chat": 0, "saved_messages": 0, "unreadable": 0}
        cache = self._read_scan_cache()
        folder_names = await self._folder_map()
        for folder in (0, 1):  # main list, then Archive
            async for raw, entity, message in self._iter_folder(folder):
                try:
                    item = classify(entity, last_date=getattr(message, "date", None), archived=folder == 1,
                                    unread_count=raw.unread_count,
                                    muted=_is_muted(getattr(raw, "notify_settings", None)),
                                    last_is_mine=bool(getattr(message, "out", False)))
                    if isinstance(item, str):
                        excluded[item] += 1
                        continue
                    if item.id in entries:
                        continue
                    try:
                        peer = utils.get_input_peer(entity)
                    except TypeError:  # e.g. a "min" channel without access hash: list it, never act on it
                        peer = None
                        item.leavable, item.accessible, item.can_post = False, False, False
                        item.reason = "Telegram did not send enough information to act on this. Reload later."
                    entry = _Entry(item=item, peer=peer, read_max_id=getattr(raw, "read_inbox_max_id", 0) or 0,
                                   is_bot=isinstance(entity, types.User) and bool(entity.bot))
                    _apply_cached_scan(entry, cache.get(item.id))
                    item.pinned = bool(getattr(raw, "pinned", False))
                    item.unread_mentions = getattr(raw, "unread_mentions_count", 0) or 0
                    item.folders = folder_names.get(item.id, [])
                    item.protected = item.id in self._protected
                except Exception:
                    # One odd chat must never break the whole list.
                    log.exception("skipped a chat that could not be read (%s)", type(entity).__name__)
                    excluded["unreadable"] += 1
                    continue
                items.append(item)
                entries[item.id] = entry
        self._entries = entries
        log.info("loaded %d chats (%s)", len(items), ", ".join(f"{k}: {v}" for k, v in excluded.items()))
        return DialogList(items=items, excluded=excluded, loaded_at=datetime.now(timezone.utc))

    async def _folder_map(self) -> dict[str, list[str]]:
        """Chat id -> names of your Telegram chat folders that list it explicitly (best effort)."""
        try:
            result = await self._call_patiently(functions.messages.GetDialogFiltersRequest())
        except Exception as e:  # folders are a hint only; never block the list on them
            log.warning("could not read your chat folders: %s", type(e).__name__)
            return {}
        # Newer layers wrap the list in messages.dialogFilters; older ones return a plain list.
        filters = getattr(result, "filters", result if isinstance(result, list) else [])
        names: dict[str, list[str]] = {}
        for f in filters or []:
            if not isinstance(f, (types.DialogFilter, types.DialogFilterChatlist)):
                continue
            title = str(getattr(f.title, "text", f.title) or "Folder")
            for peer in list(f.pinned_peers or []) + list(f.include_peers or []):
                try:
                    peer_id = str(utils.get_peer_id(peer))
                except Exception:
                    continue
                if title not in names.setdefault(peer_id, []):
                    names[peer_id].append(title)
        return names

    def set_protected(self, ids: list[str], value: bool) -> list[str]:
        found = []
        for item_id in ids:
            entry = self._entries.get(item_id)
            if value and entry is None:
                continue  # only chats in your list can be protected
            if value:
                self._protected.add(item_id)
            else:
                self._protected.discard(item_id)
            if entry:
                entry.item.protected = value
                found.append(item_id)
        save_protected(self._data_dir, self._protected)
        return found

    def clear_protected(self) -> list[str]:
        cleared = sorted(self._protected)
        self._protected.clear()
        for entry in self._entries.values():
            entry.item.protected = False
        save_protected(self._data_dir, self._protected)
        return cleared

    async def _iter_folder(self, folder: int):
        """Yield (raw dialog, entity, top message) for every dialog in one folder, page by page.

        Uses messages.getDialogs directly instead of TelegramClient.iter_dialogs so that a
        single unusual entry (the Archive folder row, a chat without entity data, a "min"
        channel) is skipped instead of aborting the whole load.
        """
        seen: set[int] = set()
        offset_date, offset_id, offset_peer = None, 0, types.InputPeerEmpty()
        for page in range(500):  # hard stop; 500 x 100 dialogs is far beyond any real account
            request = functions.messages.GetDialogsRequest(
                offset_date=offset_date, offset_id=offset_id, offset_peer=offset_peer,
                limit=DIALOG_PAGE, hash=0, exclude_pinned=page > 0, folder_id=folder)
            result = await self._call_patiently(request)
            entities = {}
            for x in list(getattr(result, "users", [])) + list(getattr(result, "chats", [])):
                try:
                    entities[utils.get_peer_id(x)] = x
                except Exception:
                    pass
            messages = {}
            for m in getattr(result, "messages", []):
                try:
                    messages[(utils.get_peer_id(m.peer_id), m.id)] = m
                except Exception:
                    pass
            new = 0
            last_with_message = None
            for d in getattr(result, "dialogs", []):
                if not isinstance(d, types.Dialog):  # e.g. dialogFolder: the "Archived chats" row
                    continue
                try:
                    peer_id = utils.get_peer_id(d.peer)
                except Exception:
                    continue
                if peer_id in seen:
                    continue
                seen.add(peer_id)
                new += 1
                entity = entities.get(peer_id)
                message = messages.get((peer_id, d.top_message))
                if message is not None and entity is not None:
                    last_with_message = (message, entity)
                if entity is None or isinstance(entity, (types.UserEmpty, types.ChatEmpty)):
                    continue
                if getattr(entity, "migrated_to", None) is not None:
                    continue  # old basic group that was upgraded: a dead copy
                yield d, entity, message
            done = (not isinstance(result, types.messages.DialogsSlice)
                    or len(getattr(result, "dialogs", [])) < DIALOG_PAGE or new == 0 or last_with_message is None)
            if done:
                return
            message, entity = last_with_message
            try:
                offset_peer = utils.get_input_peer(entity)
            except TypeError:
                return
            offset_date, offset_id = message.date, message.id
            await asyncio.sleep(DIALOG_PAGE_PAUSE)

    async def _call_patiently(self, request):
        """Send a request; wait out short flood waits (up to twice) instead of failing."""
        for attempt in range(3):
            try:
                return await self._client(request)
            except _FLOOD_ERRORS as e:
                if attempt == 2 or e.seconds > 60:
                    raise
                log.info("Telegram rate limit while loading: waiting %s s", e.seconds)
                await asyncio.sleep(e.seconds + 1)
        raise AssertionError("unreachable")

    def cached_item(self, item_id: str) -> DialogItem | None:
        entry = self._entries.get(item_id)
        return entry.item if entry else None

    # -- actions -------------------------------------------------------------

    async def perform(self, action: str, item_id: str, options: dict) -> tuple[str, str]:
        entry = self._entries.get(item_id)
        if entry is None:
            raise ActionFailed("not_eligible", "Not in the current list. Reload the list.", False)
        reason = ineligibility(entry.item, action)
        if reason or entry.peer is None:
            raise ActionFailed("not_eligible", reason or "Telegram did not send enough information to act on this.", False)
        item = entry.item
        try:
            if action == LEAVE and entry.is_bot:
                await self._delete_bot_chat(entry.peer)
                item.leavable, item.reason = False, "You deleted this chat."
                message = "Chat deleted (on your side only)."
                if options.get("block_bots"):
                    message += " " + await self._block(entry.peer)
                return "removed", message
            if action == LEAVE:
                request = leave_request(entry.peer)
                if request is None:  # defensive: only groups/channels are leavable
                    raise ActionFailed("unsupported", "Only groups, channels and bots can be removed.", False)
                await self._client(request)
                item.leavable, item.reason = False, "You left this."
                return "left", "Left (confirmed by Telegram)."
            if action == ARCHIVE:
                await self._client(functions.folders.EditPeerFoldersRequest(
                    [types.InputFolderPeer(peer=entry.peer, folder_id=1)]))
                item.archived = True
                return "archived", "Moved to Archive."
            if action == MUTE:
                await self._client(functions.account.UpdateNotifySettingsRequest(
                    peer=types.InputNotifyPeer(entry.peer),
                    settings=types.InputPeerNotifySettings(mute_until=MUTE_FOREVER)))
                item.muted = True
                return "muted", "Muted forever."
            raise ActionFailed("bad_action", "Unknown action.", False)
        except errors.UserNotParticipantError:
            item.leavable, item.reason = False, "You already left this."
            return "not_member", "Telegram says you are not a member (already left)."
        except Exception as e:
            raise _translate(e) from None

    async def _delete_bot_chat(self, peer) -> None:
        # Deletes only your copy (revoke=False). Telegram may ask for repeated calls on long histories.
        for _ in range(100):
            result = await self._client(functions.messages.DeleteHistoryRequest(
                peer=peer, max_id=0, just_clear=False, revoke=False))
            if not getattr(result, "offset", 0):
                return
        raise ActionFailed("incomplete", "Telegram kept returning more history to delete. Retry.", True)

    async def _block(self, peer) -> str:
        try:
            await self._client(functions.contacts.BlockRequest(id=peer))
            return "Bot blocked."
        except _FLOOD_ERRORS as e:
            return f"Blocking skipped: Telegram rate limit ({e.seconds} s)."
        except errors.RPCError as e:
            return f"Blocking failed: {e.message or type(e).__name__}."

    # -- engagement scan (read-only) -----------------------------------------

    async def scan(self, item_id: str) -> tuple[str, str]:
        entry = self._entries.get(item_id)
        if entry is None:
            raise ActionFailed("not_eligible", "Not in the current list. Reload the list.", False)
        item = entry.item
        if not item.accessible or item.kind == OTHER:
            raise ActionFailed("no_access", "Nothing to scan: no access to this chat.", False)
        try:
            if item.last_read_at is None and entry.read_max_id > 0:
                # The newest message at or below your read position.
                result = await self._client(functions.messages.GetHistoryRequest(
                    peer=entry.peer, offset_id=entry.read_max_id + 1, offset_date=None, add_offset=0,
                    limit=1, max_id=0, min_id=0, hash=0))
                item.last_read_at = _first_date(result)
            if item.can_post and item.my_last_message_at is None:
                result = await self._client(functions.messages.SearchRequest(
                    peer=entry.peer, q="", filter=types.InputMessagesFilterEmpty(), min_date=None, max_date=None,
                    offset_id=0, add_offset=0, limit=1, max_id=0, min_id=0, hash=0,
                    from_id=types.InputPeerSelf()))
                item.my_last_message_at = _first_date(result)
        except Exception as e:
            raise _translate(e) from None
        item.scanned_at = datetime.now(timezone.utc)
        self._remember_scan(entry)
        parts = [f"read up to {_short(item.last_read_at)}" if item.last_read_at else "no read position",
                 (f"you last posted {_short(item.my_last_message_at)}" if item.my_last_message_at else "no messages from you")
                 if item.can_post else None]
        return "scanned", "Scanned: " + ", ".join(p for p in parts if p) + "."

    def _read_scan_cache(self) -> dict:
        path = self._data_dir / SCAN_CACHE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _remember_scan(self, entry: _Entry) -> None:
        cache = self._read_scan_cache()
        item = entry.item
        cache[item.id] = {"read_max_id": entry.read_max_id, "last_read_at": iso(item.last_read_at),
                          "my_last_message_at": iso(item.my_last_message_at), "scanned_at": iso(item.scanned_at)}
        path = self._data_dir / SCAN_CACHE
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(cache), encoding="utf-8")
            make_private(tmp)
            os.replace(tmp, path)
        except OSError as e:
            log.warning("could not save scan cache: %s", type(e).__name__)


def _apply_cached_scan(entry: _Entry, cached) -> None:
    """Reuse an earlier scan while it is still valid.

    Valid while your read position is unchanged: reading or posting in the chat moves it,
    so a changed position means the stored dates could be too old and are dropped.
    """
    if not isinstance(cached, dict) or cached.get("read_max_id") != entry.read_max_id:
        return
    item = entry.item
    item.last_read_at = item.last_read_at or parse_iso(cached.get("last_read_at"))
    item.my_last_message_at = item.my_last_message_at or parse_iso(cached.get("my_last_message_at"))
    item.scanned_at = parse_iso(cached.get("scanned_at"))


def _first_date(result):
    for message in getattr(result, "messages", None) or []:
        date = _valid_date(getattr(message, "date", None))
        if date:
            return date
    return None


def _short(value: datetime) -> str:
    return value.strftime("%Y-%m-%d")


def _translate(e: Exception) -> Exception:
    """Map a Telethon/network exception to RateLimited or ActionFailed."""
    if isinstance(e, (RateLimited, ActionFailed)):
        return e
    if isinstance(e, _FLOOD_ERRORS):
        return RateLimited(e.seconds)
    if isinstance(e, errors.UserCreatorError):
        return ActionFailed("owner", "Telegram: you are the creator, so you can't leave this.", False)
    if isinstance(e, errors.ChannelPrivateError):
        return ActionFailed("no_access", "Telegram: no access (you may have been removed or banned).", False)
    if isinstance(e, (errors.ChannelInvalidError, errors.ChatIdInvalidError, errors.PeerIdInvalidError,
                      errors.ChannelPublicGroupNaError)):
        return ActionFailed("invalid", "Telegram no longer recognizes this chat. Reload the list.", False)
    if isinstance(e, errors.RPCError):
        return ActionFailed("telegram_error", f"Telegram error: {e.message or type(e).__name__}", True)
    if isinstance(e, (OSError, asyncio.TimeoutError)):
        return ActionFailed("network", "Connection problem; Telegram did not confirm. Retry to check.", True)
    return e  # programming errors propagate unchanged
