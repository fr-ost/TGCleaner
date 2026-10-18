"""Batch job behaviour: pacing, partial failures, flood waits, cancel. Uses FakeBackend only."""

import asyncio
import unittest

from tgcleaner import jobs
from tgcleaner.fake_backend import FakeBackend, demo_items
from tgcleaner.jobs import BatchJob, ItemResult
from tgcleaner.models import LEAVE


class FastJob(BatchJob):
    """Records requested pauses instead of sleeping (cancel still wins)."""

    @property
    def pauses(self) -> list:
        return self.__dict__.setdefault("_pauses", [])

    async def _pause(self, seconds):
        self.pauses.append(seconds)
        await asyncio.sleep(0)
        return not self._cancel.is_set()


async def make(n=10, script=None):
    items, truth = demo_items(200, seed=1)
    items = [it for it in items if it.leavable][:n]
    backend = FakeBackend(items, truth, authorized=True, script=script or {})
    await backend.load_dialogs()
    job = FastJob(kind=LEAVE, items=[ItemResult(id=it.id, title=it.title, kind=it.kind) for it in items],
                  delay=2.0, max_auto_wait=60)
    return backend, job, [it.id for it in items]


def leave_op(backend, options=None):
    async def op(item_id):
        return await backend.perform(LEAVE, item_id, options or {})
    return op


def statuses(job):
    return [it.status for it in job.items]


class BatchJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_succeed_sequentially_with_pacing(self):
        backend, job, ids = await make(5)
        await job.run(leave_op(backend))
        self.assertEqual(job.state, jobs.FINISHED)
        self.assertEqual(statuses(job), ["left"] * 5)
        self.assertEqual(backend.leave_calls, ids)
        self.assertEqual(job.pauses, [2.0] * 4)  # pause between requests, not before the first
        self.assertIsNotNone(job.finished_at)

    async def test_partial_failures_do_not_stop_the_batch(self):
        backend, job, ids = await make(6)
        backend.script = {
            ids[1]: [("fail", "channel_private", False)],
            ids[3]: ["network"],
            ids[4]: ["not_member"],
        }
        await job.run(leave_op(backend))
        self.assertEqual(statuses(job), ["left", jobs.FAILED, "left", jobs.FAILED, "not_member", "left"])
        self.assertFalse(job.items[1].retryable)
        self.assertTrue(job.items[3].retryable)
        self.assertEqual(job.counts(), {"left": 3, jobs.FAILED: 2, "not_member": 1})

    async def test_short_flood_wait_is_waited_out_once_then_retried(self):
        backend, job, ids = await make(3)
        backend.script = {ids[1]: [("flood", 30), "ok"]}
        await job.run(leave_op(backend))
        self.assertEqual(statuses(job), ["left"] * 3)
        self.assertIn(31, job.pauses)  # waited the requested time (+1 s margin)
        self.assertEqual(backend.leave_calls, [ids[0], ids[1], ids[1], ids[2]])

    async def test_long_flood_wait_stops_the_batch_without_hammering(self):
        backend, job, ids = await make(5)
        backend.script = {ids[2]: [("flood", 3600)]}
        await job.run(leave_op(backend))
        self.assertEqual(job.state, jobs.STOPPED_RATE_LIMIT)
        self.assertEqual(statuses(job), ["left", "left"] + [jobs.RATE_LIMITED] * 3)
        self.assertTrue(all(it.retryable for it in job.items[2:]))
        self.assertEqual(backend.leave_calls, ids[:3])  # nothing sent after the flood wait
        self.assertIsNotNone(job.retry_after)

    async def test_repeated_flood_on_same_item_is_not_retried_again(self):
        backend, job, ids = await make(3)
        backend.script = {ids[0]: [("flood", 5), ("flood", 5)]}
        await job.run(leave_op(backend))
        self.assertEqual(job.state, jobs.STOPPED_RATE_LIMIT)
        self.assertEqual(backend.leave_calls, [ids[0], ids[0]])

    async def test_cancel_stops_before_the_next_item(self):
        backend, job, ids = await make(5)
        op = leave_op(backend)

        async def leave_then_cancel(item_id):
            result = await op(item_id)
            if item_id == ids[1]:
                job.cancel()
            return result

        await job.run(leave_then_cancel)
        self.assertEqual(job.state, jobs.CANCELLED_JOB)
        self.assertEqual(statuses(job), ["left", "left", jobs.CANCELLED, jobs.CANCELLED, jobs.CANCELLED])
        self.assertEqual(backend.leave_calls, ids[:2])

    async def test_done_items_cannot_be_processed_twice(self):
        backend, job, ids = await make(2)
        await job.run(leave_op(backend))
        self.assertFalse(backend.cached_item(ids[0]).leavable)
        again = FastJob(kind=LEAVE, items=[ItemResult(id=ids[0], title="x", kind="channel")], delay=0, max_auto_wait=60)
        await again.run(leave_op(backend))
        self.assertEqual(again.items[0].status, jobs.FAILED)
        self.assertFalse(again.items[0].retryable)

    async def test_scan_job(self):
        backend, _, ids = await make(4)
        job = FastJob(kind="scan", items=[ItemResult(id=i, title="x", kind="channel") for i in ids],
                      delay=1.0, max_auto_wait=60)
        backend.scan_script = {ids[2]: [("flood", 10), "ok"]}
        await job.run(backend.scan)
        self.assertEqual(statuses(job), ["scanned"] * 4)
        self.assertTrue(all(backend.cached_item(i).scanned_at for i in ids))

    async def test_unexpected_exception_never_leaves_job_running(self):
        backend, job, ids = await make(3)

        async def boom(item_id):
            raise RuntimeError("bug")

        with self.assertLogs("tgcleaner.jobs", "ERROR"):
            await job.run(boom)
        self.assertEqual(job.state, jobs.FINISHED)
        self.assertTrue(all(it.status == jobs.FAILED and it.retryable for it in job.items))


if __name__ == "__main__":
    unittest.main()
