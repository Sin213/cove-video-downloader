"""Unit tests for the pure functions in python/backend.py.

These cover the subtitle parser, the yt-dlp / HandBrake progress parsers and
the output-filename fallback - all pure, all previously unverified.
"""
import os
import subprocess
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import backend  # noqa: E402


# ── _parse_srt ─────────────────────────────────────────────────────────────
def test_parse_srt_basic():
    srt = (
        "1\n"
        "00:00:01,000 --> 00:00:02,000\n"
        "hello\n"
        "\n"
        "2\n"
        "00:00:03,000 --> 00:00:04,000\n"
        "world\n"
    )
    cues = backend._parse_srt(srt)
    assert [c["lines"] for c in cues] == [["hello"], ["world"]]
    assert cues[0]["start"] == "00:00:01,000"
    assert cues[1]["end"] == "00:00:04,000"


def test_parse_srt_keeps_digit_only_caption():
    """A caption whose entire text is a number must survive (M10)."""
    srt = (
        "1\n"
        "00:00:01,000 --> 00:00:02,000\n"
        "2024\n"
        "\n"
        "2\n"
        "00:00:03,000 --> 00:00:04,000\n"
        "911\n"
    )
    cues = backend._parse_srt(srt)
    assert [c["lines"] for c in cues] == [["2024"], ["911"]]


def test_parse_srt_multiline_with_digit_line():
    srt = (
        "1\n"
        "00:00:01,000 --> 00:00:02,000\n"
        "call\n"
        "911\n"
        "now\n"
    )
    cues = backend._parse_srt(srt)
    assert cues[0]["lines"] == ["call", "911", "now"]


def test_parse_srt_drops_sequence_numbers():
    """Sequence numbers must still be stripped, not turned into captions."""
    srt = (
        "1\n"
        "00:00:01,000 --> 00:00:02,000\n"
        "alpha\n"
        "\n"
        "2\n"
        "00:00:03,000 --> 00:00:04,000\n"
        "beta\n"
    )
    cues = backend._parse_srt(srt)
    assert all("1" not in c["lines"] and "2" not in c["lines"] for c in cues)


# ── _dedupe_cues ───────────────────────────────────────────────────────────
def test_dedupe_cues_collapses_rolling_captions():
    cues = [
        {"start": "00:00:01,000", "end": "00:00:02,000", "lines": ["a"]},
        {"start": "00:00:02,000", "end": "00:00:03,000", "lines": ["a", "b"]},
        {"start": "00:00:03,000", "end": "00:00:04,000", "lines": ["b"]},
    ]
    out = backend._dedupe_cues(cues)
    assert [c["text"] for c in out] == ["a", "b"]
    assert out[0]["start"] == "00:00:01,000"
    assert out[0]["end"] == "00:00:03,000"


def test_dedupe_cues_keeps_reoccurring_text_separate():
    cues = [
        {"start": "00:00:01,000", "end": "00:00:02,000", "lines": ["chorus"]},
        {"start": "00:01:00,000", "end": "00:01:01,000", "lines": ["verse"]},
        {"start": "00:02:00,000", "end": "00:02:01,000", "lines": ["chorus"]},
    ]
    out = backend._dedupe_cues(cues)
    assert [c["text"] for c in out] == ["chorus", "verse", "chorus"]


# ── yt-dlp progress parsing (M2) ───────────────────────────────────────────
@pytest.mark.parametrize("line,pct,speed", [
    ("[download]  10.0% of 5.00MiB at 2.00MiB/s ETA 00:02", 10.0, "2.00MiB/s"),
    ("[download]  10.0% of 5.00MiB at 2MiB/s ETA 00:02",    10.0, "2MiB/s"),
    ("[download]  10.0% of ~5.00MiB at Unknown B/s ETA Unknown", 10.0, None),
    ("[download] 100% of 5.00MiB in 00:03",                100.0, None),
    ("[download]   0.0% of 5.00MiB at  1.00KiB/s ETA 00:10", 0.0, "1.00KiB/s"),
])
def test_parse_dl_progress(line, pct, speed):
    got = backend._parse_dl_progress(line)
    assert got is not None, f"no match for {line!r}"
    assert got[0] == pct
    if speed is not None:
        assert got[1] == speed


def test_parse_dl_progress_ignores_non_progress():
    assert backend._parse_dl_progress("[info] Downloading 1 format(s): 137+140") is None
    assert backend._parse_dl_progress("") is None


# ── HandBrake progress parsing (M2) ────────────────────────────────────────
@pytest.mark.parametrize("line,pct", [
    ("Encoding: task 1 of 1, 5.00 %", 5.0),
    ("Encoding: task 1 of 1, 5 %", 5.0),
    ("Encoding: task 1 of 1, 99.85 % (34.19 fps, avg 30.00 fps, ETA 00h00m12s)", 99.85),
])
def test_parse_hb_progress(line, pct):
    assert backend._parse_hb_progress(line) == pct


def test_parse_hb_progress_ignores_non_progress():
    assert backend._parse_hb_progress("[15:04:05] hb_init: starting libhb") is None


# ── output-filename fallback (C3) ──────────────────────────────────────────
def test_pick_fallback_file_ignores_preexisting(tmp_path):
    """A file that predates the run must never be selected (C3)."""
    old = tmp_path / "someone-elses-movie.mp4"
    old.write_bytes(b"x")
    old_mtime = time.time() - 3600
    os.utime(old, (old_mtime, old_mtime))

    started = time.time() - 60
    assert backend._pick_fallback_file(str(tmp_path), "*.mp4", started) is None


def test_pick_fallback_file_picks_newest_from_this_run(tmp_path):
    started = time.time()
    first = tmp_path / "a.mp4"
    first.write_bytes(b"x")
    os.utime(first, (started + 1, started + 1))
    second = tmp_path / "b.mp4"
    second.write_bytes(b"x")
    os.utime(second, (started + 2, started + 2))

    got = backend._pick_fallback_file(str(tmp_path), "*.mp4", started)
    assert got == str(second)


def test_pick_fallback_file_no_match(tmp_path):
    assert backend._pick_fallback_file(str(tmp_path), "*.mp4", time.time()) is None


# ── child-process registry (Codex P1: shutdown race) ───────────────────────
class FakeProc:
    """Models subprocess.Popen's surface for the registry: terminate/kill/wait
    all exist, and wait() returns a returncode like the real thing."""
    def __init__(self, hangs=False):
        self.terminated = False
        self.killed = False
        self.waits = 0
        self._hangs = hangs
        self.returncode = None

    def terminate(self):
        self.terminated = True
        if not self._hangs:
            self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        self.waits += 1
        if self._hangs:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
        return self.returncode


@pytest.fixture(autouse=True)
def _clean_registry():
    backend._children.clear()
    backend._cancel.clear()
    backend._shutdown.clear()
    yield
    backend._children.clear()
    backend._cancel.clear()
    backend._shutdown.clear()


def test_kill_children_terminates_registered():
    p = FakeProc()
    assert backend._register_child(p) is True
    backend._kill_children()
    assert p.terminated


def test_kill_children_escalates_to_kill_when_wait_times_out():
    p = FakeProc(hangs=True)
    backend._register_child(p)
    backend._kill_children()
    assert p.terminated and p.killed


def test_register_child_kills_immediately_once_shutdown():
    """A child spawned after process teardown began must be killed, not tracked."""
    backend._shutdown.set()
    p = FakeProc()
    assert backend._register_child(p) is False
    assert p.killed
    assert all(e[0] is not p for e in backend._children)


def test_stop_does_not_block_later_searches():
    """Regression: _cancel stays set until the next start_download, so using it
    as the registration guard killed every search after a single Stop."""
    backend._cancel.set()              # user pressed Stop, never cleared since
    p = FakeProc()
    assert backend._register_child(p, kind="search") is True, \
        "Stop must not prevent a later search from registering"
    assert not p.killed


def test_stop_scoped_to_downloads_leaves_search_alive():
    """cancel_download must not tear down an unrelated in-flight search."""
    dl = FakeProc()
    srch = FakeProc()
    backend._register_child(dl, kind="download")
    backend._register_child(srch, kind="search")

    backend._kill_children(kind="download")

    assert dl.terminated, "download child should be terminated by Stop"
    assert not srch.terminated and not srch.killed, "search child must survive Stop"
    assert any(e[0] is srch for e in backend._children)


def test_stop_between_check_and_spawn_refuses_download_child():
    """The check-then-act window: a worker passes its _cancel check, Stop lands
    and sweeps an empty registry, then the worker spawns. The new child must be
    refused and killed, or a silent yt-dlp blocks the read loop forever."""
    # Worker's pre-spawn check passes.
    assert not backend._cancel.is_set()
    # Stop lands, sweeping an empty set.
    backend._cancel.set()
    backend._kill_children(kind="download")
    # Worker now spawns, having already passed its check.
    late = FakeProc()
    assert backend._register_child(late, kind="download") is False, \
        "download child spawned after Stop must be refused"
    assert late.killed
    assert all(e[0] is not late for e in backend._children)


def test_stop_does_not_refuse_search_child():
    """Same guard must not leak into searches: _cancel stays set after a Stop
    until the next start_download, so searches would be permanently broken."""
    backend._cancel.set()
    p = FakeProc()
    assert backend._register_child(p, kind="search") is True
    assert not p.killed


def test_refused_child_is_reaped_not_just_killed():
    """Killing without waiting leaves a zombie, and on Windows the process
    still holds its output file open - so a caller deleting that file
    immediately hits PermissionError and reports an unexpected error."""
    backend._shutdown.set()
    p = FakeProc()
    assert backend._register_child(p) is False
    assert p.killed, "refused child must be killed"
    assert p.waits >= 1, "refused child must be reaped (wait() never called)"


def test_refused_download_child_is_reaped():
    """Same guarantee on the Stop path, not just the shutdown path."""
    backend._cancel.set()
    p = FakeProc()
    assert backend._register_child(p, kind="download") is False
    assert p.killed and p.waits >= 1


def test_children_lock_is_reentrant():
    """A SIGTERM landing while cancel_download holds the lock re-enters
    _kill_children on the same thread; a plain Lock self-deadlocks."""
    assert isinstance(backend._children_lock, type(threading.RLock()))
    with backend._children_lock:
        # Would hang forever on a non-reentrant Lock.
        backend._kill_children()


def test_kill_children_does_not_drop_child_registered_mid_shutdown():
    """The old unconditional _children.clear() wiped handles that appeared
    between the snapshot and the clear, leaking those processes.

    Interleaving is forced deterministically: terminating the first child
    registers a second one, exactly as a download thread spawning the next
    yt-dlp would."""
    late = FakeProc()
    registered = {}

    class SpawnsOnTerminate(FakeProc):
        def terminate(self):
            super().terminate()
            # Simulates a worker thread registering a new child right now.
            registered["accepted"] = backend._register_child(late)

    first = SpawnsOnTerminate()
    backend._register_child(first)
    backend._kill_children()

    # Either the late child was refused and killed (shutdown already began),
    # or it was accepted and is still tracked so a later sweep can kill it.
    # What must never happen is "accepted but silently dropped".
    if registered["accepted"]:
        assert any(e[0] is late for e in backend._children), \
            "late child accepted but dropped from registry"
    else:
        assert late.killed, "late child refused but never killed"
