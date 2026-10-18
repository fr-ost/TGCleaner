"""Data shared by the Telegram backends, the leave job and the HTTP API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

# Item kinds shown in the dashboard.
CHANNEL = "channel"            # broadcast channel
SUPERGROUP = "supergroup"      # megagroup
BROADCAST_GROUP = "gigagroup"  # very large broadcast group
GROUP = "group"                # legacy basic group
OTHER = "other"                # anything Telegram adds later; never leavable here


@dataclass
class DialogItem:
    id: str                      # Telethon "marked" peer id, e.g. "-1001234567890"
    title: str
    kind: str
    username: str | None
    last_message_at: datetime | None  # None = no message history available to us
    role: str                    # owner | admin | member | subscriber | none
    archived: bool
    unread_count: int
    participants_count: int | None
    leavable: bool
    reason: str | None = None    # why it can't be left (when leavable is False)

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
            "unread_count": self.unread_count,
            "participants_count": self.participants_count,
            "leavable": self.leavable,
            "reason": self.reason,
        }


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class DialogList:
    items: list[DialogItem]
    excluded: dict[str, int]  # private chats, bots and Saved Messages are counted, never listed
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


class NotMember(Exception):
    """Telegram reports the account is not a participant (it already left)."""


class LeaveFailed(Exception):
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
    def mark_left(self, item_id: str, note: str) -> None: ...

    async def leave(self, item_id: str) -> None:
        """Return only after Telegram confirmed the leave.

        Raises RateLimited, NotMember or LeaveFailed otherwise.
        """
        ...
