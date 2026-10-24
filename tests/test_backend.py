"""Unit tests for the pure functions in python/backend.py.

These cover the subtitle parser, the yt-dlp / HandBrake progress parsers and
the output-filename fallback - all pure, all previously unverified.
"""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import backend  # noqa: E402


def _cert_error():
    import ssl
    error = ssl.SSLCertVerificationError(1, 'certificate verify failed')
    error.verify_code = 20
    error.verify_message = 'unable to get local issuer certificate'
    return error


@pytest.mark.parametrize('kind,expected', [
    ('cert', 'tls_cert'), ('wrapped_cert', 'tls_cert'), ('config', 'tls_config'),
    ('dns', 'dns'), ('timeout', 'timeout'), ('refused', 'refused'),
    ('proxy_http', 'proxy'), ('proxy_tunnel', 'proxy'), ('offline', 'offline'),
    ('win10051', 'offline'), ('win10065', 'offline'), ('win10050', 'offline'),
    ('unrelated407', 'other'), ('ssl_protocol', 'tls_protocol'), ('other', 'other'),
    ('ssl_eof', 'other'), ('reset', 'other'), ('ssl_syscall', 'other'),
])
def test_tls_acquisition_classification(kind, expected):
    import errno
    import socket
    import ssl
    import urllib.error
    errors = {
        'cert': _cert_error(), 'wrapped_cert': urllib.error.URLError(_cert_error()),
        'config': backend.SslTrustConfigError('SSL_CERT_FILE: invalid CA'),
        'dns': socket.gaierror(-2, 'test DNS failure'), 'timeout': TimeoutError(),
        'refused': ConnectionRefusedError(),
        'proxy_http': urllib.error.HTTPError('https://invalid', 407, 'Proxy Authentication Required', {}, None),
        'proxy_tunnel': OSError('Tunnel connection failed: 403 Forbidden'),
        'offline': OSError(errno.ENETUNREACH, 'offline'),
        'win10051': OSError(10051, 'offline'), 'win10065': OSError(10065, 'offline'),
        'win10050': OSError(10050, 'offline'),
        'unrelated407': OSError(1407, 'address 10.40.7.1'),
        'ssl_protocol': ssl.SSLError('protocol version'), 'other': ValueError('other'),
        'ssl_eof': ssl.SSLEOFError(), 'reset': ConnectionResetError(),
        'ssl_syscall': ssl.SSLError(ssl.SSL_ERROR_SYSCALL, 'connection reset'),
    }
    assert backend.classify_ytdlp_acquisition_error(errors[kind]) == expected


def test_tls_classification_cycles_causes_context_and_precedence():
    import urllib.error
    cert = _cert_error()
    outer = OSError(10051, 'offline')
    outer.__cause__ = urllib.error.URLError(cert)
    cert.__context__ = outer
    assert backend.classify_ytdlp_acquisition_error(outer) == 'tls_cert'
    cycle = RuntimeError('cycle')
    cycle.__context__ = cycle
    assert backend.classify_ytdlp_acquisition_error(cycle) == 'other'
    context = RuntimeError('wrapper')
    context.__context__ = TimeoutError()
    assert backend.classify_ytdlp_acquisition_error(context) == 'timeout'


@pytest.mark.parametrize('stage', ['metadata', 'download'])
@pytest.mark.parametrize('kind', ['cert', 'config', 'network', 'protocol', 'insecure'])
@pytest.mark.parametrize('existing', [False, True])
def test_tls_updater_exactly_one_attempt_messages(tmp_path, monkeypatch, stage, kind, existing):
    import json
    import urllib.error
    import ssl
    exe = tmp_path / 'yt-dlp.exe'
    if existing:
        exe.write_bytes(b'test-only')
        exe.chmod(0o755)
    monkeypatch.setattr(backend, 'YTDLP_EXE', exe)
    monkeypatch.setattr(backend, 'YTDLP_VER_F', tmp_path / 'version')
    monkeypatch.setattr(backend, '_active_ytdlp_is_external', lambda: False)
    monkeypatch.setattr(backend, '_ytdlp_current_tag', lambda: None)
    monkeypatch.setattr(backend, '_ytdlp_active_version', lambda: None)
    monkeypatch.setattr(backend, 'get_tool', lambda name: name)
    monkeypatch.setattr(backend, 'get_tool_version', lambda *_: '-')
    monkeypatch.setattr(backend, 'emit', lambda *_: None)
    monkeypatch.setattr(backend, 'get_trust_diagnostic', lambda: {'sources': ['windows-system', 'bundle-missing']})
    logs, calls = [], []
    monkeypatch.setattr(backend, 'log', lambda tag, msg, tone='dim': logs.append(msg))
    error = {'cert': urllib.error.URLError(_cert_error()),
             'config': backend.SslTrustConfigError('SSL_CERT_FILE: invalid CA'),
             'protocol': ssl.SSLError('handshake failure'),
             'insecure': backend.InsecureRedirectError('blocked'),
             'network': TimeoutError('test timeout')}[kind]

    def fetch():
        calls.append('metadata')
        if stage == 'metadata':
            raise error
        return 'test-tag', 'https://invalid/test'

    def download(*_):
        calls.append('download')
        raise error

    monkeypatch.setattr(backend, '_ytdlp_fetch_latest', fetch)
    monkeypatch.setattr(backend, '_download_to', download)
    backend._tools_ready.clear()
    try:
        backend._run_ytdlp_check()
        assert backend._tools_ready.is_set()
    finally:
        backend._tools_ready.clear()
    assert calls == (['metadata'] if stage == 'metadata' else ['metadata', 'download'])
    if kind == 'network':
        assert 'Auto-update failed: test timeout' in logs
        assert ('yt-dlp not found - check internet.' in logs) is not existing
    else:
        assert not any(m.startswith('Auto-update failed:') for m in logs)
        assert 'yt-dlp not found - check internet.' not in logs
        if kind == 'cert':
            assert ("Cove couldn't verify the secure connection needed to download yt-dlp. "
                    'Check your system date, certificate trust settings, or network security software. '
                    'Certificate verification was not disabled.') in logs
        elif kind == 'config':
            assert any('configured CA setting' in m and 'SSL_CERT_FILE' in m for m in logs)
        elif kind == 'protocol':
            assert ("Cove couldn't complete the secure (TLS) connection needed to download yt-dlp. "
                    'Check your system date, network security software, or proxy settings. '
                    'Certificate verification was not disabled.') in logs
        else:
            assert ('Cove blocked an insecure (non-HTTPS) redirect while downloading yt-dlp. '
                    'Certificate verification was not disabled.') in logs
        details = [json.loads(m) for m in logs if m.startswith('{')]
        expected = {'cert': 'tls_cert', 'config': 'tls_config', 'protocol': 'tls_protocol', 'insecure': 'insecure_redirect'}
        assert len(details) == 1 and details[0]['category'] == expected[kind]
        assert details[0]['verify_code'] == (20 if kind == 'cert' else None)
        assert details[0]['trust']['sources'] == ['windows-system', 'bundle-missing']


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


# ── active yt-dlp detection (M4) ───────────────────────────────────────────
# All tests point backend.YTDLP_EXE / YTDLP_VER_F at tmp_path so the real
# user-managed tools dir is never touched.
def test_active_ytdlp_is_managed_for_tools_dir_copy(tmp_path, monkeypatch):
    managed = tmp_path / "yt-dlp"
    managed.write_bytes(b"#!/bin/sh\n")
    monkeypatch.setattr(backend, "YTDLP_EXE", managed)
    monkeypatch.setattr(backend, "get_tool", lambda name: str(managed))
    assert backend._active_ytdlp_is_managed() is True


def test_active_ytdlp_not_managed_for_path_binary(tmp_path, monkeypatch):
    managed = tmp_path / "yt-dlp"
    external = tmp_path / "usr" / "bin" / "yt-dlp"
    monkeypatch.setattr(backend, "YTDLP_EXE", managed)
    monkeypatch.setattr(backend, "get_tool", lambda name: str(external))
    assert backend._active_ytdlp_is_managed() is False


def test_active_ytdlp_is_managed_survives_symlink(tmp_path, monkeypatch):
    """A symlinked resolution must not produce a false negative."""
    real = tmp_path / "real" / "yt-dlp"
    real.parent.mkdir()
    real.write_bytes(b"x")
    link = tmp_path / "link" / "yt-dlp"
    link.parent.mkdir()
    link.symlink_to(real)
    monkeypatch.setattr(backend, "YTDLP_EXE", link)
    monkeypatch.setattr(backend, "get_tool", lambda name: str(real))
    assert backend._active_ytdlp_is_managed() is True


def test_active_version_reads_tag_file_for_managed_copy(tmp_path, monkeypatch):
    managed = tmp_path / "yt-dlp"
    ver_f = tmp_path / "yt-dlp.version"
    ver_f.write_text("2026.08.20\n")
    monkeypatch.setattr(backend, "YTDLP_EXE", managed)
    monkeypatch.setattr(backend, "YTDLP_VER_F", ver_f)
    monkeypatch.setattr(backend, "get_tool", lambda name: str(managed))

    def no_probe(cmd):
        raise AssertionError("--version must not be shelled out for managed copy")

    monkeypatch.setattr(backend, "get_tool_version", no_probe)
    assert backend._ytdlp_active_version() == "2026.08.20"


def test_active_version_probes_binary_and_ignores_stale_tag_file(tmp_path, monkeypatch):
    """The actual M4 bug: the version reported described the tag file even
    when a different binary was active. The tag file must NOT be consulted,
    and the active binary must be probed via --version."""
    monkeypatch.setattr(backend, "YTDLP_EXE", tmp_path / "yt-dlp")
    stale = tmp_path / "yt-dlp.version"
    stale.write_text("2020.01.01\n")
    monkeypatch.setattr(backend, "YTDLP_VER_F", stale)
    external = tmp_path / "opt" / "yt-dlp"
    monkeypatch.setattr(backend, "get_tool", lambda name: str(external))
    calls = []

    def fake_probe(cmd):
        calls.append(list(cmd))
        return "2025.12.30"

    monkeypatch.setattr(backend, "get_tool_version", fake_probe)
    assert backend._ytdlp_active_version() == "2025.12.30"
    assert calls, "tag file returned - active binary was never probed"
    assert calls[0] == [str(external), "--version"]


def test_updater_skipped_when_active_ytdlp_is_not_managed(tmp_path, monkeypatch):
    """An external yt-dlp must short-circuit the GitHub update check: zero
    fetches, zero downloads - asserted by call count, not exception-free."""
    external = tmp_path / "opt" / "yt-dlp"
    external.parent.mkdir(parents=True)
    external.write_bytes(b"stub")
    if sys.platform != "win32":
        external.chmod(0o755)
    monkeypatch.setattr(backend, "YTDLP_EXE", tmp_path / "yt-dlp")
    monkeypatch.setattr(backend, "YTDLP_VER_F", tmp_path / "yt-dlp.version")
    monkeypatch.setattr(backend, "get_tool", lambda name: str(external))
    monkeypatch.setattr(backend, "get_tool_version", lambda cmd: "9.9.9")
    events = []
    monkeypatch.setattr(backend, "emit", lambda e: events.append(e))
    fetch_calls = []

    def no_fetch():
        fetch_calls.append(1)
        raise AssertionError("GitHub update check ran for an external yt-dlp")

    monkeypatch.setattr(backend, "_ytdlp_fetch_latest", no_fetch)
    download_calls = []

    def no_download(url, dest):
        download_calls.append((url, dest))
        raise AssertionError("download ran for an external yt-dlp")

    monkeypatch.setattr(backend, "_download_to", no_download)
    backend._tools_ready.clear()
    try:
        backend._run_ytdlp_check()
        released = backend._tools_ready.is_set()
    finally:
        backend._tools_ready.clear()

    assert fetch_calls == [] and download_calls == [], \
        "update pipeline must not run when the active yt-dlp is not managed"
    assert released, "skip path must still set _tools_ready or the UI wedges"
    tags = [e["ytdlp_tag"] for e in events if e.get("type") == "tools_ready"]
    assert tags and all(t == "9.9.9" for t in tags), \
        f"tools_ready must carry the active binary's version, got {tags}"


@pytest.mark.skipif(sys.platform == "win32",
                    reason="Windows has no executable bit to check")
def test_updater_runs_when_external_ytdlp_file_not_executable(tmp_path, monkeypatch):
    """A present but non-executable external file must NOT count as an
    external yt-dlp: a bare isfile check matches it, so the updater would
    skip the managed install yet nothing runnable exists."""
    external = tmp_path / "opt" / "yt-dlp"
    external.parent.mkdir(parents=True)
    external.write_bytes(b"stub")
    if sys.platform != "win32":
        external.chmod(0o644)
    monkeypatch.setattr(backend, "YTDLP_EXE", tmp_path / "yt-dlp")
    monkeypatch.setattr(backend, "YTDLP_VER_F", tmp_path / "yt-dlp.version")
    monkeypatch.setattr(backend, "get_tool", lambda name: str(external))
    monkeypatch.setattr(backend, "get_tool_version", lambda cmd: "-")
    events = []
    monkeypatch.setattr(backend, "emit", lambda e: events.append(e))

    def fake_fetch():
        return ("2026.08.20", "https://example.invalid/yt-dlp")

    monkeypatch.setattr(backend, "_ytdlp_fetch_latest", fake_fetch)
    download_calls = []

    def fake_download(url, dest):
        download_calls.append((url, str(dest)))
        with open(dest, "wb") as f:
            f.write(b"stub")

    monkeypatch.setattr(backend, "_download_to", fake_download)
    backend._tools_ready.clear()
    try:
        backend._run_ytdlp_check()
    finally:
        backend._tools_ready.clear()

    assert len(download_calls) >= 1, \
        ("non-executable external file must trigger the managed "
         f"download, got {download_calls}")


def test_updater_runs_on_fresh_install_with_no_binary_anywhere(tmp_path, monkeypatch):
    """Case 3: no bundled copy, no TOOLS_DIR copy, nothing on PATH -
    get_tool returns its bare last-resort fallback ('yt-dlp', or
    'yt-dlp.exe' on Windows), which is not an actual file. The updater MUST
    still run so the first-time download bootstraps yt-dlp."""
    monkeypatch.setattr(backend, "YTDLP_EXE", tmp_path / "yt-dlp")
    monkeypatch.setattr(backend, "YTDLP_VER_F", tmp_path / "yt-dlp.version")

    def fake_get_tool(name):
        # Mimics get_tool's real shape: managed copy once installed,
        # otherwise the bare name + ext fallback.
        ext = ".exe" if sys.platform == "win32" else ""
        if name == "yt-dlp" and backend.YTDLP_EXE.exists():
            return str(backend.YTDLP_EXE)
        return name + ext

    monkeypatch.setattr(backend, "get_tool", fake_get_tool)
    monkeypatch.setattr(backend, "get_tool_version", lambda cmd: "-")
    events = []
    monkeypatch.setattr(backend, "emit", lambda e: events.append(e))
    fetch_calls = []

    def fake_fetch():
        fetch_calls.append(1)
        return ("2026.08.20", "https://example.invalid/yt-dlp")

    monkeypatch.setattr(backend, "_ytdlp_fetch_latest", fake_fetch)
    download_calls = []

    def fake_download(url, dest):
        download_calls.append((url, str(dest)))
        with open(dest, "wb") as f:
            f.write(b"stub")

    monkeypatch.setattr(backend, "_download_to", fake_download)
    backend._tools_ready.clear()
    try:
        backend._run_ytdlp_check()
        released = backend._tools_ready.is_set()
    finally:
        backend._tools_ready.clear()

    assert fetch_calls, "updater must run when nothing is installed yet"
    assert len(download_calls) == 1, "first-time yt-dlp download must happen"
    assert released, "_run_ytdlp_check must set _tools_ready or the UI wedges"
    ready_tags = [e["ytdlp_tag"] for e in events if e.get("type") == "tools_ready"]
    assert "2026.08.20" in ready_tags, \
        f"post-install tools_ready must carry the new tag, got {ready_tags}"


# ── usable-binary gate (round 2: exists() is too weak) ─────────────────────
# All tests point backend.YTDLP_EXE / YTDLP_VER_F at tmp_path so the real
# user-managed tools dir is never touched.
def _managed_copy(tmp_path, monkeypatch, mode):
    """Install a managed yt-dlp stub in tmp_path with the given shape:
    'dir' makes a directory, an int chmods a regular file."""
    exe = tmp_path / "yt-dlp"
    if mode == "dir":
        exe.mkdir()
    else:
        exe.write_bytes(b"#!/bin/sh\n")
        if sys.platform != "win32":
            exe.chmod(mode)
    monkeypatch.setattr(backend, "YTDLP_EXE", exe)
    monkeypatch.setattr(backend, "YTDLP_VER_F", tmp_path / "yt-dlp.version")
    # Mimics get_tool resolving to the managed copy in TOOLS_DIR.
    monkeypatch.setattr(backend, "get_tool", lambda name: str(exe))
    return exe


def test_ytdlp_available_false_when_managed_path_is_directory(tmp_path, monkeypatch):
    """A directory named yt-dlp satisfies .exists() but cannot be executed;
    it must not count as an available binary."""
    _managed_copy(tmp_path, monkeypatch, "dir")
    assert backend._ytdlp_available() is False


@pytest.mark.skipif(sys.platform == "win32",
                    reason="Windows has no executable bit to check")
def test_ytdlp_available_false_when_managed_file_not_executable(tmp_path, monkeypatch):
    """A present but non-executable file must not count as available."""
    _managed_copy(tmp_path, monkeypatch, 0o644)
    assert backend._ytdlp_available() is False


def test_ytdlp_available_true_for_executable_managed_copy(tmp_path, monkeypatch):
    mode = 0o644 if sys.platform == "win32" else 0o755
    _managed_copy(tmp_path, monkeypatch, mode)
    assert backend._ytdlp_available() is True


def test_updater_redownloads_when_tag_matches_but_managed_path_unusable(tmp_path, monkeypatch):
    """The tag file agreeing with the latest release proves nothing when the
    managed path holds a directory instead of a binary. The updater must
    re-download instead of logging 'already up to date', and the log must say
    Downloading, not Updating - there is no working copy being updated."""
    exe = _managed_copy(tmp_path, monkeypatch, "dir")
    backend.YTDLP_VER_F.write_text("2026.08.20\n")
    assert exe.is_dir()
    monkeypatch.setattr(backend, "get_tool_version", lambda cmd: "-")
    events = []
    monkeypatch.setattr(backend, "emit", lambda e: events.append(e))
    logged = []
    monkeypatch.setattr(backend, "log",
                        lambda tag, msg, tone="dim": logged.append(msg))
    monkeypatch.setattr(
        backend, "_ytdlp_fetch_latest",
        lambda: ("2026.08.20", "https://example.invalid/yt-dlp"))
    download_calls = []

    def fake_download(url, dest):
        download_calls.append((url, str(dest)))
        with open(dest, "wb") as f:
            f.write(b"stub")

    monkeypatch.setattr(backend, "_download_to", fake_download)
    backend._tools_ready.clear()
    try:
        backend._run_ytdlp_check()
    finally:
        backend._tools_ready.clear()

    assert len(download_calls) >= 1, \
        f"unusable managed path must trigger a download, got {download_calls}"
    assert not any("already up to date" in m for m in logged), \
        f"must not report up-to-date over a broken binary, got {logged}"
    assert any(m.startswith("Downloading") for m in logged), \
        f"log must say Downloading for a unusable path, got {logged}"


# ── subtitle selector widening (M9) ────────────────────────────────────────
def test_sub_lang_selector_contains_bare_tag_and_variants():
    assert backend._sub_lang_selector("en") == "en.*,en"


def test_sub_lang_selector_is_derived_from_the_parameter():
    assert backend._sub_lang_selector("de") == "de.*,de"
    assert backend._sub_lang_selector("pt-br") == "pt-br.*,pt-br"


def test_subs_fallback_glob_matches_widened_variant_filenames():
    """The line-832 fallback glob keeps using the bare sub_lang. When the
    widened selector hits an auto-caption track yt-dlp writes e.g.
    Title.en-orig.srt - the glob *.en*.srt must still find that file."""
    import fnmatch
    pattern = "*.en*.srt"
    assert fnmatch.fnmatch("Title.en.srt", pattern)
    assert fnmatch.fnmatch("Title.en-orig.srt", pattern)
    assert fnmatch.fnmatch("Title.en-US.srt", pattern)


# ── site rules: hostname normalization ──────────────────────────────────────
def test_normalize_hostname_download_url_uppercase():
    assert backend._normalize_hostname("https://EXAMPLE.com/x", is_url=True) == "example.com"


def test_normalize_hostname_download_url_trailing_dot():
    assert backend._normalize_hostname("https://example.com./x", is_url=True) == "example.com"


def test_normalize_hostname_download_url_with_port():
    assert backend._normalize_hostname("https://example.com:8443/x", is_url=True) == "example.com"


def test_normalize_hostname_download_url_hostless_is_invalid():
    assert backend._normalize_hostname("not a url", is_url=True) is None
    assert backend._normalize_hostname("", is_url=True) is None
    assert backend._normalize_hostname(None, is_url=True) is None


def test_normalize_hostname_rule_domain_case_and_dot():
    assert backend._normalize_hostname("EXAMPLE.com.", is_url=False) == "example.com"


def test_normalize_hostname_rule_domain_bare():
    assert backend._normalize_hostname("example.com", is_url=False) == "example.com"


def test_normalize_hostname_rule_domain_wildcard_prefix():
    assert backend._normalize_hostname("*.example.com", is_url=False) == "example.com"


def test_normalize_hostname_rule_domain_with_scheme():
    assert backend._normalize_hostname("https://example.com/", is_url=False) == "example.com"


def test_normalize_hostname_rule_domain_with_scheme_port_path():
    assert backend._normalize_hostname(
        "https://example.com:443/foo", is_url=False) == "example.com"


def test_normalize_hostname_rejects_scheme_only_naive_split_bug():
    """Regression guard: a naive split('/')[0] or split(':')[0] normalizer
    would reduce 'https://example.com' to 'https'. Prove that never happens."""
    assert backend._normalize_hostname("https://example.com", is_url=False) != "https"


# ── site rules: matching ─────────────────────────────────────────────────────
def _rule(domain, referer="", cf=None, enabled=True):
    r = {"domain": domain, "referer": referer, "enabled": enabled}
    if cf is not None:
        r["concurrentFragments"] = cf
    return r


def test_resolve_site_rule_exact_host_match():
    rules = [_rule("example.com", referer="https://example.com/")]
    got = backend._resolve_site_rule("https://example.com/v", rules)
    assert got == {"referer": "https://example.com/", "concurrentFragments": None}


def test_resolve_site_rule_subdomain_match():
    rules = [_rule("example.com", referer="https://example.com/")]
    got = backend._resolve_site_rule("https://www.example.com/v", rules)
    assert got is not None and got["referer"] == "https://example.com/"


def test_resolve_site_rule_nested_subdomain_match():
    rules = [_rule("example.com", referer="https://example.com/")]
    got = backend._resolve_site_rule("https://a.b.example.com/v", rules)
    assert got is not None


def test_resolve_site_rule_no_match_similar_prefix():
    rules = [_rule("example.com", referer="https://example.com/")]
    assert backend._resolve_site_rule("https://notexample.com/v", rules) is None


def test_resolve_site_rule_no_match_suffix_domain():
    rules = [_rule("example.com", referer="https://example.com/")]
    assert backend._resolve_site_rule("https://example.com.evil.net/v", rules) is None


def test_resolve_site_rule_no_rules():
    assert backend._resolve_site_rule("https://example.com/v", []) is None
    assert backend._resolve_site_rule("https://example.com/v", None) is None


def test_resolve_site_rule_non_matching_rules_ignored():
    rules = [_rule("other.com", referer="https://other.com/")]
    assert backend._resolve_site_rule("https://example.com/v", rules) is None


def test_resolve_site_rule_longest_domain_wins():
    rules = [
        _rule("example.com", referer="https://example.com/"),
        _rule("video.example.com", referer="https://video.example.com/"),
    ]
    got = backend._resolve_site_rule("https://video.example.com/v", rules)
    assert got["referer"] == "https://video.example.com/"


def test_resolve_site_rule_duplicate_domain_tie_uses_first():
    rules = [
        _rule("example.com", referer="https://first.example.com/"),
        _rule("example.com", referer="https://second.example.com/"),
    ]
    got = backend._resolve_site_rule("https://example.com/v", rules)
    assert got["referer"] == "https://first.example.com/"


def test_resolve_site_rule_disabled_rule_skipped():
    rules = [_rule("example.com", referer="https://example.com/", enabled=False)]
    assert backend._resolve_site_rule("https://example.com/v", rules) is None


# ── site rules: malformed data never crashes resolution ─────────────────────
def test_resolve_site_rule_blank_rule_domain_skipped():
    rules = [_rule("", referer="https://x/")]
    assert backend._resolve_site_rule("https://example.com/v", rules) is None


def test_resolve_site_rule_none_entries_skipped():
    rules = [None, _rule("example.com", referer="https://example.com/")]
    got = backend._resolve_site_rule("https://example.com/v", rules)
    assert got is not None


def test_resolve_site_rule_non_dict_entries_skipped():
    rules = ["example.com", ["example.com"], 42,
             _rule("example.com", referer="https://example.com/")]
    got = backend._resolve_site_rule("https://example.com/v", rules)
    assert got is not None


def test_resolve_site_rule_malformed_url_returns_none():
    rules = [_rule("example.com", referer="https://example.com/")]
    assert backend._resolve_site_rule("not a url", rules) is None
    assert backend._resolve_site_rule("", rules) is None


def test_resolve_site_rule_malformed_rules_container_returns_none():
    assert backend._resolve_site_rule("https://example.com/v", "not a list") is None
    assert backend._resolve_site_rule("https://example.com/v", {"domain": "x"}) is None


# ── site rules: concurrent-fragments validation ─────────────────────────────
def test_valid_concurrent_fragments_accepts_in_range_int():
    assert backend._valid_concurrent_fragments(32) == 32
    assert backend._valid_concurrent_fragments(1) == 1
    assert backend._valid_concurrent_fragments(128) == 128


def test_valid_concurrent_fragments_rejects_out_of_range():
    assert backend._valid_concurrent_fragments(0) is None
    assert backend._valid_concurrent_fragments(129) is None
    assert backend._valid_concurrent_fragments(-1) is None


def test_valid_concurrent_fragments_rejects_non_integer_float():
    assert backend._valid_concurrent_fragments(32.5) is None


def test_valid_concurrent_fragments_accepts_integer_equivalent_float():
    assert backend._valid_concurrent_fragments(32.0) == 32


def test_valid_concurrent_fragments_rejects_bool_and_string():
    assert backend._valid_concurrent_fragments(True) is None
    assert backend._valid_concurrent_fragments("32") is None


def test_resolve_site_rule_invalid_fragment_keeps_valid_referer():
    rules = [_rule("example.com", referer="https://example.com/", cf=9999)]
    got = backend._resolve_site_rule("https://example.com/v", rules)
    assert got["referer"] == "https://example.com/"
    assert got["concurrentFragments"] is None


def test_resolve_site_rule_invalid_referer_scheme_dropped():
    rules = [_rule("example.com", referer="ftp://example.com/")]
    got = backend._resolve_site_rule("https://example.com/v", rules)
    assert got["referer"] == ""


def test_resolve_site_rule_referer_scheme_check_is_case_insensitive():
    """Regression: the renderer accepts an uppercase scheme (its regex is
    case-insensitive) - the backend must not silently drop what the UI just
    accepted as valid."""
    rules = [_rule("example.com", referer="HTTPS://example.com/")]
    got = backend._resolve_site_rule("https://example.com/v", rules)
    assert got["referer"] == "HTTPS://example.com/"


# ── site rules: wired into the yt-dlp argv (download loop) ──────────────────
class _FakeDownloadProc:
    """Minimal Popen surface: no stdout lines, immediate nonzero exit so the
    per-item loop fails fast without touching the filesystem."""
    def __init__(self):
        self.stdout = iter([])
        self.returncode = 1

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


def _run_download_capturing_cmds(monkeypatch, tmp_path, params, cookies_browser=None):
    captured = []

    def fake_popen(cmd, **kwargs):
        captured.append(list(cmd))
        return _FakeDownloadProc()

    monkeypatch.setattr(backend.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(backend, "get_tool", lambda name: name)
    monkeypatch.setattr(backend, "_ytdlp_available", lambda: True)
    monkeypatch.setattr(backend, "detect_browser_cookies", lambda: cookies_browser)
    backend._tools_ready.set()
    params.setdefault("savePath", str(tmp_path))
    params.setdefault("quality", "Best")
    backend._run_download(params)
    return captured


def test_matching_rule_adds_referer_exactly_once(tmp_path, monkeypatch):
    rules = [_rule("example.com", referer="https://example.com/")]
    params = {"urls": [{"url": "https://example.com/v", "id": "1"}],
              "siteRules": rules}
    cmds = _run_download_capturing_cmds(monkeypatch, tmp_path, params)
    assert len(cmds) == 1
    assert cmds[0].count("--referer") == 1
    idx = cmds[0].index("--referer")
    assert cmds[0][idx + 1] == "https://example.com/"


def test_matching_rule_replaces_default_concurrent_fragments(tmp_path, monkeypatch):
    rules = [_rule("example.com", cf=32)]
    params = {"urls": [{"url": "https://example.com/v", "id": "1"}],
              "siteRules": rules}
    cmds = _run_download_capturing_cmds(monkeypatch, tmp_path, params)
    assert cmds[0].count("--concurrent-fragments") == 1
    idx = cmds[0].index("--concurrent-fragments")
    assert cmds[0][idx + 1] == "32"


def test_no_matching_rule_preserves_default_concurrent_fragments_and_no_referer(tmp_path, monkeypatch):
    rules = [_rule("other.com", referer="https://other.com/", cf=64)]
    params = {"urls": [{"url": "https://example.com/v", "id": "1"}],
              "siteRules": rules}
    cmds = _run_download_capturing_cmds(monkeypatch, tmp_path, params)
    idx = cmds[0].index("--concurrent-fragments")
    assert cmds[0][idx + 1] == "4"
    assert "--referer" not in cmds[0]


def test_two_domains_resolve_to_disjoint_site_args(tmp_path, monkeypatch):
    rules = [
        _rule("a.com", referer="https://a.com/", cf=10),
        _rule("b.com", referer="https://b.com/", cf=20),
    ]
    params = {"urls": [{"url": "https://a.com/v", "id": "1"},
                        {"url": "https://b.com/v", "id": "2"}],
              "siteRules": rules}
    cmds = _run_download_capturing_cmds(monkeypatch, tmp_path, params)
    assert len(cmds) == 2
    assert cmds[0][cmds[0].index("--referer") + 1] == "https://a.com/"
    assert cmds[0][cmds[0].index("--concurrent-fragments") + 1] == "10"
    assert cmds[1][cmds[1].index("--referer") + 1] == "https://b.com/"
    assert cmds[1][cmds[1].index("--concurrent-fragments") + 1] == "20"


def test_most_specific_rule_values_used_in_argv(tmp_path, monkeypatch):
    rules = [
        _rule("example.com", referer="https://example.com/", cf=8),
        _rule("video.example.com", referer="https://video.example.com/", cf=16),
    ]
    params = {"urls": [{"url": "https://video.example.com/v", "id": "1"}],
              "siteRules": rules}
    cmds = _run_download_capturing_cmds(monkeypatch, tmp_path, params)
    assert cmds[0][cmds[0].index("--referer") + 1] == "https://video.example.com/"
    assert cmds[0][cmds[0].index("--concurrent-fragments") + 1] == "16"


def test_disabled_rule_applies_nothing(tmp_path, monkeypatch):
    rules = [_rule("example.com", referer="https://example.com/", cf=32, enabled=False)]
    params = {"urls": [{"url": "https://example.com/v", "id": "1"}],
              "siteRules": rules}
    cmds = _run_download_capturing_cmds(monkeypatch, tmp_path, params)
    assert "--referer" not in cmds[0]
    assert cmds[0][cmds[0].index("--concurrent-fragments") + 1] == "4"


def test_invalid_fragment_value_does_not_suppress_valid_referer_in_argv(tmp_path, monkeypatch):
    rules = [_rule("example.com", referer="https://example.com/", cf=999)]
    params = {"urls": [{"url": "https://example.com/v", "id": "1"}],
              "siteRules": rules}
    cmds = _run_download_capturing_cmds(monkeypatch, tmp_path, params)
    assert cmds[0][cmds[0].index("--referer") + 1] == "https://example.com/"
    assert cmds[0][cmds[0].index("--concurrent-fragments") + 1] == "4"


def test_global_proxy_behavior_unchanged_alongside_site_rule(tmp_path, monkeypatch):
    rules = [_rule("example.com", referer="https://example.com/")]
    params = {"urls": [{"url": "https://example.com/v", "id": "1"}],
              "siteRules": rules, "proxy": "socks5://127.0.0.1:9050"}
    cmds = _run_download_capturing_cmds(monkeypatch, tmp_path, params)
    assert cmds[0].count("--proxy") == 1
    assert cmds[0][cmds[0].index("--proxy") + 1] == "socks5://127.0.0.1:9050"


def test_cookieless_retry_receives_same_site_args_as_first_attempt(tmp_path, monkeypatch):
    rules = [_rule("example.com", referer="https://example.com/", cf=32)]
    params = {"urls": [{"url": "https://example.com/v", "id": "1"}],
              "siteRules": rules}
    cmds = _run_download_capturing_cmds(monkeypatch, tmp_path, params,
                                         cookies_browser="firefox")
    assert len(cmds) == 2, "cookie-backed attempt then cookie-less retry expected"
    for cmd in cmds:
        assert cmd[cmd.index("--referer") + 1] == "https://example.com/"
        assert cmd[cmd.index("--concurrent-fragments") + 1] == "32"


def test_subprocess_command_is_argv_list_no_shell():
    """Static guard: the download Popen call must never use shell=True."""
    import inspect
    src = inspect.getsource(backend._run_download)
    assert "shell=True" not in src
    assert "os.system" not in src


_NODE_FAKE_PATHS = set()
_NODE_FAKE_SHEBANG = f"#!{sys.executable}\n"


@pytest.fixture(autouse=True)
def _node_fakes(monkeypatch):
    """Execute only this test's generated shebang fixtures through Python on Windows.

    Install before helpers capture Popen. This remains a real Popen subclass:
    pipes, flags, environment, timeouts and the OS process handle are unchanged.
    Linux continues to execute the shebang directly, without a Popen patch.
    """
    monkeypatch.setattr(sys.modules[__name__], "_NODE_FAKE_PATHS", set())
    children = []
    if sys.platform == "win32":
        real_popen = subprocess.Popen

        class NodeFixturePopen(real_popen):
            def __init__(self, args, *positional, **kwargs):
                adapted = False
                if isinstance(args, (list, tuple)) and args:
                    file = os.fspath(args[0])
                    if file in _NODE_FAKE_PATHS and os.path.isfile(file):
                        with open(file, encoding="utf-8") as source:
                            adapted = source.readline() == _NODE_FAKE_SHEBANG
                    if adapted:
                        args = [sys.executable] + list(args)
                super().__init__(args, *positional, **kwargs)
                if adapted:
                    children.append(self)

        monkeypatch.setattr(subprocess, "Popen", NodeFixturePopen)
    yield children
    # Also reap on an assertion failure, so a failing test cannot leak a sleeper.
    for child in children:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        if child.stdout:
            child.stdout.close()


@pytest.fixture
def node_probe_children(monkeypatch, _node_fakes):
    """Observe real handles without changing how POSIX launches the fixture."""
    children = []
    register = backend._register_child

    def record(proc, kind):
        children.append(proc)
        return register(proc, kind=kind)

    monkeypatch.setattr(backend, "_register_child", record)
    yield children
    for child in children:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def _assert_pid_gone(pid):
    assert pid > 0
    if sys.platform != "win32":
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        return

    # os.kill(pid, 0) is destructive on Windows. Query and wait on a real
    # process handle instead; a terminated process may still have an open handle.
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, pid)
    if not handle:
        assert ctypes.get_last_error() == 87, "only ERROR_INVALID_PARAMETER proves PID is gone"
        return
    try:
        assert kernel32.WaitForSingleObject(handle, 0) == 0  # WAIT_OBJECT_0
        code = wintypes.DWORD()
        assert kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        assert code.value != 259  # STILL_ACTIVE
    finally:
        assert kernel32.CloseHandle(handle)


def _assert_fixture_reaped(pidfile, children):
    assert pidfile.is_file(), "fixture must have started before cleanup can pass"
    pid = int(pidfile.read_text())
    assert len(children) == 1
    proc = children[0]
    assert pid == proc.pid
    # Observe production cleanup before poll() can reap a hidden zombie.
    _assert_pid_gone(pid)
    assert proc.returncode is not None
    assert proc.stdout.closed
    assert proc.poll() is not None


def _node_fake(tmp_path, body, name="embedded node with spaces"):
    path = tmp_path / name
    marker = str(path) + ".executed"
    path.write_text(_NODE_FAKE_SHEBANG + "import os, sys, time\n"
                    "assert os.environ.get('ELECTRON_RUN_AS_NODE') == '1'\n"
                    f"open({marker!r}, 'a').write('executed')\n" + body,
                    encoding="utf-8")
    path.chmod(0o755)
    _NODE_FAKE_PATHS.add(str(path))
    return str(path)


def _probe(path, timeout=1):
    probe = getattr(backend, "_probe_node_runtime", None)
    assert callable(probe), "A bounded embedded Node runtime guard is required"
    return probe(path, timeout=timeout)


@pytest.mark.parametrize("registered", [True, False])
def test_node_fake_popen_scope(tmp_path, _node_fakes, registered):
    path = _node_fake(tmp_path,
        "assert sys.argv[1:] == ['fixture argument']\n"
        "assert os.environ['COVE_TEST_LOCAL'] == 'kept'\nprint('24.1.0')\n")
    if not registered:
        # An identical shebang outside the per-test registry must not be adapted.
        copy = tmp_path / "unregistered script"
        copy.write_bytes(Path(path).read_bytes())
        copy.chmod(0o755)
        path = str(copy)
    kwargs = dict(env=dict(os.environ, ELECTRON_RUN_AS_NODE="1", COVE_TEST_LOCAL="kept"),
                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                  creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    if sys.platform == "win32" and not registered:
        with pytest.raises(OSError):
            subprocess.Popen([path, "fixture argument"], **kwargs)
        assert _node_fakes == []
        assert not (tmp_path / "embedded node with spaces.executed").exists()
        return
    with subprocess.Popen([path, "fixture argument"], **kwargs) as proc:
        stdout, stderr = proc.communicate(timeout=3)
        assert proc.poll() == proc.returncode == 0
        assert stdout.strip() == b"24.1.0" and stderr == b""
        assert _node_fakes == ([proc] if sys.platform == "win32" else [])
    assert proc.stdout.closed
    assert (tmp_path / "embedded node with spaces.executed").read_text() == "executed"
    _assert_pid_gone(proc.pid)


def test_backend_python39_compatibility():
    import ast
    from pathlib import Path

    source = Path(backend.__file__).read_text()
    tree = ast.parse(source, feature_version=(3, 9))
    ast.parse(Path(__file__).read_text(), feature_version=(3, 9))
    # Without postponed annotations, these expressions execute at import.
    # Include class fields, whose annotation expressions run at class creation.
    annotations = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.AnnAssign, ast.arg)):
            annotations.append(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            annotations.append(node.returns)
    unions = [ast.unparse(annotation) for annotation in annotations if annotation
              and any(isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr)
                      for node in ast.walk(annotation))]
    assert not unions, f"Python 3.9 cannot evaluate PEP 604 annotations: {unions}"


@pytest.mark.parametrize("output,supported,version,reason", [
    ("v20.18.3", False, "20.18.3", "Node version too old"),
    ("21.9.0", False, "21.9.0", "Node version too old"),
    ("v22.0.0", True, "22.0.0", "supported"),
    ("22.8.1\n", True, "22.8.1", "supported"),
    ("v24.22.0-beta.1", True, "24.22.0", "supported"),
    ("v24.22.0\n", True, "24.22.0", "supported"),
    ("garbage", False, None, "no parseable Node version"),
    ("", False, None, "no parseable Node version"),
    pytest.param("x" * 100000, False, None, "output too large", id="huge-output"),
])
def test_node_probe_versions(tmp_path, output, supported, version, reason):
    path = _node_fake(tmp_path, f"print({output!r})\n")
    got = _probe(path)
    assert got.supported is supported
    assert got.version == version
    assert got.reason == reason
    assert Path(path + ".executed").read_text() == "executed"


@pytest.mark.parametrize("kind", ["missing", "not-executable", "stderr", "nonzero", "oserror"])
def test_node_probe_rejects_invalid(monkeypatch, tmp_path, kind):
    path = _node_fake(tmp_path, "print('24.1.0')\n")
    if kind == "missing":
        os.unlink(path)
    elif kind == "not-executable":
        os.chmod(path, 0o644)
        if sys.platform == "win32":
            real_access = os.access
            def access(file, mode, *args, **kwargs):
                if os.fspath(file) == path and mode == os.X_OK:
                    return False
                return real_access(file, mode, *args, **kwargs)
            monkeypatch.setattr(os, "access", access)
    elif kind == "stderr":
        path = _node_fake(tmp_path, "print('24.1.0', file=sys.stderr)\n")
    elif kind == "nonzero":
        path = _node_fake(tmp_path, "print('24.1.0'); sys.exit(1)\n")
    else:
        Path(path).write_text("not an executable format")
    got = _probe(path)
    assert not got.supported
    assert got.reason == {
        "missing": "file missing", "not-executable": "file is not executable",
        "stderr": "no parseable Node version", "nonzero": "probe exited 1",
        "oserror": "probe failed (OSError)",
    }[kind]
    marker = Path(path + ".executed")
    if kind in ("stderr", "nonzero"):
        assert marker.read_text() == "executed"
    else:
        assert not marker.exists()


def test_node_probe_timeout_reaps_process(tmp_path, node_probe_children):
    pidfile = tmp_path / "probe.pid"
    path = _node_fake(tmp_path, f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)\n")
    start = time.monotonic()
    result = _probe(path)
    elapsed = time.monotonic() - start
    assert not result.supported and "timed out" in result.reason
    assert result.reason == "probe timed out"
    assert elapsed >= 0.8
    assert elapsed < 3
    _assert_fixture_reaped(pidfile, node_probe_children)


def test_node_probe_windows_flags(monkeypatch, tmp_path):
    import io
    path = _node_fake(tmp_path, "print('24.1.0')\n")
    proc = _FakeDownloadProc()
    proc.returncode, proc.stdout = 0, io.BytesIO(b"24.1.0\n")
    def popen(cmd, **kw):
        assert cmd == [path, "-p", "process.versions.node"]
        assert kw["creationflags"] == 0x08000000
        assert kw["env"]["ELECTRON_RUN_AS_NODE"] == "1"
        assert kw["stdin"] == subprocess.DEVNULL
        assert kw["stdout"] == subprocess.PIPE
        assert kw["stderr"] == subprocess.DEVNULL
        return proc
    real_timer = threading.Timer
    def timer(interval, callback):
        assert 0 < interval <= 10
        return real_timer(interval, callback)
    monkeypatch.setattr(backend.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(threading, "Timer", timer)
    assert _probe(path).supported
    assert proc.stdout.closed


def _runtime_download(monkeypatch, tmp_path, runtime, attempts,
                      inherited_runtime="inherited", probe_children=None, **params):
    events, calls = [], []
    real_popen = subprocess.Popen
    def popen(cmd, **kwargs):
        if cmd[0] == runtime:
            proc = real_popen(cmd, **kwargs)
            if probe_children is not None:
                probe_children.append(proc)
            return proc
        calls.append((cmd, kwargs))
        lines, code = attempts[min(len(calls) - 1, len(attempts) - 1)]
        proc = _FakeDownloadProc()
        proc.stdout, proc.returncode = iter(lines), code
        return proc
    if runtime is None:
        monkeypatch.delenv("COVE_NODE_BIN", raising=False)
    else:
        monkeypatch.setenv("COVE_NODE_BIN", runtime)
    if inherited_runtime is None:
        monkeypatch.delenv("ELECTRON_RUN_AS_NODE", raising=False)
    else:
        monkeypatch.setenv("ELECTRON_RUN_AS_NODE", inherited_runtime)
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(backend, "emit", events.append)
    monkeypatch.setattr(backend, "get_tool", lambda name: name)
    monkeypatch.setattr(backend, "_ytdlp_available", lambda: True)
    backend._tools_ready.set()
    params.setdefault("urls", [{"id": "1", "url": "https://youtube.com/watch?v=test"}])
    params.setdefault("cookies", "none")
    backend._run_download(dict(savePath=str(tmp_path), **params))
    return calls, events


@pytest.mark.parametrize("kind", ["supported", "old", "missing", "hanging", "unset", "empty"])
@pytest.mark.parametrize("inherited", [None, "inherited"])
def test_runtime_queue_guard(monkeypatch, tmp_path, kind, inherited):
    counter = tmp_path / "count"
    body = f"open({str(counter)!r}, 'a').write('x')\n"
    body += "time.sleep(60)\n" if kind == "hanging" else f"print('{ '20.18.3' if kind == 'old' else '24.1.0' }')\n"
    path = _node_fake(tmp_path, body)
    if kind == "missing":
        os.unlink(path)
    runtime = None if kind == "unset" else "" if kind == "empty" else path
    probe = getattr(backend, "_probe_node_runtime", None)
    if probe:
        monkeypatch.setattr(backend, "_probe_node_runtime", lambda p: probe(p, timeout=1))
    calls, events = _runtime_download(monkeypatch, tmp_path, runtime, [([], 1)],
        inherited_runtime=inherited,
        urls=[{"id": str(i), "url": "https://youtube.com/watch?v=test"} for i in range(2)])
    assert len(calls) == 2
    for cmd, kw in calls:
        assert cmd.count("--js-runtimes") == (1 if kind == "supported" else 0)
        assert kw["env"].get("ELECTRON_RUN_AS_NODE") == ("1" if kind == "supported" else inherited)
        if kind == "supported":
            assert cmd[cmd.index("--js-runtimes") + 1] == f"node:{path}"
        assert cmd.count("--remote-components") == cmd.count("ejs:github") == 1
    assert (counter.read_text() if counter.exists() else "") == (
        "x" if kind in ("supported", "old", "hanging") else "")
    warnings = [e for e in events if e.get("tone") == "warn" and "runtime" in e.get("msg", "")]
    assert len(warnings) == (1 if kind in ("old", "missing", "hanging") else 0)


@pytest.mark.parametrize("quality,extra,prefix", [
    ("Audio", {"audioFormat": fmt}, ["-f", "bestaudio/best", "--extract-audio", "--audio-format", fmt, "--audio-quality", "0"])
    for fmt in ("mp3", "opus")
] + [
    ("Best", {}, ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4", "--concurrent-fragments", "4"]),
    ("Subs", {}, ["--skip-download", "--write-subs", "--write-auto-subs", "--sub-langs", "en.*,en", "--convert-subs", "srt"]),
])
def test_runtime_preserves_argv(monkeypatch, tmp_path, quality, extra, prefix):
    calls, _ = _runtime_download(monkeypatch, tmp_path, None, [([], 1)], quality=quality, **extra)
    assert calls[0][0] == ["yt-dlp"] + prefix + ["-o", str(tmp_path / "%(title)s.%(ext)s"),
        "--remote-components", "ejs:github", "https://youtube.com/watch?v=test"]


@pytest.mark.parametrize("warning", [
    "WARNING: [youtube] Signature solving failed",
    "WARNING: [youtube] n challenge solving failed. Ensure you have a supported JavaScript runtime and challenge solver script distribution installed.",
    "WARNING: [youtube] No supported JavaScript runtime could be found. Only deno is enabled by default; to use another runtime add  --js-runtimes RUNTIME[:PATH]  to your command/config. YouTube extraction without a JS runtime has been deprecated, and some formats may be missing. See  https://github.com/yt-dlp/yt-dlp/wiki/EJS  for details on installing one",
    "  WARNING: Challenge solver script unavailable  ",
    "ERROR: JavaScript runtime node-20.18.3 (unsupported)",
])
def test_ejs_diagnostics(monkeypatch, tmp_path, warning):
    _, events = _runtime_download(monkeypatch, tmp_path, None, [([warning], 1)])
    err = next(e["err"] for e in events if "err" in e)
    assert "YouTube challenge solving failed" in err
    assert all(word in err for word in ("yt-dlp", "Cove", "GitHub", "connectivity"))


@pytest.mark.parametrize("extra,expected", [
    ("Could not copy cookie database", "Couldn't read browser cookies - close the browser, or pick Firefox / a cookies.txt file in Destination → Cookies."),
    ("Sign in to confirm you're not a bot", "YouTube blocked this request - update yt-dlp, or sign in via Destination → Cookies if the video is private."),
    ("Only images are available", "Download failed - see the log for details."),
    ("Requested format is not available", "Download failed - see the log for details."),
    ("WARNING: Only images are available for download.", "Download failed - see the log for details."),
    ("ERROR: Requested format is not available.", "Download failed - see the log for details."),
    ("[download] Destination: /tmp/challenge solver script.mp4", "Download failed - see the log for details."),
    ("[youtube] JavaScript runtime unsupported", "Download failed - see the log for details."),
    ("WARNING: No JS challenge solver available", "Download failed - see the log for details."),
])
def test_ejs_precedence_and_generic(monkeypatch, tmp_path, extra, expected):
    lines = ["WARNING: Signature solving failed", extra] if "cookies" in expected or "blocked" in expected else [extra, "ERROR: ffmpeg failed"]
    _, events = _runtime_download(monkeypatch, tmp_path, None, [(lines, 1)])
    assert next(e["err"] for e in events if "err" in e) == expected


@pytest.mark.parametrize("code", [0, 1])
def test_ejs_final_retry_decides(monkeypatch, tmp_path, code):
    output = tmp_path / "result.mp4"
    output.write_bytes(b"video")
    lines = [f"[download] Destination: {output}"] if code == 0 else ["Requested format is not available"]
    calls, events = _runtime_download(monkeypatch, tmp_path, None,
        [(["WARNING: Signature solving failed"], 1), (lines, code)], cookies="firefox")
    assert len(calls) == 2
    assert calls[0][0][-3:-1] == ["--cookies-from-browser", "firefox"]
    for cmd, _ in calls:
        assert "--extractor-args" not in cmd
        assert not any("player_client" in arg for arg in cmd)
    assert "--cookies-from-browser" not in calls[1][0]
    errors = [e["err"] for e in events if "err" in e]
    assert errors == ([] if code == 0 else ["Download failed - see the log for details."])
    assert events[-1]["success"] == (1 if code == 0 else 0)


def _assert_default_clients(cmd):
    assert "--extractor-args" not in cmd
    assert not any("android" in arg or "player_client" in arg for arg in cmd)


@pytest.mark.parametrize("error", [
    "ERROR: unable to download video data: HTTP Error 403",
    "ERROR: Sign in to confirm you're not a bot",
])
def test_default_clients_no_cookie_failure(monkeypatch, tmp_path, error):
    calls, events = _runtime_download(monkeypatch, tmp_path, None, [([error], 1)])
    # Already-green boundaries: one attempt and unchanged error classification.
    assert len(calls) == 1
    assert [e["err"] for e in events if "err" in e] == [
        "YouTube blocked this request - update yt-dlp, "
        "or sign in via Destination \u2192 Cookies if the video is private."]
    _assert_default_clients(calls[0][0])


def test_default_clients_firefox_retry(monkeypatch, tmp_path):
    calls, events = _runtime_download(monkeypatch, tmp_path, None,
        [(["HTTP Error 403"], 1)], cookies="firefox")
    # Already-green boundaries: exactly one credential-free retry.
    assert len(calls) == 2
    first, retry = [cmd for cmd, _ in calls]
    assert first.count("--cookies-from-browser") == first.count("firefox") == 1
    assert first[-3:-1] == ["--cookies-from-browser", "firefox"]
    assert "--cookies" not in first
    assert not any(arg in retry for arg in ("--cookies", "--cookies-from-browser", "firefox"))
    assert events[-1] == {"type": "download_complete", "success": 0, "fail": 1}
    for cmd, _ in calls:
        _assert_default_clients(cmd)


def test_default_clients_cookies_file_retry(monkeypatch, tmp_path):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n")
    calls, _ = _runtime_download(monkeypatch, tmp_path, None,
        [(["HTTP Error 403"], 1)], cookies=str(cookie_file))
    # Already-green boundaries: file source is used only on the first attempt.
    assert len(calls) == 2
    first, retry = [cmd for cmd, _ in calls]
    assert first.count("--cookies") == first.count(str(cookie_file)) == 1
    assert first[-3:-1] == ["--cookies", str(cookie_file)]
    assert "--cookies-from-browser" not in first
    assert not any(arg in retry for arg in ("--cookies", "--cookies-from-browser", str(cookie_file)))
    for cmd, _ in calls:
        _assert_default_clients(cmd)


def test_default_clients_retry_success(monkeypatch, tmp_path):
    output = tmp_path / "result.mp4"
    output.write_bytes(b"video")
    calls, events = _runtime_download(monkeypatch, tmp_path, None,
        [(["HTTP Error 403"], 1), ([f"[download] Destination: {output}"], 0)],
        cookies="firefox")
    # Already-green boundaries: retry success counts and emits no error row.
    assert len(calls) == 2
    assert events[-1] == {"type": "download_complete", "success": 1, "fail": 0}
    assert not any("err" in e for e in events)
    retry = calls[1][0]
    assert not any(arg in retry for arg in ("--cookies", "--cookies-from-browser", "firefox"))
    _assert_default_clients(retry)


@pytest.mark.parametrize("cookies", ["none", "firefox"])
def test_default_clients_other_site(monkeypatch, tmp_path, cookies):
    calls, _ = _runtime_download(monkeypatch, tmp_path, None, [([], 1)],
        cookies=cookies, urls=[{"id": "1", "url": "https://example.com/v"}])
    assert len(calls) == (1 if cookies == "none" else 2)  # Already-green bound.
    for cmd, _ in calls:
        _assert_default_clients(cmd)
        assert not any("youtube" in arg.lower() for arg in cmd)


def test_default_clients_multi_url_bound(monkeypatch, tmp_path):
    urls = [{"id": str(i), "url": f"https://youtube.com/watch?v={i}"} for i in range(3)]
    calls, events = _runtime_download(monkeypatch, tmp_path, None,
        [(["HTTP Error 403"], 1)], urls=urls)
    # Already-green boundaries: one call per URL, no repeated batch.
    assert len(calls) == 3
    assert [cmd[-1] for cmd, _ in calls] == [item["url"] for item in urls]
    assert events[-1] == {"type": "download_complete", "success": 0, "fail": 3}
    for cmd, _ in calls:
        _assert_default_clients(cmd)


@pytest.mark.parametrize("kind", ["supported", "missing", "unsupported"])
def test_default_clients_runtime_retry(monkeypatch, tmp_path, kind):
    runtime = None if kind == "missing" else str(tmp_path / "electron node.exe")
    probes = []
    def probe(path):
        probes.append(path)
        return backend.NodeRuntime(kind == "supported", "24.22.0" if kind == "supported"
                                   else "20.18.3", kind)
    monkeypatch.setattr(backend, "_probe_node_runtime", probe)
    calls, _ = _runtime_download(monkeypatch, tmp_path, runtime,
        [(["HTTP Error 403"], 1)], cookies="firefox")
    # Already-green boundaries: runtime/EJS flags and environment on both attempts.
    assert probes == ([] if runtime is None else [runtime])
    assert len(calls) == 2
    for cmd, kw in calls:
        assert cmd.count("--js-runtimes") == (1 if kind == "supported" else 0)
        if kind == "supported":
            assert cmd.count(f"node:{runtime}") == 1
            assert cmd[cmd.index("--js-runtimes") + 1] == f"node:{runtime}"
        assert kw["env"]["ELECTRON_RUN_AS_NODE"] == ("1" if kind == "supported" else "inherited")
        assert cmd.count("--remote-components") == cmd.count("ejs:github") == 1
        assert cmd[cmd.index("--remote-components") + 1] == "ejs:github"
        _assert_default_clients(cmd)


def test_cookie_download_cancellation_no_retry(monkeypatch, tmp_path):
    # Already-green boundary: cancellation during stdout streaming prevents retry.
    def lines():
        yield "[download] 1.0%"
        backend._cancel.set()
        yield "HTTP Error 403"
    try:
        calls, events = _runtime_download(monkeypatch, tmp_path, None,
            [(lines(), 1)], cookies="firefox")
        assert len(calls) == 1
        assert any(e.get("state") == "cancelled" for e in events)
        assert not any(e.get("state") == "error" or "err" in e for e in events)
        assert events[-1] == {"type": "download_complete", "success": 0, "fail": 0}
    finally:
        backend._cancel.clear()


def test_ejs_rejected_runtime_diagnostic(monkeypatch, tmp_path):
    path = _node_fake(tmp_path, "print('20.18.3')\n")
    _, events = _runtime_download(monkeypatch, tmp_path, path, [(["WARNING: Signature solving failed"], 1)])
    err = next(e["err"] for e in events if "err" in e)
    assert all(word in err for word in ("bundled", "unsupported", "20.18.3", ">= 22", "update Cove"))


def test_ejs_cookie_precedes_bot():
    select = getattr(backend, "_download_error_message", None)
    assert callable(select)
    assert select(True, True, True, None) == (
        "Couldn't read browser cookies - close the browser, "
        "or pick Firefox / a cookies.txt file in Destination → Cookies.")


@pytest.mark.parametrize("shutdown", [False, True])
def test_probe_stop_shutdown_owned_child(monkeypatch, tmp_path, shutdown):
    pidfile = tmp_path / "probe.pid"
    path = _node_fake(tmp_path, f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)\n")
    triggered, children = [], []
    def stop():
        deadline = time.monotonic() + 2
        while (not pidfile.exists() or not pidfile.read_text().strip()) and time.monotonic() < deadline:
            time.sleep(0.01)
        triggered.append(pidfile.exists() and bool(pidfile.read_text().strip()))
        if shutdown:
            backend._shutdown.set()
        else:
            backend._cancel.set()
        backend._kill_children(kind=None if shutdown else "download")
    thread = threading.Thread(target=stop)
    start = time.monotonic()
    thread.start()
    try:
        calls, events = _runtime_download(monkeypatch, tmp_path, path, [([], 1)], probe_children=children)
    finally:
        thread.join(timeout=3)
    assert triggered == [True] and not thread.is_alive()
    assert time.monotonic() - start < 2
    assert calls == []  # No yt-dlp child or --js-runtimes after Stop.
    assert not any(e.get("tone") == "warn" and "runtime" in e.get("msg", "") for e in events)
    assert any(e.get("msg") == "Download cancelled." for e in events)
    assert children and children[0].returncode is not None
    assert children[0].stdout.closed
    _assert_fixture_reaped(pidfile, children)
    assert not backend._children


def test_probe_registration_refusal(monkeypatch, tmp_path):
    pidfile = tmp_path / "refused.pid"
    path = _node_fake(tmp_path, f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)\n")
    register = backend._register_child
    refused, children = [], []
    def refuse(proc, kind):
        # Let the real child reach its body before refusing registration, so
        # both platforms prove cleanup of a running process, not just a spawn.
        deadline = time.monotonic() + 2
        while (not pidfile.exists() or not pidfile.read_text().strip()) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pidfile.is_file()
        backend._cancel.set()
        backend._shutdown.set()
        result = register(proc, kind=kind)
        refused.append(result)
        return result
    monkeypatch.setattr(backend, "_register_child", refuse)
    # Keep RED bounded while the old unregistered probe sleeps.
    probe = backend._probe_node_runtime
    monkeypatch.setattr(backend, "_probe_node_runtime", lambda p: probe(p, timeout=1))
    calls, events = _runtime_download(monkeypatch, tmp_path, path, [([], 1)], probe_children=children)
    assert refused == [False]
    assert calls == []
    assert not any(e.get("tag") == "runtime" for e in events)
    assert children and children[0].returncode is not None
    assert children[0].stdout.closed
    _assert_fixture_reaped(pidfile, children)
    assert not backend._children


@pytest.mark.parametrize("kind", ["sustained", "whitespace"])
def test_probe_bounded_raw_output(tmp_path, kind, node_probe_children):
    pidfile = tmp_path / "output.pid"
    body = f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
    body += ("while True:\n print('x'*1024, flush=True); time.sleep(0.01)\n"
             if kind == "sustained" else "print(' '*1025 + 'v24.22.0')\n")
    path = _node_fake(tmp_path, body)
    start = time.monotonic()
    result = _probe(path)
    assert not result.supported and result.reason == "output too large"
    assert time.monotonic() - start < 0.75
    _assert_fixture_reaped(pidfile, node_probe_children)
    assert not backend._children


def test_probe_rejects_relative_without_execution(monkeypatch, tmp_path):
    marker = tmp_path / "executed"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for directory in (tmp_path, bindir):
        _node_fake(directory,
            f"open({str(marker)!r}, 'a').write('executed')\nprint('24.22.0')\n", name="node")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(bindir))
    result = _probe("node")
    assert not result.supported and result.reason == "path is not absolute"
    assert not marker.exists()
    # Prove these same fixtures are runnable when addressed absolutely.
    for directory in (tmp_path, bindir):
        absolute = _probe(str(directory / "node"))
        assert absolute.supported and absolute.reason == "supported"
    assert marker.read_text() == "executedexecuted"
