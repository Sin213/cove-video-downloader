#!/usr/bin/env python3
"""Cove Video Downloader — JSON-lines backend.

Reads commands (one JSON object per line) on stdin, writes events (one JSON
object per line) on stdout. The Electron main process spawns this script
and wires stdout lines to IPC events delivered to the renderer.

Commands (Electron → Python, one JSON per stdin line):
    {"cmd": "check_updates"}
    {"cmd": "start_download", "params": {...}}
    {"cmd": "cancel_download"}

Events (Python → Electron, one JSON per stdout line):
    {"type": "ready"}
    {"type": "log", "tag": "...", "tone": "ok|err|warn|dim", "msg": "..."}
    {"type": "item_state", "id": "...", "state": "...", "pct": 0-100, ...}
    {"type": "tools_ready", "ytdlp_tag": "...", "ffmpeg_ver": "...", "hb_ver": "..."}
    {"type": "download_complete", "success": N, "fail": N}
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

# Make `ssl_context.py` importable whether we're running from the checkout
# (python/ dir) or from an installed location (alongside backend.py).
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from ssl_context import get_ssl_context

__version__ = "1.2.0"


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
    if sys.platform == "win32":
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
    resources/runtime/ — two levels up, not one."""
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
    # 3. Resolve from PATH — returns an absolute path when found, or the
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
        return m.group(1) if m else "—"
    except Exception:
        return "—"


# ── yt-dlp updater ─────────────────────────────────────────────────────────
YTDLP_API   = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
YTDLP_VER_F = TOOLS_DIR / "yt-dlp.version"
YTDLP_EXE   = TOOLS_DIR / ("yt-dlp.exe" if sys.platform == "win32" else "yt-dlp")

def _ytdlp_current_tag():
    return YTDLP_VER_F.read_text().strip() if YTDLP_VER_F.exists() else ""

def _ytdlp_fetch_latest():
    req = urllib.request.Request(YTDLP_API, headers={"User-Agent": "CoveVideoDownloader"})
    with urllib.request.urlopen(req, timeout=10, context=get_ssl_context()) as r:
        data = json.loads(r.read())
    tag      = data["tag_name"]
    exe_name = "yt-dlp.exe" if sys.platform == "win32" else "yt-dlp"
    url      = next(a["browser_download_url"] for a in data["assets"] if a["name"] == exe_name)
    return tag, url

def _download_to(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "CoveVideoDownloader"})
    with urllib.request.urlopen(req, timeout=30, context=get_ssl_context()) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


# ── cookie detection ───────────────────────────────────────────────────────
# Detection is filesystem-based: we look for the cookie database yt-dlp would
# read. This avoids a network round-trip per browser (the old `--simulate`
# approach hit YouTube and mis-reported unusable-but-installed browsers), and
# it sidesteps Windows Chrome 127+ app-bound encryption giving fast non-zero
# exits that our phrase matcher couldn't recognize. Firefox is checked first
# because its cookies are plain SQLite — no keyring/DPAPI decryption — so it's
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

def detect_browser_cookies(ytdlp_bin):
    log("cookies", "Detecting browser cookies...", "dim")
    try:
        for browser in BROWSERS:
            for pattern in _browser_cookie_globs(browser):
                s = str(pattern)
                hits = glob.glob(s) if "*" in s else ([s] if os.path.exists(s) else [])
                if hits:
                    log("cookies", f"Auto-detected: {browser}", "ok")
                    return browser
        log("cookies", "No browser cookies found — proceeding without.", "dim")
        return None
    except Exception as e:
        log("cookies", f"Detection skipped: {e}", "warn")
        return None


# ── download pipeline ──────────────────────────────────────────────────────
_cancel = threading.Event()
_busy   = False
_busy_lock = threading.Lock()

def _run_ytdlp_check():
    try:
        log("yt-dlp", "Checking for updates...", "dim")
        tag, url = _ytdlp_fetch_latest()
        current  = _ytdlp_current_tag()
        if current == tag and YTDLP_EXE.exists():
            log("yt-dlp", f"{tag} already up to date.", "ok")
        else:
            action = "Updating" if YTDLP_EXE.exists() else "Downloading"
            log("yt-dlp", f"{action} {tag}...", "dim")
            tmp = YTDLP_EXE.with_suffix(".tmp")
            _download_to(url, tmp)
            shutil.move(str(tmp), str(YTDLP_EXE))
            if sys.platform != "win32":
                YTDLP_EXE.chmod(0o755)
            YTDLP_VER_F.write_text(tag)
            log("yt-dlp", f"{tag} installed.", "ok")

        ytdlp_tag  = _ytdlp_current_tag() or tag
        ffmpeg_ver = get_tool_version([get_tool("ffmpeg"), "-version"])
        hb_ver     = get_tool_version([get_tool("HandBrakeCLI"), "--version"])

        log("tools", f"yt-dlp    → {get_tool('yt-dlp')}", "dim")
        log("tools", f"ffmpeg    → {get_tool('ffmpeg')}", "dim")
        log("tools", f"HandBrake → {get_tool('HandBrakeCLI')}", "dim")
        emit({
            "type": "tools_ready",
            "ytdlp_tag":  ytdlp_tag,
            "ffmpeg_ver": ffmpeg_ver,
            "hb_ver":     hb_ver,
        })
        log("ready", "Ready. Paste links above to begin.", "ok")
    except Exception as e:
        log("yt-dlp", f"Auto-update failed: {e}", "err")
        if YTDLP_EXE.exists():
            log("yt-dlp", "Using existing version.", "warn")
            log("ready", "Ready (update check failed).", "ok")
        else:
            log("yt-dlp", "yt-dlp not found — check internet.", "err")

def _run_download(params):
    global _busy
    try:
        urls       = params.get("urls") or []
        quality    = params.get("quality", "Best")
        compress   = bool(params.get("compress", False))
        save_to    = params.get("savePath") or str(Path.home() / "Downloads")
        audio_req  = (params.get("audioFormat") or "mp3").lower()

        is_audio   = (quality == "Audio")
        _amap      = {"mp3": ("mp3", "mp3"), "ogg": ("vorbis", "ogg"), "opus": ("opus", "opus")}
        ytdlp_afmt, audio_fmt = _amap.get(audio_req, ("mp3", "mp3"))
        do_compress = compress and not is_audio

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
        child_env = os.environ.copy()
        if electron_node:
            child_env["ELECTRON_RUN_AS_NODE"] = "1"
        if ffmpeg_loc:
            child_env["PATH"] = ffmpeg_loc + os.pathsep + child_env.get("PATH", "")

        detected_browser = detect_browser_cookies(ytdlp_bin)
        success = fail = 0

        for i, item in enumerate(urls, 1):
            if _cancel.is_set():
                log("dl", "Download cancelled.", "warn")
                break
            url     = item.get("url", "") if isinstance(item, dict) else str(item)
            item_id = item.get("id", str(i)) if isinstance(item, dict) else str(i)
            try:
                output_template = str(Path(output_dir) / "%(title)s.%(ext)s")

                if is_audio:
                    cmd = [
                        ytdlp_bin, "-f", "bestaudio/best",
                        "--extract-audio", "--audio-format", ytdlp_afmt,
                        "--audio-quality", "0",
                        "-o", output_template,
                    ]
                else:
                    fmt_map = {
                        "Best":  "bestvideo+bestaudio/best",
                        "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
                        "720p":  "bestvideo[height<=720]+bestaudio/best[height<=720]",
                        "480p":  "bestvideo[height<=480]+bestaudio/best[height<=480]",
                    }
                    cmd = [
                        ytdlp_bin,
                        "-f", fmt_map.get(quality, "bestvideo+bestaudio/best"),
                        "--merge-output-format", "mp4",
                        "--concurrent-fragments", "4",
                        "-o", output_template,
                    ]
                    if not detected_browser:
                        cmd.extend(["--extractor-args",
                                    "youtube:player_client=android_vr,android"])

                if ffmpeg_loc:
                    cmd.extend(["--ffmpeg-location", ffmpeg_loc])
                if electron_node:
                    cmd.extend(["--js-runtimes", f"node:{electron_node}"])
                if detected_browser:
                    cmd.extend(["--cookies-from-browser", detected_browser])
                cmd.append(url)

                emit({"type": "item_state", "id": item_id,
                      "state": "downloading", "pct": 0, "speed": ""})
                log("yt-dlp", f"[{i}/{len(urls)}] {url}", "dim")

                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    text=True, bufsize=1, creationflags=creationflags,
                    env=child_env,
                )
                downloaded_file = None
                cookie_error    = False

                for line in proc.stdout:
                    if _cancel.is_set():
                        proc.terminate()
                        break
                    s = line.strip()
                    if "[download]" in s and "%" in s:
                        m = re.search(r"(\d+\.?\d*)%.*?(\d+\.?\d+\s*\w+/s)", s)
                        if m:
                            pct   = float(m.group(1))
                            speed = m.group(2)
                            emit({"type": "item_state", "id": item_id,
                                  "state": "downloading", "pct": pct, "speed": speed})
                    if "[Merger] Merging formats into" in s:
                        m = re.search(r'"([^"]+)"', s)
                        if m:
                            downloaded_file = m.group(1)
                    elif "[ExtractAudio] Destination:" in s:
                        downloaded_file = s.split("Destination:", 1)[1].strip()
                    elif "[download] Destination:" in s and not is_audio:
                        candidate = s.split("Destination:", 1)[1].strip()
                        if candidate.endswith(".mp4"):
                            downloaded_file = candidate
                    elif "has already been downloaded" in s:
                        candidate = (s.replace("[download]", "")
                                      .replace("has already been downloaded", "").strip())
                        if is_audio or candidate.endswith(".mp4"):
                            downloaded_file = candidate
                    elif "login" in s.lower() or "sign in" in s.lower() or "403" in s:
                        cookie_error = True
                    if s:
                        log("yt-dlp", s, "dim")

                proc.wait()
                if proc.returncode != 0:
                    fail += 1
                    err_msg = ("Login required — sign in via Firefox/Chrome then retry."
                               if cookie_error else "Download failed.")
                    emit({"type": "item_state", "id": item_id,
                          "state": "error", "err": err_msg})
                    log("dl", err_msg, "err")
                    continue

                if downloaded_file is None or not os.path.exists(downloaded_file):
                    pattern = f"*.{audio_fmt}" if is_audio else "*.mp4"
                    candidates = sorted(
                        Path(output_dir).glob(pattern),
                        key=lambda p: p.stat().st_mtime, reverse=True,
                    )
                    if candidates:
                        downloaded_file = str(candidates[0])

                if do_compress and downloaded_file and os.path.exists(downloaded_file):
                    if not os.path.isfile(hbcli_bin):
                        log("HandBrake",
                            "HandBrakeCLI not found — install via your package "
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
                            for hb_line in hb_proc.stdout:
                                s = hb_line.replace("\r", "\n").strip()
                                m = re.search(r"(\d+\.?\d+)\s*%", s)
                                if m:
                                    emit({"type": "item_state", "id": item_id,
                                          "state": "encoding", "pct": float(m.group(1))})
                            hb_proc.wait()
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
                                        "Original already optimal — kept as-is.", "dim")
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

        log("dl", f"Queue complete — {success} succeeded, {fail} failed.", "ok")
        emit({"type": "download_complete", "success": success, "fail": fail})
    finally:
        with _busy_lock:
            _busy = False


# ── command dispatcher (stdin reader loop) ────────────────────────────────
def handle_command(cmd_obj):
    global _busy
    cmd = cmd_obj.get("cmd")

    if cmd == "check_updates":
        threading.Thread(target=_run_ytdlp_check, daemon=True).start()

    elif cmd == "start_download":
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

    elif cmd == "version":
        emit({"type": "version", "version": __version__})


def main():
    # unbuffered stdout so events reach Electron the instant they're emitted
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    emit({"type": "ready", "version": __version__})

    # Kick off yt-dlp check immediately.
    threading.Thread(target=_run_ytdlp_check, daemon=True).start()

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


if __name__ == "__main__":
    main()
