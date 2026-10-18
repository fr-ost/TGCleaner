"""In-memory stand-in for Telegram, used by `--demo` and the tests.

It never talks to Telegram. Demo login: any phone number, code 12345,
two-step password "demo".
"""

from __future__ import annotations

import random
from dataclasses import fields
from datetime import datetime, timedelta, timezone

from .models import (
    ARCHIVE, BOT, BROADCAST_GROUP, CHANNEL, GROUP, LEAVE, MUTE, OTHER, SUPERGROUP,
    ActionFailed, DialogItem, DialogList, RateLimited, ServiceError, ineligibility,
)

DEMO_CODE = "12345"
DEMO_PASSWORD = "demo"
OWNER_REASON = ("You own this. Transfer ownership or delete it in the Telegram app; "
                "this tool does not leave chats you created.")

_WORDS = ("Crypto", "Python", "Travel", "Startup", "Design", "News", "Music", "Gaming", "Fitness",
          "Books", "Movies", "AI", "Photo", "Coffee", "Hiking", "Rust", "Market", "Jobs", "Local", "Memes")
_SUFFIX = ("Chat", "Community", "Hub", "Daily", "Lounge", "Club", "Talk", "Updates", "Deals", "Official")
_BOTS = ("Weather", "Translate", "Poll", "Music", "Sticker", "Vote", "Remind", "Quiz", "File", "Shop",
         "News", "Price", "Crypto", "Meme", "Job", "Feed", "Gif", "Wiki", "Notes", "Timer")


def demo_items(count: int = 320, seed: int = 7, now: datetime | None = None, bots: int = 0):
    """Return (items, truth). `truth[id]` holds the engagement dates only a scan can discover."""
    rnd = random.Random(seed)
    now = now or datetime.now(timezone.utc)
    ago = lambda days: now - timedelta(days=days)  # noqa: E731
    items, truth = [], {}
    for i in range(count + bots):
        is_bot = i >= count
        kind = BOT if is_bot else rnd.choices([CHANNEL, SUPERGROUP, GROUP, BROADCAST_GROUP], [40, 40, 17, 3])[0]
        last = None if rnd.random() < 0.08 else ago(rnd.choice(
            [rnd.uniform(0, 7), rnd.uniform(7, 60), rnd.uniform(60, 400), rnd.uniform(400, 1100)]))
        role = "none" if is_bot else rnd.choices(["member", "admin", "owner"], [88, 8, 4])[0]
        if kind == CHANNEL and role == "member":
            role = "subscriber"
        reason, accessible = None, True
        if role == "owner":
            reason = OWNER_REASON
        elif not is_bot and rnd.random() < 0.02:
            reason, role, accessible = "You were banned or this is no longer accessible.", "none", False
        can_post = accessible and (kind != CHANNEL or role in ("owner", "admin"))

        # Engagement: some chats you keep up with, others you ignore although they're busy.
        ignored = last is not None and rnd.random() < 0.45
        unread = rnd.choice([40, 180, 950, 3200]) if ignored else 0
        real_read = ago(rnd.uniform(60, 700)) if ignored else last
        if ignored and real_read > last:
            real_read = last - timedelta(days=1)
        real_mine = None
        if can_post and rnd.random() < (0.9 if is_bot else 0.5):
            anchor = real_read or ago(900)
            real_mine = anchor - timedelta(days=rnd.uniform(0, 300))
        last_is_mine = real_mine is not None and last is not None and not ignored and rnd.random() < 0.2
        if last_is_mine:
            real_mine = last
        muted = rnd.random() < (0.55 if ignored else 0.1)

        if is_bot:
            title = f"{_BOTS[(i - count) % len(_BOTS)]}{'' if i - count < len(_BOTS) else i - count} Bot"
            username = title.replace(" ", "").lower()
        else:
            title = f"{rnd.choice(_WORDS)} {rnd.choice(_SUFFIX)} {i + 1}"
            username = title.lower().replace(" ", "_") if rnd.random() < 0.5 else None
        base = 1_000_000_000 + i
        item_id = str(base) if is_bot else (f"-{base}" if kind == GROUP else f"-100{base}")
        items.append(DialogItem(
            id=item_id, title=title, kind=kind, username=username, last_message_at=last, role=role,
            archived=rnd.random() < 0.1, unread_count=unread,
            participants_count=None if is_bot else rnd.randint(3, 90_000),
            leavable=reason is None, reason=reason, muted=muted, accessible=accessible, can_post=can_post,
            last_read_at=last if unread == 0 else None,
            my_last_message_at=last if last_is_mine else None,
        ))
        truth[item_id] = (real_read, real_mine)
    return items, truth


def _copy(item: DialogItem) -> DialogItem:
    return DialogItem(**{f.name: getattr(item, f.name) for f in fields(item)})


class FakeBackend:
    demo = True

    def __init__(self, items: list[DialogItem] | None = None, truth: dict | None = None, *,
                 require_password: bool = True, authorized: bool = False,
                 script: dict | None = None, scan_script: dict | None = None, random_failures: bool = False):
        """`script` / `scan_script` map item id -> list of outcomes consumed one per attempt:
        "ok", "not_member", ("flood", seconds), ("fail", code, retryable), "network".
        With `random_failures`, a few unscripted items fail like real ones would (demo mode)."""
        if items is None:
            items, truth = demo_items(bots=24)
        self._all = items
        self._truth = truth or {}
        self._gone: set[str] = set()     # left / deleted
        self._state: dict[str, dict] = {}  # archived / muted / scan results that survive reloads
        self._require_password = require_password
        self._authorized = authorized
        self._login_step: str | None = None
        self._entries: dict[str, DialogItem] = {}
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.scan_script = {k: list(v) for k, v in (scan_script or {}).items()}
        self._random_failures = random_failures
        self._count = 0
        self.calls: list[tuple[str, str]] = []  # (action, item id), including scans
        self.blocked: list[str] = []

    @property
    def leave_calls(self) -> list[str]:
        return [item_id for action, item_id in self.calls if action == LEAVE]

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
        for original in self._all:
            if original.id in self._gone and original.kind in (BOT, CHANNEL, SUPERGROUP, BROADCAST_GROUP):
                continue  # like Telegram: left channels and deleted chats disappear from the list
            item = _copy(original)
            for key, value in self._state.get(item.id, {}).items():
                setattr(item, key, value)
            if item.id in self._gone:
                item.leavable, item.reason, item.can_post = False, "You already left this group.", False
            items.append(item)
        self._entries = {it.id: it for it in items}
        return DialogList(items=items, excluded={"private_chat": 41, "saved_messages": 1},
                          loaded_at=datetime.now(timezone.utc))

    def cached_item(self, item_id: str) -> DialogItem | None:
        return self._entries.get(item_id)

    def _remember(self, item_id: str, **values) -> None:
        self._state.setdefault(item_id, {}).update(values)
        item = self._entries.get(item_id)
        for key, value in values.items():
            setattr(item, key, value)

    def _next_outcome(self, script: dict, item_id: str):
        steps = script.get(item_id)
        if steps:
            return steps.pop(0)
        self._count += 1
        if not self._random_failures:
            return "ok"
        n = self._count
        if n == 25:
            return ("flood", 6)        # short wait: the job waits it out and continues
        if n % 17 == 0:
            return "network"           # retryable
        if n % 29 == 0:
            return ("fail", "channel_private", False)
        return "ok"

    @staticmethod
    def _raise_for(outcome) -> None:
        if outcome == "network":
            raise ActionFailed("network", "Connection problem; Telegram did not confirm. Retry to check.", True)
        if isinstance(outcome, tuple) and outcome[0] == "flood":
            raise RateLimited(outcome[1])
        if isinstance(outcome, tuple) and outcome[0] == "fail":
            raise ActionFailed(outcome[1], f"Telegram error: {outcome[1].upper()}", outcome[2])
        if outcome not in ("ok", "not_member"):
            raise AssertionError(f"bad script outcome {outcome!r}")

    async def perform(self, action: str, item_id: str, options: dict) -> tuple[str, str]:
        self.calls.append((action, item_id))
        item = self._entries.get(item_id)
        if item is None:
            raise ActionFailed("not_eligible", "Not in the current list. Reload the list.", False)
        reason = ineligibility(item, action)
        if reason:
            raise ActionFailed("not_eligible", reason, False)
        outcome = self._next_outcome(self.script, item_id)
        self._raise_for(outcome)
        if action == LEAVE:
            self._gone.add(item_id)
            if outcome == "not_member":
                item.leavable, item.reason = False, "You already left this."
                return "not_member", "Telegram says you are not a member (already left)."
            if item.kind == BOT:
                item.leavable, item.reason = False, "You deleted this chat."
                message = "Chat deleted (on your side only)."
                if options.get("block_bots"):
                    self.blocked.append(item_id)
                    message += " Bot blocked."
                return "removed", message
            item.leavable, item.reason = False, "You left this."
            return "left", "Left (confirmed by Telegram)."
        if action == ARCHIVE:
            self._remember(item_id, archived=True)
            return "archived", "Moved to Archive."
        if action == MUTE:
            self._remember(item_id, muted=True)
            return "muted", "Muted forever."
        raise ActionFailed("bad_action", "Unknown action.", False)

    async def scan(self, item_id: str) -> tuple[str, str]:
        self.calls.append(("scan", item_id))
        item = self._entries.get(item_id)
        if item is None:
            raise ActionFailed("not_eligible", "Not in the current list. Reload the list.", False)
        if not item.accessible or item.kind == OTHER:
            raise ActionFailed("no_access", "Nothing to scan: no access to this chat.", False)
        self._raise_for(self._next_outcome(self.scan_script, item_id))
        real_read, real_mine = self._truth.get(item_id, (None, None))
        self._remember(item_id,
                       last_read_at=item.last_read_at or real_read,
                       my_last_message_at=item.my_last_message_at or (real_mine if item.can_post else None),
                       scanned_at=datetime.now(timezone.utc))
        return "scanned", "Scanned."
