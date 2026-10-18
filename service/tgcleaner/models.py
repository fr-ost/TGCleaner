"""Data shared by the Telegram backends, the batch jobs and the HTTP API."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

# Item kinds shown in the dashboard.
CHANNEL = "channel"            # broadcast channel
SUPERGROUP = "supergroup"      # megagroup
BROADCAST_GROUP = "gigagroup"  # very large broadcast group
GROUP = "group"                # legacy basic group
BOT = "bot"                    # chat with a bot (human private chats are never listed)
OTHER = "other"                # anything Telegram adds later; never left/removed here

# Cleanup actions.
LEAVE = "leave"      # leave a group/channel, or delete the chat with a bot
ARCHIVE = "archive"  # move to the Archive folder
MUTE = "mute"        # mute notifications forever
ACTIONS = (LEAVE, ARCHIVE, MUTE)


@dataclass
class DialogItem:
    id: str                      # Telethon "marked" peer id, e.g. "-1001234567890"
    title: str
    kind: str
    username: str | None
    last_message_at: datetime | None  # newest message in the chat; None = no history available to us
    role: str                    # owner | admin | member | subscriber | none
    archived: bool
    unread_count: int
    participants_count: int | None
    leavable: bool               # can the LEAVE action apply?
    reason: str | None = None    # why LEAVE can't apply
    muted: bool = False
    accessible: bool = True      # False when banned/removed: nothing can be done with it
    can_post: bool = True        # False for channels where you can't post (so "your last message" doesn't apply)
    # Your own engagement. None = unknown (not determinable from the list; a scan may fill it in).
    last_read_at: datetime | None = None       # date of the newest message you have read
    my_last_message_at: datetime | None = None  # date of your newest message there
    scanned_at: datetime | None = None          # when the engagement scan last looked at this item
    # Signals that a chat may matter to you (shown, filterable, used by the safety guard).
    pinned: bool = False
    folders: list[str] = field(default_factory=list)  # your Telegram chat folders that list it explicitly
    unread_mentions: int = 0
    protected: bool = False      # you marked it: no action may touch it

    @property
    def link(self) -> str | None:
        return f"https://t.me/{self.username}" if self.username else None

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "username": self.username,
            "link": self.link,
            "last_message_at": iso(self.last_message_at),
            "role": self.role,
            "archived": self.archived,
            "muted": self.muted,
            "unread_count": self.unread_count,
            "participants_count": self.participants_count,
            "leavable": self.leavable,
            "reason": self.reason,
            "accessible": self.accessible,
            "can_post": self.can_post,
            "last_read_at": iso(self.last_read_at),
            "my_last_message_at": iso(self.my_last_message_at),
            "scanned_at": iso(self.scanned_at),
            "pinned": self.pinned,
            "folders": self.folders,
            "unread_mentions": self.unread_mentions,
            "protected": self.protected,
        }


def ineligibility(item: DialogItem, action: str) -> str | None:
    """Why `action` can't be applied to `item`, or None if it can."""
    if item.protected:
        return "Protected by you. Unprotect it first if you really want to change it."
    if action == LEAVE:
        return None if item.leavable else (item.reason or "This can't be left here.")
    if not item.accessible:
        return "You no longer have access to this."
    if item.kind == OTHER:
        return "This type isn't supported by this tool."
    if action == ARCHIVE:
        return "Already archived." if item.archived else None
    if action == MUTE:
        return "Already muted." if item.muted else None
    return "Unknown action."


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class DialogList:
    items: list[DialogItem]
    excluded: dict[str, int]  # private chats and Saved Messages are counted, never listed
    loaded_at: datetime


class ServiceError(Exception):
    """An error that is safe to show to the user. Never include codes or passwords in it."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class RateLimited(Exception):
    """Telegram answered FLOOD_WAIT: nothing may be sent for `seconds`."""

    def __init__(self, seconds: int):
        super().__init__(f"Telegram rate limit: wait {seconds} s")
        self.seconds = max(1, int(seconds))


class ActionFailed(Exception):
    def __init__(self, code: str, message: str, retryable: bool):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class Backend(Protocol):
    """What the HTTP layer needs from Telegram. Implemented by TelethonBackend and FakeBackend."""

    demo: bool

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def status(self) -> dict: ...

    async def send_code(self, phone: str) -> dict: ...
    async def submit_code(self, code: str) -> dict: ...
    async def submit_password(self, password: str) -> dict: ...
    async def cancel_login(self) -> None: ...
    async def log_out(self) -> dict: ...

    async def load_dialogs(self) -> DialogList: ...
    def cached_item(self, item_id: str) -> DialogItem | None: ...

    async def perform(self, action: str, item_id: str, options: dict) -> tuple[str, str]:
        """Apply one action. Returns (status, message) only after Telegram confirmed it.

        Raises RateLimited or ActionFailed otherwise.
        """
        ...

    def set_protected(self, ids: list[str], value: bool) -> list[str]:
        """Mark/unmark items as protected (persisted). Returns the ids that were found."""
        ...

    def clear_protected(self) -> list[str]:
        """Remove every protection (the whole saved list). Returns the ids that were protected."""
        ...

    async def scan(self, item_id: str) -> tuple[str, str]:
        """Read-only: look up your read position and your last message for one item."""
        ...
