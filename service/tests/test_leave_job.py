"""Bulk leave behaviour: pacing, partial failures, flood waits, cancel. Uses FakeBackend only."""

import asyncio
import unittest

from tgcleaner import leave_job as lj
from tgcleaner.fake_backend import FakeBackend, demo_items
from tgcleaner.leave_job import ItemResult, LeaveJob


class FastJob(LeaveJob):
    """Records requested pauses instead of sleeping (cancel still wins)."""

    @property
    def pauses(self) -> list:
        return self.__dict__.setdefault("_pauses", [])

    async def _pause(self, seconds):
        self.pauses.append(seconds)
        await asyncio.sleep(0)
        return not self._cancel.is_set()


async def make(n=10, script=None):
    items = [it for it in demo_items(200, seed=1) if it.leavable][:n]
    backend = FakeBackend(items, authorized=True, script=script or {})
    await backend.load_dialogs()
    job = FastJob(items=[ItemResult(id=it.id, title=it.title, kind=it.kind) for it in items],
                  delay=2.0, max_auto_wait=60)
    return backend, job, [it.id for it in items]


def statuses(job):
    return [it.status for it in job.items]


class LeaveJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_succeed_sequentially_with_pacing(self):
        backend, job, ids = await make(5)
        await job.run(backend)
        self.assertEqual(job.state, lj.FINISHED)
        self.assertEqual(statuses(job), [lj.LEFT] * 5)
        self.assertEqual(backend.leave_calls, ids)
        self.assertEqual(job.pauses, [2.0] * 4)  # pause between requests, not before the first
        self.assertIsNotNone(job.finished_at)

    async def test_partial_failures_do_not_stop_the_batch(self):
        backend, job, ids = await make(6, script={})
        backend.script = {
            ids[1]: [("fail", "channel_private", False)],
            ids[3]: ["network"],
            ids[4]: ["not_member"],
        }
        await job.run(backend)
        self.assertEqual(statuses(job), [lj.LEFT, lj.FAILED, lj.LEFT, lj.FAILED, lj.NOT_MEMBER, lj.LEFT])
        self.assertFalse(job.items[1].retryable)
        self.assertTrue(job.items[3].retryable)
        self.assertEqual(job.counts(), {lj.LEFT: 3, lj.FAILED: 2, lj.NOT_MEMBER: 1})

    async def test_short_flood_wait_is_waited_out_once_then_retried(self):
        backend, job, ids = await make(3)
        backend.script = {ids[1]: [("flood", 30), "ok"]}
        await job.run(backend)
        self.assertEqual(statuses(job), [lj.LEFT] * 3)
        self.assertIn(31, job.pauses)  # waited the requested time (+1 s margin)
        self.assertEqual(backend.leave_calls, [ids[0], ids[1], ids[1], ids[2]])

    async def test_long_flood_wait_stops_the_batch_without_hammering(self):
        backend, job, ids = await make(5)
        backend.script = {ids[2]: [("flood", 3600)]}
        await job.run(backend)
        self.assertEqual(job.state, lj.STOPPED_RATE_LIMIT)
        self.assertEqual(statuses(job), [lj.LEFT, lj.LEFT] + [lj.RATE_LIMITED] * 3)
        self.assertTrue(all(it.retryable for it in job.items[2:]))
        self.assertEqual(backend.leave_calls, ids[:3])  # nothing sent after the flood wait
        self.assertIsNotNone(job.retry_after)

    async def test_repeated_flood_on_same_item_is_not_retried_again(self):
        backend, job, ids = await make(3)
        backend.script = {ids[0]: [("flood", 5), ("flood", 5)]}
        await job.run(backend)
        self.assertEqual(job.state, lj.STOPPED_RATE_LIMIT)
        self.assertEqual(backend.leave_calls, [ids[0], ids[0]])

    async def test_cancel_stops_before_the_next_item(self):
        backend, job, ids = await make(5)
        original = backend.leave

        async def leave_then_cancel(item_id):
            await original(item_id)
            if item_id == ids[1]:
                job.cancel()

        backend.leave = leave_then_cancel
        await job.run(backend)
        self.assertEqual(job.state, lj.CANCELLED_JOB)
        self.assertEqual(statuses(job), [lj.LEFT, lj.LEFT, lj.CANCELLED, lj.CANCELLED, lj.CANCELLED])
        self.assertEqual(backend.leave_calls, ids[:2])

    async def test_left_items_cannot_be_left_twice(self):
        backend, job, ids = await make(2)
        await job.run(backend)
        self.assertFalse(backend.cached_item(ids[0]).leavable)
        again = FastJob(items=[ItemResult(id=ids[0], title="x", kind="channel")], delay=0, max_auto_wait=60)
        await again.run(backend)
        self.assertEqual(again.items[0].status, lj.FAILED)
        self.assertEqual(backend.leave_calls.count(ids[0]), 2)  # 2nd call rejected inside backend before Telegram

    async def test_unexpected_exception_never_leaves_job_running(self):
        backend, job, ids = await make(3)

        async def boom(item_id):
            raise RuntimeError("bug")

        backend.leave = boom
        with self.assertLogs("tgcleaner.leave", "ERROR"):
            await job.run(backend)
        self.assertEqual(job.state, lj.FINISHED)
        self.assertTrue(all(it.status == lj.FAILED and it.retryable for it in job.items))


if __name__ == "__main__":
    unittest.main()
