"""In-memory stand-in for Telegram, used by `--demo` and the tests.

It never talks to Telegram. Demo login: any phone number, code 12345,
two-step password "demo".
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from .models import (
    BROADCAST_GROUP, CHANNEL, GROUP, SUPERGROUP,
    DialogItem, DialogList, LeaveFailed, NotMember, RateLimited, ServiceError,
)

DEMO_CODE = "12345"
DEMO_PASSWORD = "demo"

_WORDS = ("Crypto", "Python", "Travel", "Startup", "Design", "News", "Music", "Gaming", "Fitness",
          "Books", "Movies", "AI", "Photo", "Coffee", "Hiking", "Rust", "Market", "Jobs", "Local", "Memes")
_SUFFIX = ("Chat", "Community", "Hub", "Daily", "Lounge", "Club", "Talk", "Updates", "Deals", "Official")


def demo_items(count: int = 320, seed: int = 7, now: datetime | None = None) -> list[DialogItem]:
    rnd = random.Random(seed)
    now = now or datetime.now(timezone.utc)
    items = []
    for i in range(count):
        kind = rnd.choices([CHANNEL, SUPERGROUP, GROUP, BROADCAST_GROUP], [40, 40, 17, 3])[0]
        r = rnd.random()
        if r < 0.08:
            last = None  # no history available
        else:
            # Spread from minutes to ~3 years ago, skewed to old.
            days = rnd.choice([rnd.uniform(0, 7), rnd.uniform(7, 60), rnd.uniform(60, 400), rnd.uniform(400, 1100)])
            last = now - timedelta(days=days)
        role = rnd.choices(["member", "admin", "owner"], [88, 8, 4])[0]
        if kind == CHANNEL and role == "member":
            role = "subscriber"
        reason = None
        if role == "owner":
            reason = "You own this. Transfer ownership or delete it in the Telegram app; this tool does not leave chats you created."
        elif rnd.random() < 0.02:
            reason = "You were banned or this is no longer accessible."
            role = "none"
        title = f"{rnd.choice(_WORDS)} {rnd.choice(_SUFFIX)} {i + 1}"
        username = title.lower().replace(" ", "_") if rnd.random() < 0.5 else None
        base = 1_000_000_000 + i
        item_id = f"-{base}" if kind == GROUP else f"-100{base}"
        items.append(DialogItem(
            id=item_id, title=title, kind=kind, username=username, last_message_at=last, role=role,
            archived=rnd.random() < 0.1, unread_count=rnd.choice([0, 0, 0, 3, 12, 140]),
            participants_count=rnd.randint(3, 90_000), leavable=reason is None, reason=reason,
        ))
    return items


class FakeBackend:
    demo = True

    def __init__(self, items: list[DialogItem] | None = None, *, require_password: bool = True,
                 authorized: bool = False, script: dict | None = None, random_failures: bool = False):
        """`script` maps item id -> list of outcomes consumed one per leave attempt:
        "ok", "not_member", ("flood", seconds), ("fail", code, retryable), "network".
        With `random_failures`, a few unscripted items fail like real ones would (demo mode)."""
        self._all = items if items is not None else demo_items()
        self._members = {it.id for it in self._all}
        self._require_password = require_password
        self._authorized = authorized
        self._login_step: str | None = None
        self._entries: dict[str, DialogItem] = {}
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self._random_failures = random_failures
        self._leave_count = 0
        self.leave_calls: list[str] = []
        self.sent_codes: list[str] = []

    async def start(self):
        pass

    async def stop(self):
        pass

    async def status(self) -> dict:
        return {
            "telegram_reachable": True,
            "error": None,
            "authorized": self._authorized,
            "account": {"id": 777000, "name": "Demo User", "username": "demo_user", "phone": "+99•••••01"}
            if self._authorized else None,
            "login_step": None if self._authorized else self._login_step,
            "login_phone": "+99•••••01" if self._login_step else None,
        }

    async def send_code(self, phone: str) -> dict:
        if self._authorized:
            raise ServiceError("already_authorized", "Already connected. Disconnect first.", 409)
        self.sent_codes.append(phone)
        self._login_step = "code"
        return {"step": "code", "delivery": f"demo mode: enter {DEMO_CODE}"}

    async def submit_code(self, code: str) -> dict:
        if self._login_step != "code":
            raise ServiceError("no_login", "Request a login code first.", 409)
        if code != DEMO_CODE:
            raise ServiceError("code_invalid", "That code is not correct. Check it and try again.")
        if self._require_password:
            self._login_step = "password"
            return {"step": "password", "hint": f"demo: {DEMO_PASSWORD}"}
        self._login_step = None
        self._authorized = True
        return {"step": "done"}

    async def submit_password(self, password: str) -> dict:
        if self._login_step != "password":
            raise ServiceError("no_login", "Two-step verification is not pending.", 409)
        if password != DEMO_PASSWORD:
            raise ServiceError("password_invalid", "Wrong two-step verification password.")
        self._login_step = None
        self._authorized = True
        return {"step": "done"}

    async def cancel_login(self) -> None:
        self._login_step = None

    async def log_out(self) -> dict:
        self._authorized = False
        self._login_step = None
        self._entries.clear()
        return {"telegram_confirmed": True}

    async def load_dialogs(self) -> DialogList:
        if not self._authorized:
            raise ServiceError("not_authorized", "Connect your Telegram account first.", 401)
        items = []
        for it in self._all:
            copy = DialogItem(**{k: getattr(it, k) for k in it.__dataclass_fields__})
            if it.id not in self._members and copy.leavable:
                copy.leavable, copy.reason = False, "You already left this."
            items.append(copy)
        self._entries = {it.id: it for it in items}
        return DialogList(items=items, excluded={"private_chat": 41, "bot": 6, "saved_messages": 1},
                          loaded_at=datetime.now(timezone.utc))

    def cached_item(self, item_id: str) -> DialogItem | None:
        return self._entries.get(item_id)

    def mark_left(self, item_id: str, note: str) -> None:
        item = self._entries.get(item_id)
        if item:
            item.leavable, item.reason = False, note

    async def leave(self, item_id: str) -> None:
        self.leave_calls.append(item_id)
        item = self._entries.get(item_id)
        if item is None or not item.leavable:
            raise LeaveFailed("not_eligible", "Not in the current list or not leavable. Reload the list.", False)
        steps = self.script.get(item_id)
        outcome = steps.pop(0) if steps else self._unscripted_outcome()
        if outcome == "ok":
            self._members.discard(item_id)
            return
        if outcome == "not_member":
            self._members.discard(item_id)
            raise NotMember()
        if outcome == "network":
            raise LeaveFailed("network", "Connection problem; Telegram did not confirm. Retry to check.", True)
        if outcome[0] == "flood":
            raise RateLimited(outcome[1])
        if outcome[0] == "fail":
            raise LeaveFailed(outcome[1], f"Telegram error: {outcome[1].upper()}", outcome[2])
        raise AssertionError(f"bad script outcome {outcome!r}")

    def _unscripted_outcome(self):
        self._leave_count += 1
        if not self._random_failures:
            return "ok"
        n = self._leave_count
        if n == 25:
            return ("flood", 6)        # short wait: the job waits it out and continues
        if n % 17 == 0:
            return "network"           # retryable
        if n % 29 == 0:
            return ("fail", "channel_private", False)
        return "ok"
