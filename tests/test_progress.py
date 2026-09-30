"""Progress must remain bounded and disposable even after a client disconnects."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from agent_dispatch.progress import ProgressBuffer


def test_flood_keeps_recent_messages_and_reports_omissions():
    buffer = ProgressBuffer()
    for i in range(1000):
        buffer.append(f"{i}:" + "x" * 1000)
    messages, dropped = buffer.drain()
    assert len(messages) == 100
    assert dropped == 900
    assert messages[0].startswith("900:")
    assert messages[-1].startswith("999:")
    assert all(len(message) == 300 for message in messages)
    assert buffer.drain() == ([], 0)


def test_close_discards_progress_and_ignores_late_callbacks():
    buffer = ProgressBuffer()
    buffer.append("before disconnect")
    buffer.close()
    for _ in range(1000):
        buffer.append("after disconnect")
    assert buffer.drain() == ([], 0)


def test_concurrent_writers_account_for_every_message():
    buffer = ProgressBuffer()

    def produce(worker):
        for i in range(1000):
            buffer.append(f"{worker}:{i}")

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(produce, range(4)))
    messages, dropped = buffer.drain()
    assert len(messages) == 100
    assert len(set(messages)) == 100
    assert dropped == 3900
