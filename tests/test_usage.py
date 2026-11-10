"""Tests for the usage journal (recording, tail reads, rotation, aggregation)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from agent_dispatch import usage


def _write(path: Path, entries: list[dict]) -> None:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


class TestRecord:
    def test_token_fields_roundtrip_in_one_bounded_write(self, monkeypatch):
        writes = []
        real_write = os.write

        def capture(fd, data):
            writes.append(data)
            return real_write(fd, data)

        monkeypatch.setattr(os, "write", capture)
        usage.record(
            "界" * 500, ok=False, caller="界" * 500, outcome="界" * 200,
            error_type="界" * 200,
            tokens={"in": 0, "out": 12, "cr": 999999999999, "cw": 42,
                    "model": "界" * 500, "other": "ignore"},
        )
        (entry,) = usage.load()
        assert {key: entry[key] for key in ("in", "out", "cr", "cw", "model")} == {
            "in": 0, "out": 12, "cr": 999999999999, "cw": 42, "model": "界" * 60,
        }
        assert "other" not in entry
        assert len(writes) == 1
        assert len(writes[0]) <= usage._MAX_LINE_BYTES
        assert len(writes[0]) < 4096

    @pytest.mark.parametrize("value", ["2", "abc", -1, -0.5, None, True, 1e400,
                                      float("nan"), 10**12, 10**400, [], {}])
    def test_invalid_tokens_do_not_lose_the_dispatch_record(self, value):
        usage.record("a", ok=True, tokens={"in": value, "out": value,
                                         "cr": value, "cw": value, "model": value})
        (entry,) = usage.load()
        assert not set(entry) & {"in", "out", "cr", "cw"}
        assert entry.get("model") == (value[:60] if isinstance(value, str) else None)
        assert entry["ok"] is True

    @pytest.mark.parametrize("tokens", [None, [], "junk", 42])
    def test_non_dict_tokens_are_ignored(self, tokens):
        usage.record("a", ok=True, tokens=tokens)
        (entry,) = usage.load()
        assert set(entry) == {"t", "agent", "ok"}

    def test_roundtrip_keeps_only_the_fields_that_were_given(self):
        usage.record("infra", ok=True, cost_usd=0.25, duration_ms=1500, num_turns=3, outcome="done")
        (entry,) = usage.load()
        assert entry["agent"] == "infra"
        assert entry["ok"] is True
        assert entry["cost"] == 0.25
        assert entry["ms"] == 1500
        assert entry["turns"] == 3
        assert entry["outcome"] == "done"
        # Absent fields are omitted rather than written as null.
        assert "err" not in entry and "cached" not in entry and "caller" not in entry

    def test_journal_is_owner_only(self):
        usage.record("infra", ok=True)
        assert usage.journal_path().stat().st_mode & 0o777 == 0o600

    def test_appends_rather_than_truncates(self):
        for i in range(3):
            usage.record(f"a{i}", ok=True)
        assert [e["agent"] for e in usage.load()] == ["a0", "a1", "a2"]

    def test_every_record_fits_one_atomic_append(self):
        # Concurrency rests on each line staying under PIPE_BUF; a long agent
        # name or caller must be capped, never allowed to widen the write.
        usage.record("x" * 500, ok=True, caller="c" * 500, outcome="o" * 200, error_type="e" * 200)
        raw = usage.journal_path().read_bytes()
        assert len(raw) < usage._MAX_LINE_BYTES
        (entry,) = usage.load()
        assert len(entry["caller"]) == usage._CALLER_MAX_CHARS

    def test_an_unwritable_journal_never_raises(self, tmp_path: Path, monkeypatch):
        # A dispatch that already ran and was billed must not fail because a
        # diagnostics file could not be written.
        monkeypatch.setenv("AGENT_DISPATCH_USAGE_LOG", str(tmp_path / "nope" / "x" / "u.jsonl"))
        (tmp_path / "nope").write_text("I am a file, not a directory")
        usage.record("infra", ok=True)  # must not raise
        assert usage.load() == []

    def test_recording_is_skipped_when_a_field_cannot_be_encoded(self):
        class Weird:
            def __float__(self):
                raise ValueError("nope")

        usage.record("infra", ok=True, cost_usd=Weird())  # type: ignore[arg-type]
        assert usage.load() == []


class TestLoad:
    def test_corrupt_and_foreign_lines_are_skipped(self):
        path = usage.journal_path()
        path.write_text(
            '{"t": 1, "agent": "a", "ok": true}\n'
            "not json at all\n"
            "[1,2,3]\n"  # valid JSON, wrong shape
            '{"t": 2, "no_agent": true}\n'
            '{"t": 3, "agent": "b", "ok": false}\n',
            encoding="utf-8",
        )
        assert [e["agent"] for e in usage.load()] == ["a", "b"]

    def test_days_filter_uses_the_record_timestamp(self):
        now = time.time()
        _write(
            usage.journal_path(),
            [
                {"t": int(now - 10 * 86400), "agent": "old", "ok": True},
                {"t": int(now - 3600), "agent": "recent", "ok": True},
            ],
        )
        assert [e["agent"] for e in usage.load(days=7)] == ["recent"]
        assert len(usage.load(days=0)) == 2

    def test_agent_filter(self):
        _write(
            usage.journal_path(),
            [{"t": 1, "agent": "a", "ok": True}, {"t": 2, "agent": "b", "ok": True}],
        )
        assert [e["agent"] for e in usage.load(agent="b")] == ["b"]

    def test_tail_read_drops_the_record_the_seek_landed_inside(self):
        _write(usage.journal_path(), [{"t": i, "agent": f"a{i}", "ok": True} for i in range(50)])
        entries = usage.load(max_bytes=200)
        assert entries  # some tail survived
        assert all("agent" in e for e in entries)
        assert len(entries) < 50  # and it really was a partial read

    def test_missing_journal_is_empty_not_an_error(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("AGENT_DISPATCH_USAGE_LOG", str(tmp_path / "absent.jsonl"))
        assert usage.load() == []


class TestRotation:
    def test_rotates_at_the_cap_and_load_reads_both_generations(self, monkeypatch):
        monkeypatch.setattr(usage, "_MAX_JOURNAL_BYTES", 200)
        for i in range(30):
            usage.record(f"a{i}", ok=True)
        path = usage.journal_path()
        rotated = path.with_name(path.name + ".1")
        assert rotated.exists()
        assert path.stat().st_size < 400  # the live file was reset
        names = [e["agent"] for e in usage.load()]
        # History survives the rotation, and the older generation reads first.
        assert "a29" in names
        assert len(names) > 1


class TestSummarize:
    def test_mixed_token_history_counts_only_measured_live_records(self):
        report = usage.summarize([
            {"agent": "a", "ok": True, "cost": 0.5},  # older CLI
            {"agent": "a", "ok": True, "in": 100, "out": 20, "cr": 600, "cw": 300,
             "model": "main"},
            {"agent": "a", "ok": False, "in": 50, "out": 10, "cr": 300, "cw": 150,
             "model": "main"},
            {"agent": "b", "ok": True, "out": 7, "model": "cheap"},
            {"agent": "a", "ok": True, "cached": True, "in": 999, "cr": 999,
             "model": "ignored"},
        ])
        assert report["total"]["tokens"] == {
            "input": 150, "output": 37, "cache_read": 900, "cache_write": 450, "records": 3,
        }
        assert report["total"]["cache_read_share"] == 0.6
        assert report["total"]["models"] == {"main": 2, "cheap": 1}
        assert report["agents"]["a"]["tokens"]["records"] == 2
        assert report["agents"]["b"]["tokens"]["output"] == 7
        assert "cache_read_share" not in report["agents"]["b"]

    @pytest.mark.parametrize("value", ["abc", None, True, False, -1, -0.5, float("nan"),
                                      float("inf"), 1e308, 10**12, 10**400, [], {}])
    def test_corrupt_tokens_cannot_poison_statistics(self, value):
        report = usage.summarize([
            {"agent": "a", "in": value, "out": value, "cr": value, "cw": value,
             "model": []},
            {"agent": "a", "in": 10, "out": 20, "cr": 60, "cw": 30, "model": "m"},
        ])
        assert report["total"]["tokens"] == {
            "input": 10, "output": 20, "cache_read": 60, "cache_write": 30, "records": 1,
        }
        assert report["total"]["cache_read_share"] == 0.6
        assert report["total"]["models"] == {"m": 1}
        json.dumps(report, allow_nan=False)

    def test_disk_numbers_are_coerced_and_zero_is_a_measurement(self):
        total = usage.summarize([{"agent": "a", "in": "0", "out": "42"}])["total"]
        assert total["tokens"] == {
            "input": 0, "output": 42, "cache_read": 0, "cache_write": 0, "records": 1,
        }
        assert "cache_read_share" not in total

    def test_share_is_rounded_and_invalid_fields_do_not_drop_valid_siblings(self):
        total = usage.summarize([
            {"agent": "a", "in": 1, "cr": 1, "cw": 1, "out": "broken"},
        ])["total"]
        assert total["cache_read_share"] == 0.333
        assert total["tokens"] == {
            "input": 1, "output": 0, "cache_read": 1, "cache_write": 1, "records": 1,
        }

    def test_old_and_model_only_records_do_not_invent_tokens(self):
        old = usage.summarize([{"agent": "a", "ok": True}])["total"]
        assert not set(old) & {"tokens", "cache_read_share", "models"}
        model_only = usage.summarize([{"agent": "a", "model": "m"}])["total"]
        assert model_only["models"] == {"m": 1}
        assert "tokens" not in model_only

    def _sample(self) -> list[dict]:
        return [
            {"t": 1, "agent": "infra", "ok": True, "ms": 1000, "cost": 0.1, "outcome": "done"},
            {"t": 2, "agent": "infra", "ok": True, "ms": 3000, "cost": 0.3, "outcome": "partial"},
            {"t": 3, "agent": "infra", "ok": False, "ms": 300000, "err": "timeout"},
            {"t": 4, "agent": "db", "ok": True, "ms": 500, "cost": 0.05, "outcome": "done"},
            {"t": 5, "agent": "db", "ok": True, "cached": True},
        ]

    def test_totals_and_per_agent_split(self):
        report = usage.summarize(self._sample())
        total = report["total"]
        assert total["dispatches"] == 4  # the cached hit is not a dispatch
        assert total["cached_hits"] == 1
        assert total["ok"] == 3
        assert total["failed"] == 1
        assert total["cost_usd"] == 0.45
        assert total["outcomes"] == {"done": 2, "partial": 1}
        assert total["errors"] == {"timeout": 1}
        assert set(report["agents"]) == {"infra", "db"}
        assert report["agents"]["db"]["cached_hits"] == 1

    def test_agents_are_ordered_by_spend(self):
        assert list(usage.summarize(self._sample())["agents"]) == ["infra", "db"]

    def test_cached_hits_do_not_pollute_durations_or_cost(self):
        report = usage.summarize([{"t": 1, "agent": "a", "ok": True, "cached": True}])
        assert report["total"]["dispatches"] == 0
        assert report["total"]["cost_usd"] == 0.0
        assert "median_ms" not in report["total"]


class TestProfiles:
    def test_tokens_and_models_do_not_expand_discovery_profiles(self):
        for _ in range(3):
            usage.record("a", ok=True, duration_ms=1000, cost_usd=0.2,
                         tokens={"in": 10, "cr": 60, "cw": 30, "out": 20, "model": "m"})
        assert usage.profiles()["a"] == {
            "dispatches": 3, "median_seconds": 1.0, "p90_seconds": 1.0, "median_cost_usd": 0.2,
        }

    def _record_runs(self, agent: str, n: int, ms: int = 60000) -> None:
        for _ in range(n):
            usage.record(agent, ok=True, duration_ms=ms, cost_usd=0.2, outcome="done")

    def test_thin_data_yields_no_profile(self):
        self._record_runs("infra", 2)
        assert usage.agent_profile("infra") is None

    def test_profile_reports_seconds_not_milliseconds(self):
        self._record_runs("infra", 5, ms=90000)
        profile = usage.agent_profile("infra")
        assert profile["dispatches"] == 5
        assert profile["median_seconds"] == 90.0
        assert profile["p90_seconds"] == 90.0
        assert profile["median_cost_usd"] == 0.2
        assert profile["outcomes"] == {"done": 5}

    def test_failures_are_surfaced_in_the_profile(self):
        self._record_runs("infra", 3)
        usage.record("infra", ok=False, error_type="timeout", duration_ms=300000)
        assert usage.agent_profile("infra")["failed"] == 1

    def test_profiles_groups_every_agent_from_one_read(self):
        self._record_runs("infra", 4)
        self._record_runs("db", 4)
        self._record_runs("thin", 1)
        result = usage.profiles()
        assert set(result) == {"infra", "db"}  # "thin" is below min_samples

    def test_profiles_survives_a_corrupt_journal(self):
        usage.journal_path().write_text("garbage\n", encoding="utf-8")
        assert usage.profiles() == {}
        assert usage.agent_profile("infra") is None


class TestSuggestedTimeout:
    def test_pads_p90_and_rounds_up_to_thirty_seconds(self):
        for _ in range(5):
            usage.record("infra", ok=True, duration_ms=100_000)
        # p90 100s -> 150s padded -> already a multiple of 30
        assert usage.suggested_timeout("infra") == 150

    def test_has_a_floor(self):
        for _ in range(5):
            usage.record("fast", ok=True, duration_ms=500)
        assert usage.suggested_timeout("fast") == 30

    def test_none_without_enough_history(self):
        usage.record("infra", ok=True, duration_ms=1000)
        assert usage.suggested_timeout("infra") is None


class TestJournalPath:
    def test_env_override_wins(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("AGENT_DISPATCH_USAGE_LOG", str(tmp_path / "custom.jsonl"))
        assert usage.journal_path() == tmp_path / "custom.jsonl"

    def test_defaults_next_to_the_config(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("AGENT_DISPATCH_USAGE_LOG", raising=False)
        monkeypatch.setenv("AGENT_DISPATCH_CONFIG", str(tmp_path / "cfg" / "agents.yaml"))
        assert usage.journal_path() == tmp_path / "cfg" / "usage.jsonl"


class TestConcurrentWriters:
    def test_two_processes_appending_never_interleave_a_record(self, tmp_path: Path):
        # The whole concurrency design is "one os.write under PIPE_BUF"; this
        # asserts the property that matters — every line stays parseable when
        # two independent processes write at once.
        import subprocess
        import sys

        journal = tmp_path / "shared.jsonl"
        code = (
            "import os,sys;"
            f"os.environ['AGENT_DISPATCH_USAGE_LOG']={str(journal)!r};"
            "from agent_dispatch import usage;"
            "[usage.record(sys.argv[1], ok=True, caller='x'*50) for _ in range(150)]"
        )
        env = {**os.environ, "AGENT_DISPATCH_USAGE_LOG": str(journal)}
        procs = [
            subprocess.Popen([sys.executable, "-c", code, name], env=env) for name in ("p1", "p2")
        ]
        for p in procs:
            assert p.wait(timeout=60) == 0
        lines = journal.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 300
        agents = [json.loads(line)["agent"] for line in lines]  # no torn line
        assert agents.count("p1") == 150
        assert agents.count("p2") == 150


class TestProfileMemo:
    """Profiles are memoized per journal state — discovery runs on the event loop."""

    def _seed(self, n: int = 4, ms: int = 1000) -> None:
        for _ in range(n):
            usage.record("infra", ok=True, duration_ms=ms, cost_usd=0.1, outcome="done")

    def test_repeat_calls_do_not_re_read_the_journal(self, monkeypatch):
        self._seed()
        usage.profiles()  # warm

        calls = []
        real_load = usage.load
        monkeypatch.setattr(usage, "load", lambda **kw: (calls.append(1), real_load(**kw))[1])
        usage.profiles()
        usage.agent_profile("infra")  # same memo, not a second parse
        assert calls == []

    def test_a_new_dispatch_invalidates_it(self):
        self._seed(n=4, ms=1000)
        assert usage.profiles()["infra"]["dispatches"] == 4
        usage.record("infra", ok=True, duration_ms=99_000, cost_usd=0.1, outcome="done")
        profile = usage.profiles()["infra"]
        assert profile["dispatches"] == 5
        assert profile["p90_seconds"] == 99.0  # the new run really is in there

    def test_rotation_invalidates_it(self, monkeypatch):
        self._seed()
        assert usage.profiles()["infra"]["dispatches"] == 4
        monkeypatch.setattr(usage, "_MAX_JOURNAL_BYTES", 1)
        usage.record("infra", ok=True, duration_ms=1000)  # triggers a rotation
        # Still correct after the file was renamed under the memo.
        assert usage.profiles()["infra"]["dispatches"] == 5

    def test_discovery_reads_a_narrower_window_than_the_report(self):
        # The two windows are deliberately different: `stats` wants history,
        # discovery wants to not block the event loop.
        assert usage._PROFILE_TAIL_BYTES < usage._TAIL_BYTES

    def test_a_missing_journal_memoizes_nothing_harmful(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("AGENT_DISPATCH_USAGE_LOG", str(tmp_path / "absent.jsonl"))
        assert usage.profiles() == {}
        usage.record("infra", ok=True, duration_ms=1000, cost_usd=0.1)
        usage.record("infra", ok=True, duration_ms=1000, cost_usd=0.1)
        usage.record("infra", ok=True, duration_ms=1000, cost_usd=0.1)
        assert usage.profiles()["infra"]["dispatches"] == 3


class TestCorruptFieldsNeverRaise:
    def test_nonfinite_cost_is_omitted_without_losing_the_journal_record(self):
        usage.record("infra", ok=True, cost_usd=float("nan"))
        raw = usage.journal_path().read_text(encoding="utf-8")
        entry = json.loads(raw, parse_constant=pytest.fail)
        assert entry["agent"] == "infra"
        assert entry["ok"] is True
        assert "cost" not in entry

    """A file a user can edit must degrade the report, never crash the command.

    `float("abc")` raises ValueError — not JSONDecodeError, not OSError — so it
    walked past the per-line handler and turned `agent-dispatch stats --days 7`
    into a traceback. Same escape route as this codebase's two previous rounds
    of error-handling bugs.
    """

    def test_a_corrupt_timestamp_is_skipped_not_fatal(self):
        usage.journal_path().write_text(
            '{"t": "not-a-number", "agent": "bad", "ok": true}\n'
            '{"t": null, "agent": "nulled", "ok": true}\n'
            f'{{"t": {int(time.time())}, "agent": "good", "ok": true}}\n',
            encoding="utf-8",
        )
        # Unfiltered: every line still parses.
        assert {e["agent"] for e in usage.load()} == {"bad", "nulled", "good"}
        # Windowed: the undatable records fall outside it instead of raising.
        assert [e["agent"] for e in usage.load(days=7)] == ["good"]

    def test_corrupt_numeric_fields_do_not_break_aggregation(self):
        usage.journal_path().write_text(
            '{"t": 1, "agent": "a", "ok": true, "ms": "slow", "cost": "free"}\n'
            '{"t": 2, "agent": "a", "ok": true, "ms": 1000, "cost": 0.5}\n',
            encoding="utf-8",
        )
        report = usage.summarize(usage.load())
        assert report["total"]["dispatches"] == 2
        assert report["total"]["cost_usd"] == 0.5  # the junk value is ignored
        assert report["total"]["median_ms"] == 1000

    def test_as_float_coerces_or_defaults(self):
        assert usage.as_float("3.5") == 3.5
        assert usage.as_float(7) == 7.0
        assert usage.as_float("abc") == 0.0
        assert usage.as_float(None) == 0.0
        assert usage.as_float({"a": 1}, default=-1.0) == -1.0

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), "1e999", 10**400])
    def test_nonfinite_numbers_use_the_default(self, value):
        assert usage.as_float(value, default=-1) == -1

    def test_nonfinite_metrics_do_not_poison_statistics(self):
        entries = [
            {"agent": "a", "ok": True, "ms": float("nan"), "cost": float("inf")},
            {"agent": "a", "ok": True, "ms": 10**400, "cost": float("nan")},
            {"agent": "a", "ok": True, "ms": 1000, "cost": 0.5},
        ]
        report = usage.summarize(entries)
        json.dumps(report, allow_nan=False)
        assert report["total"]["dispatches"] == 3
        assert report["total"]["cost_usd"] == 0.5
        assert report["total"]["median_ms"] == 1000

    def test_huge_finite_values_cannot_overflow_the_report(self):
        # Each 1e308 is finite, so a finiteness filter alone keeps them — and
        # their sum is inf, which `stats --json` printed as `Infinity`.
        usage.journal_path().write_text(
            '{"t": 1, "agent": "a", "ok": true, "cost": 1e308}\n'
            '{"t": 2, "agent": "a", "ok": true, "cost": 1e308}\n'
            '{"t": 3, "agent": "a", "ok": true, "ms": 1e308}\n'
            '{"t": 4, "agent": "a", "ok": true, "ms": -5, "cost": -1}\n'
            '{"t": 5, "agent": "a", "ok": true, "ms": 2000, "cost": 0.25}\n',
            encoding="utf-8",
        )
        report = usage.summarize(usage.load())
        json.dumps(report, allow_nan=False)
        total = report["total"]
        assert total["dispatches"] == 5
        assert total["cost_usd"] == 0.25
        assert total["max_cost_usd"] == 0.25
        assert total["max_ms"] == total["p90_ms"] == total["median_ms"] == 2000
