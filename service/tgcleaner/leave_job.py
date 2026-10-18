"""Sequential bulk-leave job.

Rules:
  * One item at a time, with a pause between requests.
  * An item is "left" only after Telegram answered the leave request without error.
  * FLOOD_WAIT: wait it out once per item if it is short; otherwise stop the batch
    and mark everything not yet attempted as "rate_limited" (retryable later).
    Telethon additionally refuses to resend a flood-waited request early.
  * One failure never stops the batch.
  * Cancelling takes effect between items; a request already sent is never abandoned,
    because then we could not know whether Telegram applied it.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field

from .models import Backend, LeaveFailed, NotMember, RateLimited

log = logging.getLogger("tgcleaner.leave")

# Item statuses
PENDING = "pending"
IN_PROGRESS = "in_progress"
LEFT = "left"                  # Telegram confirmed the leave
NOT_MEMBER = "not_member"      # Telegram says we are not a participant (already left)
FAILED = "failed"
RATE_LIMITED = "rate_limited"  # not attempted (or not completed) because of a long flood wait
CANCELLED = "cancelled"        # not attempted because you cancelled

DONE_STATUSES = {LEFT, NOT_MEMBER}

# Job states
RUNNING = "running"
WAITING = "waiting"            # sleeping through a flood wait
FINISHED = "finished"
STOPPED_RATE_LIMIT = "stopped_rate_limited"
CANCELLED_JOB = "cancelled"


@dataclass
class ItemResult:
    id: str
    title: str
    kind: str
    status: str = PENDING
    message: str | None = None
    retryable: bool = False
    finished_at: float | None = None

    def to_json(self) -> dict:
        return {
            "id": self.id, "title": self.title, "kind": self.kind, "status": self.status,
            "message": self.message, "retryable": self.retryable, "finished_at": self.finished_at,
        }


@dataclass
class LeaveJob:
    items: list[ItemResult]
    delay: float
    max_auto_wait: int
    id: str = field(default_factory=lambda: secrets.token_hex(8))
    state: str = RUNNING
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    wait_until: float | None = None
    retry_after: float | None = None
    _cancel: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def active(self) -> bool:
        return self.state in (RUNNING, WAITING)

    def cancel(self) -> None:
        self._cancel.set()

    def counts(self) -> dict:
        out: dict[str, int] = {}
        for it in self.items:
            out[it.status] = out.get(it.status, 0) + 1
        return out

    def to_json(self) -> dict:
        processed = sum(1 for it in self.items if it.status not in (PENDING, IN_PROGRESS))
        return {
            "id": self.id,
            "state": self.state,
            "total": len(self.items),
            "processed": processed,
            "counts": self.counts(),
            "wait_until": self.wait_until,
            "retry_after": self.retry_after,
            "cancel_requested": self._cancel.is_set(),
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "items": [it.to_json() for it in self.items],
        }

    async def _pause(self, seconds: float) -> bool:
        """Sleep, waking early on cancel. Returns False if cancelled."""
        if seconds <= 0:
            return not self._cancel.is_set()
        try:
            await asyncio.wait_for(self._cancel.wait(), timeout=seconds)
            return False
        except asyncio.TimeoutError:
            return True

    async def run(self, backend: Backend) -> None:
        try:
            await self._run(backend)
        except Exception:  # never leave a job stuck in "running"
            log.exception("leave job crashed")
            for it in self.items:
                if it.status in (PENDING, IN_PROGRESS):
                    it.status = FAILED
                    it.message = "Internal error; this item was not confirmed. Reload the list and retry."
                    it.retryable = True
            self.state = FINISHED
        finally:
            self.wait_until = None
            self.finished_at = time.time()

    async def _run(self, backend: Backend) -> None:
        for index, item in enumerate(self.items):
            if self._cancel.is_set():
                self._mark_rest(index, CANCELLED, "Not attempted: you cancelled.")
                self.state = CANCELLED_JOB
                return
            if index > 0 and not await self._pause(self.delay):
                self._mark_rest(index, CANCELLED, "Not attempted: you cancelled.")
                self.state = CANCELLED_JOB
                return

            waited = False
            while True:
                item.status = IN_PROGRESS
                try:
                    await backend.leave(item.id)
                except RateLimited as e:
                    if not waited and e.seconds <= self.max_auto_wait:
                        waited = True
                        item.status = PENDING
                        item.message = f"Telegram rate limit: waiting {e.seconds} s, then retrying."
                        self.state = WAITING
                        self.wait_until = time.time() + e.seconds
                        log.info("flood wait %s s", e.seconds)
                        ok = await self._pause(e.seconds + 1)
                        self.state = RUNNING
                        self.wait_until = None
                        if not ok:
                            self._mark_rest(index, CANCELLED, "Not attempted: you cancelled.")
                            self.state = CANCELLED_JOB
                            return
                        continue
                    self.retry_after = time.time() + e.seconds
                    when = time.strftime("%H:%M", time.localtime(self.retry_after))
                    self._mark_rest(index, RATE_LIMITED,
                                    f"Not attempted: Telegram asked to wait {e.seconds} s. Retry after {when}.")
                    self.state = STOPPED_RATE_LIMIT
                    return
                except NotMember:
                    self._finish(item, NOT_MEMBER, "Telegram says you are not a member (already left).")
                    backend.mark_left(item.id, "You already left this.")
                except LeaveFailed as e:
                    self._finish(item, FAILED, e.message, retryable=e.retryable)
                else:
                    self._finish(item, LEFT, "Left (confirmed by Telegram).")
                    backend.mark_left(item.id, "You left this.")
                break
        self.state = FINISHED

    def _finish(self, item: ItemResult, status: str, message: str, retryable: bool = False) -> None:
        item.status = status
        item.message = message
        item.retryable = retryable
        item.finished_at = time.time()

    def _mark_rest(self, start: int, status: str, message: str) -> None:
        for it in self.items[start:]:
            if it.status in (PENDING, IN_PROGRESS):
                self._finish(it, status, message, retryable=True)
