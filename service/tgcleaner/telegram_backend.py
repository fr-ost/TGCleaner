"""Telegram access through Telethon (MTProto, user account).

Telegram operations used (verified against Telethon 1.45 / API layer 229):
  login     auth.sendCode -> auth.signIn -> (2FA) account.getPassword + auth.checkPassword
  list      messages.getDialogs via TelegramClient.iter_dialogs (all folders)
  activity  date of each dialog's top (most recent) message; absent when there is no history
  leave     channels.leaveChannel for channels/supergroups,
            messages.deleteChatUser(chat_id, inputUserSelf) for basic groups
  logout    auth.logOut, then the local session file is deleted

We deliberately do not use TelegramClient.delete_dialog(): given a user it would
delete a private conversation. Leaving here can only ever target a channel or a
basic group that was listed, classified as leavable and confirmed.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient, errors, functions, types, utils

from . import __version__
from .config import Config, make_private, session_path
from .models import (
    BROADCAST_GROUP, CHANNEL, GROUP, OTHER, SUPERGROUP,
    DialogItem, DialogList, LeaveFailed, NotMember, RateLimited, ServiceError,
)

log = logging.getLogger("tgcleaner.telegram")

# Before Telegram existed: a date like this means "no real date".
_MIN_VALID_DATE = datetime(2013, 1, 1, tzinfo=timezone.utc)

OWNER_REASON = ("You own this. Transfer ownership or delete it in the Telegram app; "
                "this tool does not leave chats you created.")

CONNECT_TIMEOUT = 45  # seconds for one complete connect attempt

# Errors that carry a wait time (FLOOD_WAIT_X, FLOOD_PREMIUM_WAIT_X, ...).
_FLOOD_ERRORS = (errors.FloodWaitError, errors.FloodPremiumWaitError)


# ---------------------------------------------------------------------------
# Classification (pure: unit tested with real Telethon types, no network)
# ---------------------------------------------------------------------------

def classify(entity, *, last_date, archived: bool, unread_count: int):
    """Turn one dialog's entity into a DialogItem.

    Returns a string category ("private_chat", "bot", "saved_messages") for
    dialogs that must never be listed.
    """
    if isinstance(entity, types.User):
        if entity.is_self:
            return "saved_messages"
        return "bot" if entity.bot else "private_chat"

    peer_id = str(utils.get_peer_id(entity))
    title = getattr(entity, "title", None) or "(untitled)"
    last = _valid_date(last_date)
    common = dict(id=peer_id, title=title, last_message_at=last, archived=archived,
                  unread_count=unread_count or 0)

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
        return DialogItem(kind=kind, username=_username(entity), role=role,
                          participants_count=entity.participants_count,
                          leavable=reason is None, reason=reason, **common)

    if isinstance(entity, types.ChannelForbidden):
        kind = SUPERGROUP if entity.megagroup else CHANNEL
        return DialogItem(kind=kind, username=None, role="none", participants_count=None,
                          leavable=False, reason="You were banned or this is no longer accessible.", **common)

    if isinstance(entity, types.Chat):
        reason = None
        if entity.deactivated or entity.migrated_to is not None:
            reason = "This group was upgraded to a supergroup. Leave the supergroup instead."
        elif entity.left:
            reason = "You already left this group."
        elif entity.creator:
            reason = OWNER_REASON
        return DialogItem(kind=GROUP, username=None, role=_role(entity, broadcast=False),
                          participants_count=entity.participants_count,
                          leavable=reason is None, reason=reason, **common)

    if isinstance(entity, types.ChatForbidden):
        return DialogItem(kind=GROUP, username=None, role="none", participants_count=None,
                          leavable=False, reason="You were removed from this group.", **common)

    # Communities and any entity type added by a future Telegram layer.
    return DialogItem(kind=OTHER, username=None, role="unknown", participants_count=None,
                      leavable=False,
                      reason=f"{type(entity).__name__} is not supported by this tool. Use the Telegram app.",
                      **common)


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
        self._entries: dict[str, tuple[DialogItem, object]] = {}

    def _make_client(self) -> TelegramClient:
        return TelegramClient(
            str(session_path(self._data_dir)), self._cfg.api_id, self._cfg.api_hash,
            # Never sleep silently: every flood wait surfaces to us so the
            # dashboard can show it and the leave job can decide what to do.
            flood_sleep_threshold=0,
            raise_last_call_error=True,
            connection_retries=2,
            receive_updates=False,
            device_model="TG Group Cleaner (personal)",
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
                self._client = self._client_factory()
                self._kick_connect()
            log.info("logged out (telegram confirmed: %s)", remote_ok)
            return {"telegram_confirmed": bool(remote_ok)}

    # -- dialogs -------------------------------------------------------------

    async def load_dialogs(self) -> DialogList:
        async with self._lock:
            await self._require_authorized()
            for attempt in range(2):
                try:
                    return await self._fetch_dialogs()
                except _FLOOD_ERRORS as e:
                    if attempt == 0 and e.seconds <= 30:
                        log.info("dialog list rate limited, waiting %s s", e.seconds)
                        await asyncio.sleep(e.seconds + 1)
                        continue
                    raise ServiceError("rate_limited",
                                       f"Telegram asks to wait {e.seconds} seconds before loading the list again.", 429) from None
                except (OSError, asyncio.TimeoutError):
                    raise ServiceError("telegram_unreachable", "Lost the connection to Telegram while loading. Try again.", 503) from None

    async def _fetch_dialogs(self) -> DialogList:
        items: list[DialogItem] = []
        entries: dict[str, tuple[DialogItem, object]] = {}
        excluded = {"private_chat": 0, "bot": 0, "saved_messages": 0}
        # ignore_migrated hides old basic groups that were upgraded (they are dead copies).
        async for dialog in self._client.iter_dialogs(ignore_migrated=True):
            item = classify(dialog.entity, last_date=dialog.date, archived=dialog.archived,
                            unread_count=dialog.unread_count)
            if isinstance(item, str):
                excluded[item] += 1
                continue
            if item.id in entries:
                continue
            items.append(item)
            entries[item.id] = (item, dialog.input_entity)
        self._entries = entries
        return DialogList(items=items, excluded=excluded, loaded_at=datetime.now(timezone.utc))

    def cached_item(self, item_id: str) -> DialogItem | None:
        entry = self._entries.get(item_id)
        return entry[0] if entry else None

    def mark_left(self, item_id: str, note: str) -> None:
        entry = self._entries.get(item_id)
        if entry:
            entry[0].leavable = False
            entry[0].reason = note

    # -- leave ---------------------------------------------------------------

    async def leave(self, item_id: str) -> None:
        entry = self._entries.get(item_id)
        if entry is None or not entry[0].leavable:
            raise LeaveFailed("not_eligible", "Not in the current list or not leavable. Reload the list.", False)
        request = leave_request(entry[1])
        if request is None:  # defensive: classify() never marks users leavable
            raise LeaveFailed("unsupported", "Only groups and channels can be left.", False)
        try:
            await self._client(request)
        except _FLOOD_ERRORS as e:
            raise RateLimited(e.seconds) from None
        except errors.UserNotParticipantError:
            raise NotMember() from None
        except errors.UserCreatorError:
            raise LeaveFailed("owner", "Telegram: you are the creator, so you can't leave this.", False) from None
        except errors.ChannelPrivateError:
            raise LeaveFailed("no_access", "Telegram: no access (you may have been removed or banned).", False) from None
        except (errors.ChannelInvalidError, errors.ChatIdInvalidError, errors.PeerIdInvalidError,
                errors.ChannelPublicGroupNaError):
            raise LeaveFailed("invalid", "Telegram no longer recognizes this chat. Reload the list.", False) from None
        except errors.RPCError as e:
            raise LeaveFailed("telegram_error", f"Telegram error: {e.message or type(e).__name__}", True) from None
        except (OSError, asyncio.TimeoutError):
            raise LeaveFailed("network", "Connection problem; Telegram did not confirm. Retry to check.", True) from None
