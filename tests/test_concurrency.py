"""DispatchLimiter: FIFO hand-off across threads and coroutines, cancellation, resize."""

from __future__ import annotations

import asyncio
import threading

import pytest

from agent_dispatch.concurrency import DispatchLimiter


async def _until(predicate, timeout: float = 2.0) -> None:
    """Poll a condition from the event loop without blocking it."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail("condition not reached")
        await asyncio.sleep(0.005)


def _queued(limiter: DispatchLimiter) -> int:
    return len(limiter._waiters)


class TestFifoHandOff:
    @pytest.mark.asyncio
    async def test_queued_coroutine_beats_a_thread_that_queued_later(self):
        # The starvation bug: release() used notify_all, a parked thread took
        # the slot within microseconds, and the coroutine (polling every 50ms)
        # never saw it free — a sync dispatch starved behind async jobs.
        limiter = DispatchLimiter(1)
        await limiter.acquire_async()
        order: list[str] = []
        coroutine_holds = asyncio.Event()
        coroutine_done = asyncio.Event()

        async def coroutine_waiter():
            await limiter.acquire_async()
            order.append("coroutine")
            coroutine_holds.set()
            await coroutine_done.wait()
            limiter.release()

        def thread_waiter():
            with limiter:
                order.append("thread")

        task = asyncio.create_task(coroutine_waiter())
        await _until(lambda: _queued(limiter) == 1)
        thread = threading.Thread(target=thread_waiter, daemon=True)
        thread.start()
        await _until(lambda: _queued(limiter) == 2)

        limiter.release()
        await asyncio.wait_for(coroutine_holds.wait(), 1)
        # The thread is still parked even though it was woken-capable the
        # whole time: the slot was reserved for the coroutine, not offered up.
        await asyncio.sleep(0.05)
        assert order == ["coroutine"]
        assert limiter._active == 1

        coroutine_done.set()
        await task
        await asyncio.to_thread(thread.join, 2)
        assert order == ["coroutine", "thread"]
        assert limiter._active == 0

    @pytest.mark.asyncio
    async def test_newcomer_cannot_take_a_slot_reserved_for_the_head(self):
        limiter = DispatchLimiter(1)
        await limiter.acquire_async()
        head = asyncio.create_task(limiter.acquire_async())
        await _until(lambda: _queued(limiter) == 1)

        limiter.release()
        # Same tick, before `head` has resumed: a newcomer must queue behind.
        newcomer = asyncio.create_task(limiter.acquire_async())
        await asyncio.wait_for(head, 1)
        await _until(lambda: _queued(limiter) == 1)
        assert not newcomer.done()

        limiter.release()
        await asyncio.wait_for(newcomer, 1)
        limiter.release()
        assert limiter._active == 0

    def test_threads_are_served_in_arrival_order(self):
        limiter = DispatchLimiter(1)
        limiter.__enter__()
        order: list[int] = []

        def worker(i: int):
            with limiter:
                order.append(i)

        threads = []
        for i in range(4):
            t = threading.Thread(target=worker, args=(i,), daemon=True)
            t.start()
            threads.append(t)
            # Wait until this thread is queued before starting the next one,
            # so arrival order is well defined.
            for _ in range(400):
                if _queued(limiter) == i + 1:
                    break
                threading.Event().wait(0.005)
        limiter.release()
        for t in threads:
            t.join(2)
        assert order == [0, 1, 2, 3]
        assert limiter._active == 0


class TestCancellation:
    @pytest.mark.asyncio
    async def test_cancel_while_queued_leaves_the_queue(self):
        limiter = DispatchLimiter(1)
        await limiter.acquire_async()
        task = asyncio.create_task(limiter.acquire_async())
        await _until(lambda: _queued(limiter) == 1)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert _queued(limiter) == 0
        assert limiter._active == 1

        limiter.release()
        assert limiter._active == 0
        await asyncio.wait_for(limiter.acquire_async(), 1)
        limiter.release()

    @pytest.mark.asyncio
    async def test_wait_for_timeout_does_not_leak(self):
        limiter = DispatchLimiter(1)
        await limiter.acquire_async()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(limiter.acquire_async(), 0.02)
        assert _queued(limiter) == 0
        limiter.release()
        assert limiter._active == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("resolved_first", [False, True])
    async def test_cancel_after_grant_passes_the_slot_on(self, resolved_first):
        # resolved_first=False: cancelled before the wake-up callback ran (the
        # future gets cancelled). True: the callback already resolved the
        # future, but the task was cancelled before it resumed — the future
        # says "done" and the coroutine still sees CancelledError. Both hold
        # a reservation that must go to the next waiter, exactly once.
        limiter = DispatchLimiter(1)
        await limiter.acquire_async()
        first = asyncio.create_task(limiter.acquire_async())
        await _until(lambda: _queued(limiter) == 1)
        second = asyncio.create_task(limiter.acquire_async())
        await _until(lambda: _queued(limiter) == 2)
        first_future = limiter._waiters[0].future

        limiter.release()
        assert limiter._active == 1  # reserved for `first` already
        if resolved_first:
            await asyncio.sleep(0)
        # Pin which race window this case exercises.
        assert first_future.done() is resolved_first
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        await asyncio.wait_for(second, 1)
        assert limiter._active == 1
        assert _queued(limiter) == 0
        limiter.release()
        assert limiter._active == 0
        with pytest.raises(RuntimeError):
            limiter.release()

    def test_waiter_on_a_closed_loop_hands_its_grant_on(self):
        limiter = DispatchLimiter(1)
        limiter.__enter__()

        dead_loop = asyncio.new_event_loop()
        dead_task = dead_loop.create_task(limiter.acquire_async())
        dead_loop.run_until_complete(asyncio.sleep(0))
        assert _queued(limiter) == 1
        dead_loop.close()

        got_it = threading.Event()

        def next_in_line():
            with limiter:
                got_it.set()

        thread = threading.Thread(target=next_in_line, daemon=True)
        thread.start()
        for _ in range(400):
            if _queued(limiter) == 2:
                break
            threading.Event().wait(0.005)

        limiter.release()
        assert got_it.wait(1), "grant was lost on the closed loop"
        thread.join(1)
        assert limiter._active == 0
        # The orphaned coroutine is finalized without touching the count.
        dead_task._log_destroy_pending = False
        dead_task.get_coro().close()
        assert limiter._active == 0
        assert _queued(limiter) == 0


class TestResize:
    @pytest.mark.asyncio
    async def test_growing_grants_queued_waiters(self):
        limiter = DispatchLimiter(1)
        await limiter.acquire_async()
        async_waiter = asyncio.create_task(limiter.acquire_async())
        await _until(lambda: _queued(limiter) == 1)
        thread_in = threading.Event()
        thread_out = threading.Event()

        def thread_waiter():
            with limiter:
                thread_in.set()
                thread_out.wait(2)

        thread = threading.Thread(target=thread_waiter, daemon=True)
        thread.start()
        await _until(lambda: _queued(limiter) == 2)

        limiter.resize(3)
        await asyncio.wait_for(async_waiter, 1)
        assert await asyncio.to_thread(thread_in.wait, 1)
        assert limiter._active == 3

        thread_out.set()
        await asyncio.to_thread(thread.join, 2)
        limiter.release()
        limiter.release()
        assert limiter._active == 0

    @pytest.mark.asyncio
    async def test_shrinking_never_revokes_occupied_slots(self):
        limiter = DispatchLimiter(2)
        await limiter.acquire_async()
        await limiter.acquire_async()
        limiter.resize(1)
        assert limiter._active == 2

        waiter = asyncio.create_task(limiter.acquire_async())
        await _until(lambda: _queued(limiter) == 1)
        limiter.release()
        await asyncio.sleep(0.02)
        assert not waiter.done()  # 1 active == new limit 1
        limiter.release()
        await asyncio.wait_for(waiter, 1)
        assert limiter._active == 1
        limiter.release()

    def test_limit_must_be_positive(self):
        with pytest.raises(ValueError):
            DispatchLimiter(0)
        limiter = DispatchLimiter(1)
        with pytest.raises(ValueError):
            limiter.resize(0)
        assert limiter._limit == 1


def test_release_without_reservation_raises():
    limiter = DispatchLimiter(2)
    with pytest.raises(RuntimeError, match="without a reservation"):
        limiter.release()
    with limiter:
        pass
    with pytest.raises(RuntimeError):
        limiter.release()
    assert limiter._active == 0
