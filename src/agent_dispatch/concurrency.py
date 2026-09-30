"""One resizable process limit shared by asyncio callers and background threads."""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from types import TracebackType


class _Waiter:
    """One queued acquirer: a thread (``event``) or a coroutine (``loop`` + ``future``).

    ``granted`` is the source of truth for "a slot is reserved on my behalf",
    set under the limiter lock. The future's state is not: a task can be
    cancelled after its future resolved but before it resumed, and then the
    future says "done" while the coroutine sees CancelledError.
    """

    __slots__ = ("event", "loop", "future", "granted")

    def __init__(
        self,
        *,
        event: threading.Event | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        future: asyncio.Future[None] | None = None,
    ):
        self.event = event
        self.loop = loop
        self.future = future
        self.granted = False


def _resolve(future: asyncio.Future[None]) -> None:
    # Runs on the waiter's own loop. A cancelled future is skipped here; its
    # coroutine sees ``granted`` and passes the reserved slot on.
    if not future.done():
        future.set_result(None)


class DispatchLimiter:
    """A FIFO counting limit whose reservations survive a limit change.

    Async-job worker threads (``with limiter:``) and asyncio callers
    (``await acquire_async()`` ... ``release()``) share one queue and are served
    strictly in arrival order. A freed slot is never put back up for grabs: it
    is handed to the head of the queue by reserving it on the waiter's behalf
    (``_active`` is incremented before the waiter even wakes), so nobody —
    not a thread woken microseconds earlier, not a newcomer on the fast path —
    can overtake. The previous design (``notify_all`` for threads, a 50 ms poll
    for coroutines) let async jobs queued *later* starve a synchronous
    ``dispatch`` indefinitely, because a parked thread always won the race.

    Async waiters park on a future of their own loop and are woken with
    ``call_soon_threadsafe``, so queueing costs no executor thread and no
    polling. A waiter cancelled while queued leaves the queue; one cancelled
    after its grant releases the reserved slot to the next waiter. A waiter
    whose loop is already closed can never resume, so its grant is retracted
    and passed on as well. (A loop that is merely stopped cannot be detected:
    its waiter keeps the slot until the loop runs again.)

    ``resize``: occupied slots are never revoked, growing grants queued
    waiters immediately, shrinking only lowers the ceiling for future grants.
    """

    def __init__(self, limit: int):
        self._lock = threading.Lock()
        self._waiters: deque[_Waiter] = deque()
        self._active = 0
        self.resize(limit)

    def resize(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("Concurrency limit must be positive")
        with self._lock:
            self._limit = limit
            self._grant_waiters()

    def _grant_waiters(self) -> None:
        """Reserve free slots for the head of the queue. Caller holds the lock."""
        while self._waiters and self._active < self._limit:
            waiter = self._waiters.popleft()
            self._active += 1
            waiter.granted = True
            if waiter.event is not None:
                waiter.event.set()
                continue
            try:
                # Scheduling only enqueues a callback, so doing it under our
                # lock cannot re-enter the limiter.
                waiter.loop.call_soon_threadsafe(_resolve, waiter.future)
            except RuntimeError:
                # Loop closed: the coroutine will never run again. Take the
                # reservation back and offer the slot to the next waiter.
                waiter.granted = False
                self._active -= 1

    def _enqueue(self, waiter: _Waiter) -> bool:
        """Take a free slot now (True) or join the queue (False)."""
        with self._lock:
            if not self._waiters and self._active < self._limit:
                self._active += 1
                return True
            self._waiters.append(waiter)
            return False

    def _abandon(self, waiter: _Waiter) -> None:
        """A waiter gave up. Leave the queue, or pass on a slot granted meanwhile."""
        with self._lock:
            if not waiter.granted:
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    pass  # Retracted grant (closed loop): nothing is held.
                return
        # Granted between the wake-up and the cancellation: the reservation is
        # ours, so release it exactly once, which hands it to the next waiter.
        self.release()

    async def acquire_async(self) -> None:
        loop = asyncio.get_running_loop()
        waiter = _Waiter(loop=loop, future=loop.create_future())
        if self._enqueue(waiter):
            return
        try:
            await waiter.future
        except BaseException:
            # CancelledError (task cancelled, wait_for timeout) and also
            # GeneratorExit when a never-resumed coroutine is garbage-collected.
            self._abandon(waiter)
            raise

    def __enter__(self) -> DispatchLimiter:
        waiter = _Waiter(event=threading.Event())
        if self._enqueue(waiter):
            return self
        try:
            waiter.event.wait()
        except BaseException:
            self._abandon(waiter)
            raise
        return self

    def release(self) -> None:
        with self._lock:
            if self._active == 0:
                raise RuntimeError("Concurrency slot released without a reservation")
            self._active -= 1
            self._grant_waiters()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
