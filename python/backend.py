#!/usr/bin/env python3
"""Cove Video Downloader - JSON-lines backend.

Reads commands (one JSON object per line) on stdin, writes events (one JSON
object per line) on stdout. The Electron main process spawns this script
and wires stdout lines to IPC events delivered to the renderer.

Commands (Electron -> Python, one JSON per stdin line):
    {"cmd": "start_download", "params": {...}}
    {"cmd": "cancel_download"}
    {"cmd": "search", "params": {"query": "...", "start": N}}

Events (Python -> Electron, one JSON per stdout line):
    {"type": "ready", "version": "..."}
    {"type": "log", "tag": "...", "tone": "ok|err|warn|dim", "msg": "..."}
    {"type": "item_state", "id": "...", "state": "...", "pct": 0-100, ...}
    {"type": "tools_ready", "ytdlp_tag": "...", "ffmpeg_ver": "...", "hb_ver": "..."}
    {"type": "download_complete", "success": N, "fail": N}
    {"type": "search_results", "query": "...", "start": N, "has_more": bool, "results": [...]}
"""
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import NamedTuple, Optional

# Current yt-dlp/EJS requirement: this is runtime policy, not a prediction.
MIN_NODE_MAJOR = 22
NODE_PROBE_TIMEOUT = 5


class NodeRuntime(NamedTuple):
    supported: bool
    version: Optional[str]
    reason: str


def _probe_node_runtime(path, timeout=NODE_PROBE_TIMEOUT):
    """Probe an owned child with bounded time and raw output; always reap it."""
    proc = timer = None
    timed_out = threading.Event()
    try:
        if not os.path.isabs(path):
            return NodeRuntime(False, None, "path is not absolute")
        if not os.path.isfile(path):
            return NodeRuntime(False, None, "file missing")
        if not os.access(path, os.X_OK):
            return NodeRuntime(False, None, "file is not executable")
        env = os.environ.copy()
        env["ELECTRON_RUN_AS_NODE"] = "1"
        proc = subprocess.Popen(
            [path, "-p", "process.versions.node"], env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if not _register_child(proc, kind="download"):
            return NodeRuntime(False, None, "probe cancelled")
        def expire():
            timed_out.set()
            _kill_and_reap(proc)
        timer = threading.Timer(min(max(timeout, 0.01), 10), expire)
        timer.daemon = True
        timer.start()
        output = proc.stdout.read(1025)
        if len(output) > 1024:
            return NodeRuntime(False, None, "output too large")
        proc.wait()
        if timed_out.is_set():
            return NodeRuntime(False, None, "probe timed out")
        output = output.decode("utf-8").strip()
        match = re.match(r"^v?(\d+)\.\d+\.\d+", output)
        version = match.group(0).removeprefix("v") if match else None
        if proc.returncode:
            return NodeRuntime(False, version, f"probe exited {proc.returncode}")
        if not match:
            return NodeRuntime(False, None, "no parseable Node version")
        if int(match.group(1)) < MIN_NODE_MAJOR:
            return NodeRuntime(False, version, "Node version too old")
        return NodeRuntime(True, version, "supported")
    except Exception as exc:
        return NodeRuntime(False, None, f"probe failed ({type(exc).__name__})")
    finally:
        if timer is not None:
            timer.cancel()
            if timer.ident is not None:
                timer.join()
        if proc is not None:
            _kill_and_reap(proc)
            _unregister_child(proc)
            if proc.stdout:
                try:
                    proc.stdout.close()
                except Exception:
                    pass


def _runtime_update_message(runtime):
    version = f"Node {runtime.version}" if runtime.version else "Node version unknown"
    return (f"Cove's bundled JavaScript runtime is unsupported ({version}; "
            f"{runtime.reason}). YouTube needs Node >= {MIN_NODE_MAJOR}; update Cove.")


def _download_error_message(cookie_error, bot_error, ejs_error, runtime):
    """The final attempt's evidence decides the actionable error."""
    if cookie_error:
        return ("Couldn't read browser cookies - close the browser, "
                "or pick Firefox / a cookies.txt file in Destination → Cookies.")
    if bot_error:
        return ("YouTube blocked this request - update yt-dlp, "
                "or sign in via Destination → Cookies if the video is private.")
    if ejs_error:
        if runtime is not None and not runtime.supported:
            return _runtime_update_message(runtime)
        return ("YouTube challenge solving failed - update yt-dlp and Cove. "
                "Solver scripts are fetched from GitHub; check connectivity.")
    return "Download failed - see the log for details."

# Make `ssl_context.py` importable whether we're running from the checkout
# (python/ dir) or from an installed location (alongside backend.py).
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from ssl_context import get_ssl_context, get_trust_diagnostic, SslTrustConfigError
import ssl
import socket
import errno

def _read_version():
    """Single source of truth: package.json. Falls back only if unreadable
    (e.g. backend.py run standalone from a checkout without the app)."""
    for cand in (
        os.path.join(_HERE, "..", "package.json"),
        os.path.join(_HERE, "..", "..", "package.json"),
        os.path.join(_HERE, "..", "..", "app.asar", "package.json"),
    ):
        try:
            with open(cand, "r", encoding="utf-8") as f:
                v = json.load(f).get("version")
            if v:
                return str(v)
        except Exception:
            continue
    return "0.0.0"

__version__ = _read_version()


# ── stdout/stderr helpers ──────────────────────────────────────────────────
_out_lock = threading.Lock()

def emit(event):
    """Write a single JSON event to stdout (one line). Thread-safe."""
    with _out_lock:
        sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
        sys.stdout.flush()

def log(tag, msg, tone="dim"):
    emit({"type": "log", "tag": tag, "tone": tone, "msg": msg})


# ── tool directory / resolution ────────────────────────────────────────────
def tools_dir():
    portable = os.environ.get("COVE_PORTABLE_DATA")
    if portable:
        base = Path(portable) / "tools"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home())) / "CoveVideoDownloader"
    else:
        base = Path.home() / ".cove-video-downloader"
    base.mkdir(parents=True, exist_ok=True)
    return base

TOOLS_DIR = tools_dir()

def resource_path(relative):
    """Look for bundled resources (ffmpeg, HandBrakeCLI) adjacent to this
    script. In the packaged Electron app backend.py lives at
    resources/app.asar.unpacked/python/, while the runtime ships to
    resources/runtime/ - two levels up, not one."""
    for cand in (
        os.path.join(_HERE, relative),
        os.path.join(_HERE, "..", relative),
        os.path.join(_HERE, "..", "runtime", relative),
        os.path.join(_HERE, "..", "..", "runtime", relative),
    ):
        if os.path.exists(cand):
            return os.path.abspath(cand)
    return relative

def get_tool(name):
    ext = ".exe" if sys.platform == "win32" else ""
    # 1. Bundled next to backend.py / in runtime/
    bundled = resource_path(name + ext)
    if os.path.isfile(bundled):
        return bundled
    # 2. User-managed yt-dlp in tools dir
    managed = TOOLS_DIR / (name + ext)
    if managed.exists():
        return str(managed)
    # 3. Resolve from PATH - returns an absolute path when found, or the
    #    bare name as a last-resort fallback so log messages stay readable.
    found = shutil.which(name + ext)
    if found:
        return found
    return name + ext

def get_tool_version(cmd):
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=5,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        first_line = (result.stdout or result.stderr or "").split("\n")[0]
        m = re.search(r"(\d+\.\d+(?:\.\d+)?)", first_line)
        return m.group(1) if m else "-"
    except Exception:
        return "-"


# ── yt-dlp updater ─────────────────────────────────────────────────────────
YTDLP_API   = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
YTDLP_VER_F = TOOLS_DIR / "yt-dlp.version"
YTDLP_EXE   = TOOLS_DIR / ("yt-dlp.exe" if sys.platform == "win32" else "yt-dlp")

def _ytdlp_current_tag():
    return YTDLP_VER_F.read_text().strip() if YTDLP_VER_F.exists() else ""

def _active_ytdlp_is_managed():
    """True when get_tool("yt-dlp") resolves to the TOOLS_DIR copy this
    updater manages. Compares resolved absolute paths so a symlink or a
    relative path does not produce a false negative."""
    return os.path.realpath(get_tool("yt-dlp")) == os.path.realpath(str(YTDLP_EXE))

def _active_ytdlp_is_external():
    """True when get_tool("yt-dlp") resolved to a real RUNNABLE binary
    outside the managed TOOLS_DIR copy (bundled, or found on PATH):
    a regular file plus the executable bit where the platform has one.
    get_tool's last resort returns the bare name ("yt-dlp", "yt-dlp.exe")
    when nothing is installed anywhere; that string is not an actual file,
    so it must NOT count as external - on a fresh install the updater has
    to run so the first-time download still happens."""
    tool = get_tool("yt-dlp")
    return _is_usable_binary(tool) and not _active_ytdlp_is_managed()

def _ytdlp_active_version():
    """Version of the yt-dlp actually in use. The managed copy keeps using
    the yt-dlp.version tag file - it holds the real release tag like
    2026.08.20, which --version does not print. Anything else is probed."""
    if _active_ytdlp_is_managed():
        return _ytdlp_current_tag()
    return get_tool_version([get_tool("yt-dlp"), "--version"])

class InsecureRedirectError(Exception):
    """Managed acquisition refused a non-HTTPS target; never contains the URL."""


def _require_ytdlp_https(url):
    if urllib.parse.urlsplit(url).scheme.lower() != "https":
        raise InsecureRedirectError("Managed yt-dlp acquisition requires HTTPS")


class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):
        location = headers.get("location") or headers.get("uri")
        if location is not None:
            try:
                _require_ytdlp_https(urllib.parse.urljoin(req.full_url, location))
            except InsecureRedirectError:
                fp.close()
                raise
        return super().http_error_302(req, fp, code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def _open_ytdlp_url(url, timeout):
    _require_ytdlp_https(url)
    req = urllib.request.Request(url, headers={"User-Agent": "CoveVideoDownloader"})
    client = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=get_ssl_context()), _HttpsOnlyRedirect())
    return client.open(req, timeout=timeout)


def _ytdlp_fetch_latest():
    with _open_ytdlp_url(YTDLP_API, timeout=10) as r:
        data = json.loads(r.read())
    tag      = data["tag_name"]
    exe_name = "yt-dlp.exe" if sys.platform == "win32" else "yt-dlp"
    url      = next(a["browser_download_url"] for a in data["assets"] if a["name"] == exe_name)
    _require_ytdlp_https(url)
    return tag, url

def _download_to(url, dest):
    with _open_ytdlp_url(url, timeout=30) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


# ── subtitle post-processing ───────────────────────────────────────────────
# YouTube auto-captions are "rolling": each phrase appears in 2-3 consecutive
# cues as it scrolls on-screen, so a raw SRT has each line repeated 2-3
# times with millisecond gaps. We sliding-window over the parsed cues and
# emit one cue per unique line spanning first-appearance → last-appearance,
# which matches what downsub.com and similar tools produce.
_SRT_TS_RE = re.compile(r"(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})")

def _is_seq_number(lines, i):
    """True if lines[i] is an SRT sequence number rather than caption text.

    A sequence number is a digit-only line whose next non-empty line is a
    timestamp. Checking the lookahead (instead of `s.isdigit()` alone) keeps
    captions that are themselves numbers - "2024", "911" - out of the bin.
    """
    if not lines[i].strip().isdigit():
        return False
    for nxt in lines[i + 1:]:
        s = nxt.strip()
        if not s:
            continue
        return bool(_SRT_TS_RE.match(s))
    return False

def _parse_srt(text):
    cues, cur = [], None
    lines = text.splitlines()
    for i, line in enumerate(lines):
        s = line.strip()
        m = _SRT_TS_RE.match(s)
        if m:
            if cur: cues.append(cur)
            cur = {"start": m.group(1), "end": m.group(2), "lines": []}
        elif cur is not None and s and not _is_seq_number(lines, i):
            cur["lines"].append(s)
    if cur: cues.append(cur)
    return cues

def _dedupe_cues(cues):
    """Sliding-window dedup: a line's span is extended while it keeps
    appearing in consecutive cues; once it drops out, the span closes.
    Different occurrences of the same text (e.g. a chorus, minutes apart)
    correctly produce separate cues."""
    out, active = [], {}
    for cue in cues:
        new_set = set(cue["lines"])
        for t in list(active):
            if t not in new_set:
                del active[t]
        for t in cue["lines"]:
            if t in active:
                out[active[t]]["end"] = cue["end"]
            else:
                out.append({"start": cue["start"], "end": cue["end"], "text": t})
                active[t] = len(out) - 1
    return out

def _render_srt(cues):
    parts = []
    for i, c in enumerate(cues, 1):
        parts.append(str(i))
        parts.append(f"{c['start']} --> {c['end']}")
        parts.append(c["text"])
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"

def _render_vtt(cues):
    parts = ["WEBVTT", ""]
    for c in cues:
        start = c["start"].replace(",", ".")
        end   = c["end"].replace(",", ".")
        parts.append(f"{start} --> {end}")
        parts.append(c["text"])
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"

def _render_txt(cues):
    return "\n".join(c["text"] for c in cues) + "\n"


# ── progress parsing ───────────────────────────────────────────────────────
# Percentage and speed are matched separately: yt-dlp omits the speed on the
# terminal "100% ... in 00:03" line and prints "Unknown B/s" while it is still
# probing, and a single combined pattern silently drops both cases.
_DL_PCT_RE   = re.compile(r"(\d+(?:\.\d+)?)%")
_DL_SPEED_RE = re.compile(r"at\s+(\d+(?:\.\d+)?\s*[KMGT]?i?B/s)")
_HB_PCT_RE   = re.compile(r"(\d+(?:\.\d+)?)\s*%")

def _parse_dl_progress(line):
    """Parse a yt-dlp progress line -> (pct, speed) or None.

    `speed` is "" when yt-dlp did not report one (unknown, or the final line).
    """
    if "%" not in line:
        return None
    m = _DL_PCT_RE.search(line)
    if not m:
        return None
    speed = _DL_SPEED_RE.search(line)
    return float(m.group(1)), (speed.group(1).strip() if speed else "")

def _parse_hb_progress(line):
    """Parse a HandBrakeCLI progress line -> pct or None."""
    if "%" not in line:
        return None
    m = _HB_PCT_RE.search(line)
    return float(m.group(1)) if m else None


def _pick_fallback_file(output_dir, pattern, started_at):
    """Newest file in `output_dir` matching `pattern` that this run created.

    Files modified before `started_at` are ignored: without that bound the
    glob can select a pre-existing user file, report it as the download, and
    (with compression on) overwrite it. Returns None when nothing qualifies.
    """
    candidates = []
    for p in Path(output_dir).glob(pattern):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        # 1s of slack absorbs filesystem mtime granularity.
        if mtime >= started_at - 1:
            candidates.append((mtime, str(p)))
    if not candidates:
        return None
    return max(candidates)[1]

def _postprocess_subs(srt_path, final_fmt):
    """Read yt-dlp's raw SRT, dedupe, write to final format. Returns the
    final file path (renaming/deleting the intermediate SRT as needed)."""
    with open(srt_path, "r", encoding="utf-8") as f:
        raw = f.read()
    cues = _dedupe_cues(_parse_srt(raw))
    if final_fmt == "vtt":
        body, final_path = _render_vtt(cues), re.sub(r"\.srt$", ".vtt", srt_path)
    elif final_fmt == "txt":
        body, final_path = _render_txt(cues), re.sub(r"\.srt$", ".txt", srt_path)
    else:
        body, final_path = _render_srt(cues), srt_path
    with open(final_path, "w", encoding="utf-8") as f:
        f.write(body)
    if final_path != srt_path and os.path.exists(srt_path):
        os.remove(srt_path)
    return final_path


# ── cookie detection ───────────────────────────────────────────────────────
# Detection is filesystem-based: we look for the cookie database yt-dlp would
# read. This avoids a network round-trip per browser (the old `--simulate`
# approach hit YouTube and mis-reported unusable-but-installed browsers), and
# it sidesteps Windows Chrome 127+ app-bound encryption giving fast non-zero
# exits that our phrase matcher couldn't recognize. Firefox is checked first
# because its cookies are plain SQLite - no keyring/DPAPI decryption - so it's
# the most likely to actually work when handed to yt-dlp.
BROWSERS = ["firefox", "chrome", "brave", "chromium", "edge"]

def _browser_cookie_globs(browser):
    home = Path.home()
    if sys.platform == "win32":
        appdata  = Path(os.environ.get("APPDATA",      home / "AppData" / "Roaming"))
        local    = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        return {
            "firefox":  [appdata / "Mozilla/Firefox/Profiles/*/cookies.sqlite"],
            "chrome":   [local   / "Google/Chrome/User Data/Default/Network/Cookies",
                         local   / "Google/Chrome/User Data/Default/Cookies"],
            "brave":    [local   / "BraveSoftware/Brave-Browser/User Data/Default/Network/Cookies",
                         local   / "BraveSoftware/Brave-Browser/User Data/Default/Cookies"],
            "chromium": [local   / "Chromium/User Data/Default/Network/Cookies",
                         local   / "Chromium/User Data/Default/Cookies"],
            "edge":     [local   / "Microsoft/Edge/User Data/Default/Network/Cookies",
                         local   / "Microsoft/Edge/User Data/Default/Cookies"],
        }.get(browser, [])
    if sys.platform == "darwin":
        sup = home / "Library/Application Support"
        return {
            "firefox":  [sup / "Firefox/Profiles/*/cookies.sqlite"],
            "chrome":   [sup / "Google/Chrome/Default/Cookies"],
            "brave":    [sup / "BraveSoftware/Brave-Browser/Default/Cookies"],
            "chromium": [sup / "Chromium/Default/Cookies"],
            "edge":     [sup / "Microsoft Edge/Default/Cookies"],
        }.get(browser, [])
    # Linux
    config = home / ".config"
    return {
        "firefox":  [home / ".mozilla/firefox/*/cookies.sqlite"],
        "chrome":   [config / "google-chrome/Default/Cookies"],
        "brave":    [config / "BraveSoftware/Brave-Browser/Default/Cookies"],
        "chromium": [config / "chromium/Default/Cookies"],
        "edge":     [config / "microsoft-edge/Default/Cookies"],
    }.get(browser, [])

def detect_browser_cookies():
    log("cookies", "Detecting browser cookies...", "dim")
    try:
        for browser in BROWSERS:
            for pattern in _browser_cookie_globs(browser):
                s = str(pattern)
                hits = glob.glob(s) if "*" in s else ([s] if os.path.exists(s) else [])
                if hits:
                    log("cookies", f"Auto-detected: {browser}", "ok")
                    return browser
        log("cookies", "No browser cookies found - proceeding without.", "dim")
        return None
    except Exception as e:
        log("cookies", f"Detection skipped: {e}", "warn")
        return None


# ── download pipeline ──────────────────────────────────────────────────────
_cancel = threading.Event()
_busy   = False
_busy_lock = threading.Lock()

# Live yt-dlp / HandBrakeCLI children, tagged by what spawned them. The
# Electron main process only ever signals *this* script, so without an explicit
# registry a quit leaves the grandchildren running - still writing files and
# burning bandwidth.
#
# Entries are (proc, kind) with kind in {"download", "search"}.
_children = set()
# Reentrant: cancel_download runs on the main thread and holds this lock, and a
# SIGTERM arriving in that window re-enters _kill_children on the *same* thread.
# A plain Lock self-deadlocks there.
_children_lock = threading.RLock()

# Process-teardown signal, deliberately distinct from _cancel. _cancel means
# "the user pressed Stop" and stays set until the next start_download, so using
# it to refuse registration killed every subsequent search.
_shutdown = threading.Event()

def _register_child(proc, kind="download"):
    """Track a child, or kill it outright if it is already obsolete.

    Two independent reasons to refuse, and both are needed:

    * `_shutdown` - the process is tearing down; nothing may start.
    * `_cancel` for a *download* child - the worker checks `_cancel` before
      spawning, so Stop can land in the window between that check and the
      spawn. `_kill_children` would then sweep an empty set and the new child
      would register unopposed. A silent yt-dlp emits no lines, so the read
      loop's per-line `_cancel` check never runs and it blocks indefinitely.
      Searches are deliberately exempt: `_cancel` stays set until the next
      `start_download`, so honouring it there would kill every later search.

    The check and the insert share one lock, so a child spawned just after
    `_kill_children` snapshotted cannot slip through untracked. A refused child
    is killed *and reaped* outside the lock: killing without waiting leaves a
    zombie, and on Windows the process still holds its output file open, so a
    caller deleting that file immediately would hit a PermissionError.
    """
    with _children_lock:
        refuse = _shutdown.is_set() or (kind == "download" and _cancel.is_set())
        if not refuse:
            _children.add((proc, kind))
    if refuse:
        _kill_and_reap(proc)
        return False
    return True

def _kill_and_reap(proc):
    """SIGKILL a process and wait for it, so no zombie is left behind and its
    file handles are released before the caller touches those files."""
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass

def _unregister_child(proc):
    with _children_lock:
        for entry in list(_children):
            if entry[0] is proc:
                _children.discard(entry)

def _kill_children(kind=None):
    """Terminate live children, escalating to SIGKILL after a grace period.

    `kind` limits the sweep - Stop should not tear down an unrelated search.
    """
    with _children_lock:
        entries = [e for e in _children if kind is None or e[1] == kind]
        # Discard only what we snapshotted. Anything registered after this
        # point is handled by the _shutdown guard in _register_child.
        for e in entries:
            _children.discard(e)
    procs = [e[0] for e in entries]
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    for p in procs:
        try:
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

def _is_usable_binary(path):
    """True when path is a runnable program: a regular file, plus the
    executable bit on platforms that have one. .exists() alone also matches
    directories and non-executable files, which are not usable."""
    if not os.path.isfile(path):
        return False
    return sys.platform == "win32" or os.access(path, os.X_OK)


def _ytdlp_available():
    """True when a real yt-dlp binary is resolvable (not the bare-name fallback)."""
    tool = get_tool("yt-dlp")
    return _is_usable_binary(tool) or _is_usable_binary(YTDLP_EXE)

# Set once the startup tool probe/update has settled. Downloads and searches
# started before that would call Popen on the bare name "yt-dlp" and surface a
# raw "[Errno 2] No such file or directory" on a fresh install.
_tools_ready = threading.Event()

def _acquisition_errors(error):
    """Bounded, cycle-safe traversal, including urllib's reason wrappers."""
    pending = [(error, 0)]
    seen = set()
    while pending and len(seen) < 64:
        item, depth = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        yield item
        if depth < 12:
            for name in ("reason", "__cause__", "__context__"):
                child = getattr(item, name, None)
                if child is not None:
                    pending.append((child, depth + 1))


def classify_ytdlp_acquisition_error(error):
    """Pure classification; certificate rejection always outranks network errors."""
    errors = list(_acquisition_errors(error))
    if any(isinstance(e, SslTrustConfigError) for e in errors):
        return "tls_config"
    if any(isinstance(e, ssl.SSLCertVerificationError) for e in errors):
        return "tls_cert"
    if any(isinstance(e, InsecureRedirectError) for e in errors):
        return "insecure_redirect"
    if any((isinstance(e, urllib.error.HTTPError) and e.code == 407)
           or (isinstance(e, OSError) and str(e).startswith("Tunnel connection failed"))
           for e in errors):
        return "proxy"
    if any(isinstance(e, socket.gaierror) for e in errors):
        return "dns"
    if any(isinstance(e, TimeoutError) for e in errors):
        return "timeout"
    if any(isinstance(e, ConnectionRefusedError) for e in errors):
        return "refused"
    if any(getattr(e, "errno", None) in (errno.ENETUNREACH, errno.EHOSTUNREACH,
                                        errno.ENETDOWN, 10051, 10065, 10050)
           or getattr(e, "winerror", None) in (10051, 10065, 10050) for e in errors):
        return "offline"
    # EOF, syscall/reset, clean shutdown and want-read/write retain legacy behavior.
    if any(type(e) is ssl.SSLError and e.errno in (None, ssl.SSL_ERROR_SSL) for e in errors):
        return "tls_protocol"
    return "other"


def _tls_acquisition_detail(error, category):
    cert = next((e for e in _acquisition_errors(error)
                 if isinstance(e, ssl.SSLCertVerificationError)), None)
    # No URLs, environment values, exception reprs, or local filenames.
    return json.dumps({"category": category,
                       "verify_code": getattr(cert, "verify_code", None),
                       "verify_message": getattr(cert, "verify_message", None),
                       "trust": get_trust_diagnostic()}, sort_keys=True)


def _run_ytdlp_check():
    """Probe tools + auto-update yt-dlp.

    Emits tools_ready as soon as we have local versions probed, so the UI
    lights up immediately - even if the GitHub update check is slow or
    fails. Re-emits after a successful yt-dlp update to refresh the version
    string.
    """
    def _snapshot_and_emit():
        ffmpeg_ver = get_tool_version([get_tool("ffmpeg"), "-version"])
        hb_ver     = get_tool_version([get_tool("HandBrakeCLI"), "--version"])
        ytdlp_tag  = _ytdlp_active_version() or "-"
        emit({
            "type": "tools_ready",
            "ytdlp_tag":  ytdlp_tag,
            "ffmpeg_ver": ffmpeg_ver,
            "hb_ver":     hb_ver,
        })

    log("tools", f"yt-dlp    → {get_tool('yt-dlp')}", "dim")
    log("tools", f"ffmpeg    → {get_tool('ffmpeg')}", "dim")
    log("tools", f"HandBrake → {get_tool('HandBrakeCLI')}", "dim")
    _snapshot_and_emit()

    try:
        if _active_ytdlp_is_external():
            log("yt-dlp", f"Using yt-dlp from {get_tool('yt-dlp')} - "
                          "skipping managed update.", "warn")
            log("ready", "Ready. Paste links above to begin.", "ok")
            return
        log("yt-dlp", "Checking for updates...", "dim")
        tag, url = _ytdlp_fetch_latest()
        current  = _ytdlp_current_tag()
        if current == tag and _is_usable_binary(YTDLP_EXE):
            log("yt-dlp", f"{tag} already up to date.", "ok")
        else:
            action = "Updating" if _is_usable_binary(YTDLP_EXE) else "Downloading"
            log("yt-dlp", f"{action} {tag}...", "dim")
            tmp = YTDLP_EXE.with_suffix(".tmp")
            _download_to(url, tmp)
            shutil.move(str(tmp), str(YTDLP_EXE))
            if sys.platform != "win32":
                YTDLP_EXE.chmod(0o755)
            YTDLP_VER_F.write_text(tag)
            log("yt-dlp", f"{tag} installed.", "ok")
            _snapshot_and_emit()
        log("ready", "Ready. Paste links above to begin.", "ok")
    except Exception as e:
        category = classify_ytdlp_acquisition_error(e)
        if category == "tls_cert":
            log("yt-dlp", "Cove couldn't verify the secure connection needed to download yt-dlp. "
                "Check your system date, certificate trust settings, or network security software. "
                "Certificate verification was not disabled.", "err")
        elif category == "insecure_redirect":
            log("yt-dlp", "Cove blocked an insecure (non-HTTPS) redirect while downloading yt-dlp. "
                "Certificate verification was not disabled.", "err")
        elif category == "tls_protocol":
            log("yt-dlp", "Cove couldn't complete the secure (TLS) connection needed to download yt-dlp. "
                "Check your system date, network security software, or proxy settings. "
                "Certificate verification was not disabled.", "err")
        elif category == "tls_config":
            log("yt-dlp", "Cove couldn't load the configured CA setting for the secure connection "
                "needed to download yt-dlp. Check SSL_CERT_FILE or the supplemental certifi bundle. "
                "Certificate verification was not disabled.", "err")
        else:
            log("yt-dlp", f"Auto-update failed: {e}", "err")
        if category in ("tls_cert", "tls_config", "tls_protocol", "insecure_redirect"):
            log("yt-dlp", _tls_acquisition_detail(e, category), "err")
        if _is_usable_binary(YTDLP_EXE):
            log("yt-dlp", "Using existing version.", "warn")
            log("ready", "Ready (update check failed).", "ok")
        elif category not in ("tls_cert", "tls_config", "tls_protocol", "insecure_redirect"):
            log("yt-dlp", "yt-dlp not found - check internet.", "err")
    finally:
        # Set even on failure: _ytdlp_available() is the real gate, and leaving
        # this clear would wedge the UI permanently when GitHub is unreachable
        # but a usable yt-dlp is already on disk.
        _tools_ready.set()

def _sub_lang_selector(lang):
    """Widen a bare language tag so YouTube auto-caption variants match.
    yt-dlp anchors --sub-langs patterns at the start of the tag, so
    en -> en.*,en covers the literal tag en plus regional and auto-caption
    variants such as en-US and en-orig."""
    return f"{lang}.*,{lang}"

# ── site rules (typed per-domain yt-dlp overrides) ──────────────────────────
# A rule is {"domain", "referer", "concurrentFragments", "enabled"}. These are
# pure/defensive: no network, no subprocess, never raise - malformed stored
# data must degrade to "no rule matched", not crash a download.

def _normalize_hostname(host_source, is_url):
    """Extract a lowercase, trailing-dot-stripped hostname from a download
    URL (is_url=True) or a user-entered rule domain (is_url=False, which
    tolerates bare hostnames without a scheme)."""
    if not isinstance(host_source, str):
        return None
    s = host_source.strip()
    if not s:
        return None
    if not is_url:
        if s.startswith("*."):
            s = s[2:]
        if not s:
            return None
        if "://" not in s:
            s = "//" + s
    try:
        host = urllib.parse.urlsplit(s).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower().rstrip(".")
    return host or None

def _valid_concurrent_fragments(value):
    """Accept only an integer-equivalent 1..128; reject bool/float-fraction/str."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        n = value
    elif isinstance(value, float) and value.is_integer():
        n = int(value)
    else:
        return None
    return n if 1 <= n <= 128 else None

def _resolve_site_rule(url, rules):
    """Pick the best-matching enabled rule for `url` out of `rules`, or None.
    Longest normalized domain wins; ties keep the first list entry. Never
    raises - any malformed rule or URL is treated as non-matching."""
    try:
        host = _normalize_hostname(url, is_url=True)
        if not host or not isinstance(rules, list):
            return None
        best = None
        best_len = -1
        for rule in rules:
            if not isinstance(rule, dict) or not rule.get("enabled"):
                continue
            domain = _normalize_hostname(rule.get("domain"), is_url=False)
            if not domain:
                continue
            if host != domain and not host.endswith("." + domain):
                continue
            if len(domain) > best_len:
                best = rule
                best_len = len(domain)
        if best is None:
            return None
        referer = best.get("referer")
        referer = referer.strip() if isinstance(referer, str) else ""
        if referer and not referer.lower().startswith(("http://", "https://")):
            referer = ""
        return {
            "referer": referer,
            "concurrentFragments": _valid_concurrent_fragments(best.get("concurrentFragments")),
        }
    except Exception:
        return None

def _run_download(params):
    global _busy
    try:
        if not _tools_ready.is_set() or not _ytdlp_available():
            msg = ("yt-dlp is still being installed - wait for "
                   "\"Ready\" in the log, then try again.")
            log("dl", msg, "warn")
            for item in (params.get("urls") or []):
                item_id = item.get("id") if isinstance(item, dict) else None
                if item_id:
                    emit({"type": "item_state", "id": item_id,
                          "state": "error", "err": msg})
            emit({"type": "download_complete", "success": 0,
                  "fail": len(params.get("urls") or [])})
            return
        urls       = params.get("urls") or []
        quality    = params.get("quality", "Best")
        compress   = bool(params.get("compress", False))
        save_to    = params.get("savePath") or str(Path.home() / "Downloads")
        video_fmt    = (params.get("videoFormat") or "mp4").lower()
        # Codec preference: yt-dlp's `-S vcodec:<codec>` sorts the chosen
        # codec first but falls back to the next-best if it's unavailable
        # at the requested resolution - this keeps Auto behavior intact and
        # gives users a soft preference rather than a hard filter.
        video_codec  = (params.get("videoCodec") or "auto").lower()
        audio_req    = (params.get("audioFormat") or "mp3").lower()
        sub_fmt_req  = (params.get("subFormat")   or "srt").lower()
        sub_lang     = (params.get("subLang")     or "en").lower()
        if sub_fmt_req not in ("srt", "vtt", "txt"):
            sub_fmt_req = "srt"
        # We always ask yt-dlp for SRT so we can dedupe YouTube's rolling
        # auto-caption cues uniformly, then render out to the user's chosen
        # format in Python.
        sub_fmt_ytdlp = "srt"

        proxy_url = (params.get("proxy") or "").strip()

        # Cookies source: "auto" (detect), browser name ("firefox"/"chrome"/...),
        # "none" (skip), or an absolute path to a Netscape cookies.txt file.
        cookies_mode = (params.get("cookies") or "auto").strip()

        is_audio = (quality == "Audio")
        is_subs  = (quality == "Subs")
        # (ytdlp --audio-format token, output file extension)
        _amap = {
            "mp3":  ("mp3",    "mp3"),
            "opus": ("opus",   "opus"),
            "aac":  ("aac",    "m4a"),
            "ogg":  ("vorbis", "ogg"),
            "flac": ("flac",   "flac"),
            "wav":  ("wav",    "wav"),
        }
        ytdlp_afmt, audio_fmt = _amap.get(audio_req, ("mp3", "mp3"))
        # avi/mov/etc. supported by --merge-output-format; default to mp4 if unknown
        if video_fmt not in ("mp4", "webm", "mkv", "mov", "avi"):
            video_fmt = "mp4"
        do_compress = compress and not is_audio and not is_subs

        output_dir    = save_to
        ytdlp_bin     = get_tool("yt-dlp")
        ffmpeg_bin    = get_tool("ffmpeg")
        hbcli_bin     = get_tool("HandBrakeCLI")
        ffmpeg_loc    = os.path.dirname(os.path.abspath(ffmpeg_bin)) if os.path.isfile(ffmpeg_bin) else None
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

        # Electron binary doubling as Node for yt-dlp's YouTube EJS solver.
        # main.js injects its own process.execPath as COVE_NODE_BIN; with
        # ELECTRON_RUN_AS_NODE=1 Electron behaves as plain Node, so yt-dlp
        # treats it as a regular `node` runtime.
        electron_node = os.environ.get("COVE_NODE_BIN")
        node_runtime = _probe_node_runtime(electron_node) if electron_node else None
        if _shutdown.is_set():
            _cancel.set()
        if _cancel.is_set():
            node_runtime = electron_node = None
        elif node_runtime is not None and not node_runtime.supported:
            log("runtime", _runtime_update_message(node_runtime), "warn")
            electron_node = None
        child_env = os.environ.copy()
        if electron_node:
            child_env["ELECTRON_RUN_AS_NODE"] = "1"
        if ffmpeg_loc:
            child_env["PATH"] = ffmpeg_loc + os.pathsep + child_env.get("PATH", "")

        # Resolve the cookie source up-front. cookies_file wins over cookies_browser.
        cookies_browser = None
        cookies_file    = None
        if cookies_mode == "none":
            log("cookies", "Cookies disabled.", "dim")
        elif cookies_mode == "auto" or cookies_mode == "":
            cookies_browser = detect_browser_cookies()
        elif cookies_mode in BROWSERS:
            cookies_browser = cookies_mode
            log("cookies", f"Using {cookies_mode} cookies.", "ok")
        elif os.path.isfile(cookies_mode):
            cookies_file = cookies_mode
            log("cookies", f"Using cookies file: {cookies_mode}", "ok")
        else:
            log("cookies", f"Unknown cookies source '{cookies_mode}' - proceeding without.", "warn")

        success = fail = 0

        for i, item in enumerate(urls, 1):
            if _cancel.is_set():
                log("dl", "Download cancelled.", "warn")
                break
            url     = item.get("url", "") if isinstance(item, dict) else str(item)
            item_id = item.get("id", str(i)) if isinstance(item, dict) else str(i)
            # Resolved fresh per item so a rule never leaks across queue entries.
            site_rule = _resolve_site_rule(url, params.get("siteRules"))
            try:
                output_template = str(Path(output_dir) / "%(title)s.%(ext)s")

                if is_audio:
                    cmd = [
                        ytdlp_bin, "-f", "bestaudio/best",
                        "--extract-audio", "--audio-format", ytdlp_afmt,
                        "--audio-quality", "0",
                        "-o", output_template,
                    ]
                elif is_subs:
                    # Subtitles-only: no video, no audio. yt-dlp writes
                    # <title>.<lang>.<fmt> alongside the output template.
                    cmd = [
                        ytdlp_bin,
                        "--skip-download",
                        "--write-subs", "--write-auto-subs",
                        "--sub-langs", _sub_lang_selector(sub_lang),
                        "--convert-subs", sub_fmt_ytdlp,
                        "-o", output_template,
                    ]
                else:
                    fmt_map = {
                        "Best":  "bestvideo+bestaudio/best",
                        "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
                        "720p":  "bestvideo[height<=720]+bestaudio/best[height<=720]",
                        "480p":  "bestvideo[height<=480]+bestaudio/best[height<=480]",
                        "360p":  "bestvideo[height<=360]+bestaudio/best[height<=360]",
                    }
                    concurrent_fragments = "4"
                    if site_rule and site_rule.get("concurrentFragments"):
                        concurrent_fragments = str(site_rule["concurrentFragments"])
                    cmd = [
                        ytdlp_bin,
                        "-f", fmt_map.get(quality, "bestvideo+bestaudio/best"),
                        "--merge-output-format", video_fmt,
                        "--concurrent-fragments", concurrent_fragments,
                        "-o", output_template,
                    ]
                    # Codec preference. yt-dlp normalizes codec strings, so
                    # "h264", "h265", "vp9", "av1" all match cleanly via
                    # vcodec:<name>. "auto" leaves yt-dlp's default sort.
                    codec_sort = {
                        "h264": "vcodec:h264",
                        "h265": "vcodec:h265",
                        "vp9":  "vcodec:vp9",
                        "av1":  "vcodec:av01",
                    }.get(video_codec)
                    if codec_sort:
                        cmd.extend(["-S", codec_sort])

                if ffmpeg_loc:
                    cmd.extend(["--ffmpeg-location", ffmpeg_loc])
                if electron_node:
                    cmd.extend(["--js-runtimes", f"node:{electron_node}"])
                # yt-dlp (2025+) needs the yt-dlp-ejs challenge-solver scripts
                # to crack YouTube's n-challenge. They are NOT bundled, and
                # remote fetching is disabled by default, so without this flag
                # only image formats resolve. A JS runtime alone is not enough.
                cmd.extend(["--remote-components", "ejs:github"])
                cookie_args = []
                if cookies_file:
                    cookie_args = ["--cookies", cookies_file]
                elif cookies_browser:
                    cookie_args = ["--cookies-from-browser", cookies_browser]
                if proxy_url:
                    cmd.extend(["--proxy", proxy_url])
                if site_rule and site_rule.get("referer"):
                    cmd.extend(["--referer", site_rule["referer"]])

                # Cookie fallback. Browser/file cookies unlock private and
                # age-gated videos. If that attempt fails, retry once without
                # cookies before giving up.
                attempts = [cookie_args]
                if cookie_args:
                    attempts.append([])

                emit({"type": "item_state", "id": item_id,
                      "state": "downloading", "pct": 0, "speed": ""})
                log("yt-dlp", f"[{i}/{len(urls)}] {url}", "dim")

                downloaded_file = None
                cookie_read_error = False   # browser cookie DB unreadable
                bot_check_error   = False   # YouTube "sign in to confirm..."
                ejs_error         = False
                returncode        = 1
                # Bounds the output-folder glob fallback below so it can only
                # ever select a file this item actually produced.
                item_started_at   = time.time()

                for attempt_idx, ca in enumerate(attempts):
                    if attempt_idx > 0:
                        log("yt-dlp", "Extraction failed - retrying without cookies...", "warn")
                    # Reset per attempt: a cookie-DB error on attempt 1 must not
                    # explain an unrelated failure on the cookie-less retry.
                    cookie_read_error = False
                    bot_check_error   = False
                    ejs_error         = False
                    # Use yt-dlp's default client selection: forced Android
                    # clients return HTTP 403. The retry stays credential-free.
                    run_cmd = list(cmd)
                    run_cmd += ca + [url]

                    proc = subprocess.Popen(
                        run_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL,
                        text=True, bufsize=1, creationflags=creationflags,
                        env=child_env,
                    )
                    if not _register_child(proc, kind="download"):
                        # Shutdown began mid-spawn; proc is already killed.
                        break
                    downloaded_file = None

                    for line in proc.stdout:
                        if _cancel.is_set():
                            proc.terminate()
                            break
                        s = line.strip()
                        low = s.lower()
                        if low.startswith(("warning:", "error:")) and (any(marker in low for marker in (
                                "signature solving failed", "n challenge solving failed",
                                "supported javascript runtime", "challenge solver script"))
                                or ("runtime" in low and "unsupported" in low)):
                            ejs_error = True
                        if "[download]" in s and "%" in s:
                            prog = _parse_dl_progress(s)
                            if prog:
                                pct, speed = prog
                                emit({"type": "item_state", "id": item_id,
                                      "state": "downloading", "pct": pct, "speed": speed})
                        if "[Merger] Merging formats into" in s:
                            m = re.search(r'"([^"]+)"', s)
                            if m:
                                downloaded_file = m.group(1)
                        elif "[ExtractAudio] Destination:" in s:
                            downloaded_file = s.split("Destination:", 1)[1].strip()
                        elif "Writing video subtitles to:" in s and is_subs:
                            downloaded_file = s.split("to:", 1)[1].strip()
                        elif "[download] Destination:" in s and not is_audio and not is_subs:
                            candidate = s.split("Destination:", 1)[1].strip()
                            if candidate.endswith(f".{video_fmt}"):
                                downloaded_file = candidate
                        elif "has already been downloaded" in s:
                            candidate = (s.replace("[download]", "")
                                          .replace("has already been downloaded", "").strip())
                            if is_audio or candidate.endswith(f".{video_fmt}"):
                                downloaded_file = candidate
                        else:
                            low = s.lower()
                            # Two distinct failure classes that used to be
                            # collapsed into one misleading "cookies issue"
                            # message. Cookie-DB read failures (Chrome 127+
                            # app-bound encryption on Windows, or a browser
                            # holding a lock on the file) are actionable by
                            # switching cookie source. A YouTube bot check is
                            # not - it usually means a stale yt-dlp.
                            if ("failed to decrypt" in low
                                    or "could not copy" in low
                                    or "dpapi" in low
                                    or "app-bound" in low
                                    or "unsupported browser" in low
                                    or ("cookie" in low and "database" in low)):
                                cookie_read_error = True
                            elif ("sign in to confirm" in low
                                    or "not a bot" in low
                                    or "confirm your age" in low
                                    or "login required" in low
                                    or "private video" in low
                                    or "members-only" in low
                                    or "http error 403" in low):
                                bot_check_error = True
                        if s:
                            log("yt-dlp", s, "dim")

                    proc.wait()
                    _unregister_child(proc)
                    returncode = proc.returncode
                    if returncode == 0 or _cancel.is_set():
                        break

                # A user-initiated Stop terminates yt-dlp, so returncode is
                # non-zero. That is a cancellation, not a failure: reporting it
                # as one gave the user a red error row and "1 failed".
                if _cancel.is_set():
                    emit({"type": "item_state", "id": item_id, "state": "cancelled"})
                    break

                if returncode != 0:
                    fail += 1
                    err_msg = _download_error_message(
                        cookie_read_error, bot_check_error, ejs_error, node_runtime)
                    emit({"type": "item_state", "id": item_id,
                          "state": "error", "err": err_msg})
                    log("dl", err_msg, "err")
                    continue

                if downloaded_file is None or not os.path.exists(downloaded_file):
                    if is_audio:
                        pattern = f"*.{audio_fmt}"
                    elif is_subs:
                        pattern = f"*.{sub_lang}*.{sub_fmt_ytdlp}"
                    else:
                        pattern = f"*.{video_fmt}"
                    downloaded_file = _pick_fallback_file(
                        output_dir, pattern, item_started_at)
                    if downloaded_file is None:
                        fail += 1
                        msg = ("Download finished but the output file could not "
                               "be located - see the log for details.")
                        emit({"type": "item_state", "id": item_id,
                              "state": "error", "err": msg})
                        log("dl", msg, "err")
                        continue

                # Subs post-processing: dedupe YouTube's rolling auto-caption
                # cues and render out to the user's chosen final format.
                if (is_subs and downloaded_file and os.path.exists(downloaded_file)
                        and downloaded_file.endswith(".srt")):
                    try:
                        downloaded_file = _postprocess_subs(downloaded_file, sub_fmt_req)
                    except Exception as e:
                        log("subs", f"post-processing failed: {e}", "warn")

                if do_compress and downloaded_file and os.path.exists(downloaded_file):
                    if not os.path.isfile(hbcli_bin):
                        log("HandBrake",
                            "HandBrakeCLI not found - install via your package "
                            "manager (e.g. `sudo apt install handbrake-cli`) and "
                            "retry with compress enabled.", "err")
                    else:
                        orig_sz = os.path.getsize(downloaded_file)
                        orig_mb = orig_sz / (1024 * 1024)
                        if orig_sz > 0:
                            tmp_file = downloaded_file + ".tmp.mp4"
                            emit({"type": "item_state", "id": item_id,
                                  "state": "encoding", "pct": 0})
                            log("HandBrake",
                                f"Compressing {orig_mb:.1f} MB with H.265...", "dim")
                            hb_cmd = [
                                hbcli_bin, "-i", downloaded_file, "-o", tmp_file,
                                "-e", "x265", "-q", "31.5",
                                "--encoder-preset", "fast",
                                "-E", "aac", "-B", "192",
                            ]
                            hb_proc = subprocess.Popen(
                                hb_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL,
                                text=True, bufsize=1, creationflags=creationflags,
                                env=child_env,
                            )
                            if not _register_child(hb_proc, kind="download"):
                                # hb_proc is already killed and reaped, so the
                                # temp file is no longer held open. Follow the
                                # same cleanup as an interrupted encode so the
                                # row reads "stopped" rather than falling into
                                # the generic handler as an unexpected error.
                                try:
                                    if os.path.exists(tmp_file):
                                        os.remove(tmp_file)
                                except OSError:
                                    pass
                                log("HandBrake",
                                    "Compression cancelled. Original kept.", "warn")
                                emit({"type": "item_state", "id": item_id,
                                      "state": "cancelled"})
                                break
                            for hb_line in hb_proc.stdout:
                                if _cancel.is_set():
                                    hb_proc.terminate()
                                    break
                                s = hb_line.replace("\r", "\n").strip()
                                pct = _parse_hb_progress(s)
                                if pct is not None:
                                    emit({"type": "item_state", "id": item_id,
                                          "state": "encoding", "pct": pct})
                            hb_proc.wait()
                            _unregister_child(hb_proc)
                            if _cancel.is_set():
                                # Same Windows hazard: a terminated HandBrakeCLI
                                # may still hold the temp file briefly.
                                try:
                                    if os.path.exists(tmp_file):
                                        os.remove(tmp_file)
                                except OSError:
                                    pass
                                log("HandBrake", "Compression cancelled. Original kept.", "warn")
                                emit({"type": "item_state", "id": item_id,
                                      "state": "cancelled"})
                                break
                            if hb_proc.returncode == 0:
                                new_sz = os.path.getsize(tmp_file)
                                new_mb = new_sz / (1024 * 1024)
                                if new_sz < orig_sz:
                                    os.replace(tmp_file, downloaded_file)
                                    log("HandBrake",
                                        f"{orig_mb:.1f} MB → {new_mb:.1f} MB saved.", "ok")
                                else:
                                    os.remove(tmp_file)
                                    log("HandBrake",
                                        "Original already optimal - kept as-is.", "dim")
                            else:
                                log("HandBrake",
                                    "Compression failed. Original kept.", "err")
                                if os.path.exists(tmp_file):
                                    os.remove(tmp_file)

                file_size = (os.path.getsize(downloaded_file)
                             if downloaded_file and os.path.exists(downloaded_file) else 0)
                emit({"type": "item_state", "id": item_id,
                      "state": "done", "pct": 100, "size": file_size})
                log("save", f"→ {downloaded_file}", "ok")
                success += 1
            except Exception as e:
                log("dl", f"Unexpected error: {e}", "err")
                emit({"type": "item_state", "id": item_id,
                      "state": "error", "err": str(e)})
                fail += 1

        log("dl", f"Queue complete - {success} succeeded, {fail} failed.", "ok")
        emit({"type": "download_complete", "success": success, "fail": fail})
    finally:
        with _busy_lock:
            _busy = False


# ── YouTube search ─────────────────────────────────────────────────────────
_SEARCH_PAGE_SIZE = 10
_search_lock = threading.Lock()
_search_proc = None
# Monotonic ticket. Each search claims one; only the newest claimant is allowed
# to emit. Without it a slow page N-1 still sitting in communicate() can land
# after page N and repaint the pager with stale rows.
_search_seq  = 0

def _run_search(params):
    global _search_proc, _search_seq
    query = (params.get("query") or "").strip()
    try:
        start = max(1, int(params.get("start") or 1))
    except (ValueError, TypeError):
        start = 1
    if not query:
        emit({"type": "search_results", "query": query, "start": start,
              "has_more": False, "results": []})
        return

    with _search_lock:
        _search_seq += 1
        my_seq = _search_seq
        if _search_proc and _search_proc.poll() is None:
            try:
                _search_proc.terminate()
            except Exception:
                pass

    def _is_current():
        with _search_lock:
            return my_seq == _search_seq

    def _emit_results(results, has_more):
        # The generation check and the emit are done under one lock: checking
        # first and emitting after leaves a window where a newer search claims
        # the generation in between, and the stale payload still goes out.
        with _search_lock:
            if my_seq != _search_seq:
                return
            emit({"type": "search_results", "query": query, "start": start,
                  "has_more": has_more, "results": results})

    try:
        if not _tools_ready.is_set():
            log("search", "Still preparing yt-dlp - try again in a moment.", "warn")
            _emit_results([], False)
            return
        ytdlp = get_tool("yt-dlp")
        url = f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(query)}"
        # Fetch one extra to reliably detect whether there are more results,
        # and to absorb any off-by-one from yt-dlp's playlist-end handling.
        end = start + _SEARCH_PAGE_SIZE
        p = subprocess.Popen(
            [ytdlp, url, "--flat-playlist", "--dump-json", "--no-warnings",
             "--playlist-start", str(start), "--playlist-end", str(end)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, text=True,
            creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
        )
        # Claim the slot only if this thread still owns the newest generation.
        # A slower thread reaching this point after a newer search started
        # would otherwise overwrite the newer handle, so the next terminate()
        # would target the wrong process and leave the real one running.
        with _search_lock:
            if my_seq != _search_seq:
                p.kill()
                p.communicate()
                return
            _search_proc = p
        if not _register_child(p, kind="search"):
            # Process teardown began while we were spawning; p is already killed.
            return
        try:
            stdout, stderr = p.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            # communicate() does NOT kill the child on timeout; without this the
            # yt-dlp process outlives the search and _search_proc points at a
            # stale handle the next search would try to terminate.
            p.kill()
            p.communicate()
            # Only the current search may speak: a superseded thread timing out
            # would otherwise print an error about a search the user abandoned.
            # Check and log under one lock - releasing _search_lock first lets a
            # newer search start in between and receive the stale warning. Lock
            # order here is _search_lock -> _out_lock, matching _emit_results;
            # there is no path that takes them in the reverse order.
            with _search_lock:
                if my_seq == _search_seq:
                    log("search", "Search timed out after 60s.", "err")
            _emit_results([], False)
            return
        finally:
            _unregister_child(p)
            with _search_lock:
                if _search_proc is p:
                    _search_proc = None
        if p.returncode != 0:
            # A terminate() from a newer search lands here; that is not an error
            # worth showing, and _emit_results already suppresses the payload.
            if _is_current():
                err_msg = (stderr or "").strip().splitlines()
                err_short = err_msg[-1] if err_msg else "unknown error"
                log("search", f"yt-dlp search failed (exit {p.returncode}): {err_short}", "err")
            _emit_results([], False)
            return
        results = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            vid_id = item.get("id", "")
            if not vid_id:
                continue
            results.append({
                "id": vid_id,
                "title": item.get("title", ""),
                "uploader": item.get("uploader") or item.get("channel") or "",
                "duration": item.get("duration"),
                "thumbnail": f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg",
                "webpageUrl": f"https://www.youtube.com/watch?v={vid_id}",
            })
        has_more = len(results) > _SEARCH_PAGE_SIZE
        results = results[:_SEARCH_PAGE_SIZE]
        _emit_results(results, has_more)
    except Exception as e:
        if _is_current():
            log("search", f"Search error: {e}", "err")
        _emit_results([], False)


# ── command dispatcher (stdin reader loop) ────────────────────────────────
def handle_command(cmd_obj):
    global _busy
    cmd = cmd_obj.get("cmd")

    if cmd == "start_download":
        with _busy_lock:
            if _busy:
                log("dl", "A download is already in progress.", "warn")
                return
            _busy = True
        _cancel.clear()
        threading.Thread(target=_run_download, args=(cmd_obj.get("params") or {},),
                         daemon=True).start()

    elif cmd == "cancel_download":
        _cancel.set()
        # The read loops only notice _cancel between lines, and a stalled
        # yt-dlp or a long HandBrake pass may not emit one for minutes.
        # Terminating here makes Stop take effect immediately. Scoped to
        # download children so an in-flight search is left alone.
        _kill_children(kind="download")

    elif cmd == "search":
        threading.Thread(target=_run_search, args=(cmd_obj.get("params") or {},),
                         daemon=True).start()


def main():
    # unbuffered stdout so events reach Electron the instant they're emitted.
    # UTF-8 is forced here too: emit() uses ensure_ascii=False, so a non-ASCII
    # char in any log line would raise UnicodeEncodeError on a cp1252 Windows
    # console and kill the emitting thread. Electron sets PYTHONIOENCODING,
    # but this keeps the backend safe when run outside it.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace",
                               line_buffering=True)
    except Exception:
        pass

    # Electron sends SIGTERM on quit. Without a handler the interpreter dies
    # while yt-dlp / HandBrakeCLI keep running as orphans.
    def _on_signal(signum, frame):
        _shutdown.set()
        _cancel.set()
        _kill_children()
        sys.exit(0)

    for _sig in ("SIGTERM", "SIGINT", "SIGHUP"):
        s = getattr(signal, _sig, None)
        if s is not None:
            try:
                signal.signal(s, _on_signal)
            except (ValueError, OSError):
                pass

    emit({"type": "ready", "version": __version__})

    # Kick off yt-dlp check immediately.
    threading.Thread(target=_run_ytdlp_check, daemon=True).start()

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception as e:
                log("backend", f"bad command line: {e}", "err")
                continue
            try:
                handle_command(obj)
            except Exception as e:
                log("backend", f"command {obj.get('cmd')} failed: {e}", "err")
    finally:
        # stdin EOF means Electron closed the pipe - i.e. the app is quitting.
        _shutdown.set()
        _cancel.set()
        _kill_children()


if __name__ == "__main__":
    main()
