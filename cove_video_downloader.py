#!/usr/bin/env python3
import subprocess
import threading
import tkinter as tk
from tkinter import messagebox
from pathlib import Path
import re
import os
import sys
import urllib.request
import json
import shutil

# ── Color palette (dark theme) ─────────────────────────────────────────────
BG          = "#141312"
SURFACE     = "#1c1b19"
SURFACE2    = "#252321"
BORDER      = "#333130"
TEXT        = "#cdccca"
TEXT_MUTED  = "#797876"
ORANGE      = "#ffaa00"
ORANGE_DIM  = "#cc8800"
LOG_BG      = "#0f0e0d"

# ── Tool directory (writable, next to EXE or in AppData) ──────────────────
def get_tools_dir():
    """Return a writable folder to store yt-dlp.exe and HandBrakeCLI.exe."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home())) / "CoveVideoDownloader"
    else:
        base = Path.home() / ".cove-video-downloader"
    base.mkdir(parents=True, exist_ok=True)
    return base

TOOLS_DIR = get_tools_dir()

def resource_path(relative):
    """Resolve path to bundled resource — works in dev and PyInstaller EXE."""
    try:
        base = sys._MEIPASS
    except AttributeError:
        base = os.path.abspath(".")
    return os.path.join(base, relative)

def get_tool(name):
    """
    Find a CLI tool. Priority:
      1. TOOLS_DIR (auto-downloaded / updated)
      2. Bundled inside EXE (HandBrakeCLI only)
      3. System PATH
    """
    ext = ".exe" if sys.platform == "win32" else ""
    managed = TOOLS_DIR / (name + ext)
    if managed.exists():
        return str(managed)
    if sys.platform == "win32":
        bundled = resource_path(name + ".exe")
        if os.path.exists(bundled):
            return bundled
    return name  # system PATH fallback

# ── yt-dlp auto-updater ───────────────────────────────────────────────────
YTDLP_API   = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
YTDLP_VER_F = TOOLS_DIR / "yt-dlp.version"
YTDLP_EXE   = TOOLS_DIR / ("yt-dlp.exe" if sys.platform == "win32" else "yt-dlp")

def _ytdlp_current_tag():
    if YTDLP_VER_F.exists():
        return YTDLP_VER_F.read_text().strip()
    return ""

def _ytdlp_fetch_latest():
    """Return (tag, download_url) for the latest yt-dlp release."""
    req = urllib.request.Request(YTDLP_API,
          headers={"User-Agent": "CoveVideoDownloader"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    tag = data["tag_name"]
    exe_name = "yt-dlp.exe" if sys.platform == "win32" else "yt-dlp"
    url = next(
        a["browser_download_url"]
        for a in data["assets"]
        if a["name"] == exe_name
    )
    return tag, url

def ensure_ytdlp(status_cb, log_cb):
    """
    Called in a background thread at startup.
    Downloads yt-dlp if missing or outdated, then sets it executable.
    """
    try:
        status_cb("Checking for yt-dlp updates...")
        tag, url = _ytdlp_fetch_latest()
        current  = _ytdlp_current_tag()

        if current == tag and YTDLP_EXE.exists():
            status_cb(f"yt-dlp {tag} is up to date.")
            log_cb(f"[yt-dlp] {tag} already installed.\n")
            return

        action = "Updating" if YTDLP_EXE.exists() else "Downloading"
        status_cb(f"{action} yt-dlp {tag}...")
        log_cb(f"[yt-dlp] {action} {tag}...\n")

        tmp = YTDLP_EXE.with_suffix(".tmp")
        urllib.request.urlretrieve(url, tmp)
        shutil.move(str(tmp), str(YTDLP_EXE))
        if sys.platform != "win32":
            YTDLP_EXE.chmod(0o755)
        YTDLP_VER_F.write_text(tag)

        status_cb(f"yt-dlp {tag} ready.")
        log_cb(f"[yt-dlp] {tag} installed successfully.\n")

    except Exception as e:
        msg = f"[yt-dlp] Auto-update failed: {e}\n"
        log_cb(msg)
        if not YTDLP_EXE.exists():
            status_cb("yt-dlp download failed — check your internet connection.")
        else:
            status_cb("yt-dlp update check failed (using existing version).")

# ── Icon ──────────────────────────────────────────────────────────────────
def set_icon(root):
    try:
        from tkinter import PhotoImage
        path = resource_path("cove_icon.png")
        if os.path.exists(path):
            img = PhotoImage(file=path)
            root.iconphoto(False, img)
            root._icon_ref = img
    except Exception:
        pass

# ── Download logic ─────────────────────────────────────────────────────────
def download_videos():
    raw_text = urls_text.get("1.0", tk.END)
    urls = [line.strip() for line in raw_text.split("\n") if line.strip()]
    if not urls:
        messagebox.showwarning("No links", "Please paste at least one video link first.")
        return

    if not YTDLP_EXE.exists():
        messagebox.showerror("yt-dlp not ready",
            "yt-dlp hasn't finished downloading yet. Please wait a moment and try again.")
        return

    browser     = browser_var.get()
    do_compress = compress_var.get()

    download_btn.config(state=tk.DISABLED)
    status_var.set(f"Processing {len(urls)} video(s)...")

    def run():
        success = 0
        fail    = 0
        log_clear()

        ytdlp_bin = get_tool("yt-dlp")
        hbcli_bin = get_tool("HandBrakeCLI")

        for i, url in enumerate(urls, 1):
            try:
                output_template = str(Path.home() / "Downloads" / "%(title)s.%(ext)s")
                cmd = [
                    ytdlp_bin,
                    "-f", "bv*+ba/b",
                    "--merge-output-format", "mp4",
                    "-o", output_template,
                ]
                if browser != "None (Default)":
                    cmd.extend(["--cookies-from-browser", browser.lower()])
                cmd.append(url)

                log_write(f"\n{'='*52}\n")
                log_write(f"  [{i}/{len(urls)}] Downloading\n  {url}\n")
                log_write(f"{'='*52}\n\n")

                creationflags = 0
                if sys.platform == "win32":
                    creationflags = subprocess.CREATE_NO_WINDOW

                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, bufsize=1,
                    creationflags=creationflags,
                )

                downloaded_file = None
                for line in proc.stdout:
                    log_write(line)
                    s = line.strip()
                    if "[Merger] Merging formats into" in s:
                        m = re.search(r'\"([^\"]+)\"', s)
                        if m:
                            downloaded_file = m.group(1)
                    elif "[download] Destination:" in s and s.endswith(".mp4"):
                        downloaded_file = s.split("Destination:")[1].strip()
                    elif "has already been downloaded" in s:
                        downloaded_file = s.replace("[download]", "").replace("has already been downloaded", "").strip()
                proc.wait()

                if proc.returncode == 0:
                    if do_compress and downloaded_file and os.path.exists(downloaded_file):
                        orig_sz  = os.path.getsize(downloaded_file)
                        orig_mb  = orig_sz / (1024 * 1024)
                        tmp_file = downloaded_file + ".tmp.mp4"

                        log_write(f"\n--- Compressing (H.265 Web Balance) ---\n")
                        log_write(f"Original: {orig_mb:.1f} MB\n")

                        hb_cmd = [
                            hbcli_bin,
                            "-i", downloaded_file, "-o", tmp_file,
                            "-e", "x265", "-q", "31.5",
                            "--encoder-preset", "fast",
                            "-E", "aac", "-B", "192",
                        ]
                        hb_proc = subprocess.Popen(
                            hb_cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1,
                            creationflags=creationflags,
                        )
                        for hb_line in hb_proc.stdout:
                            log_write(hb_line.replace("\r", "\n"))
                        hb_proc.wait()

                        if hb_proc.returncode == 0:
                            new_sz = os.path.getsize(tmp_file)
                            new_mb = new_sz / (1024 * 1024)
                            if new_sz < orig_sz:
                                os.replace(tmp_file, downloaded_file)
                                log_write(f"\n[OK] {orig_mb:.1f}MB → {new_mb:.1f}MB saved!\n")
                            else:
                                os.remove(tmp_file)
                                log_write(f"\n[SKIP] Original already optimal ({orig_mb:.1f}MB). Kept as-is.\n")
                        else:
                            log_write("\n[ERROR] Compression failed. Original kept.\n")
                            if os.path.exists(tmp_file):
                                os.remove(tmp_file)

                    success += 1
                else:
                    fail += 1

            except FileNotFoundError:
                log_write("[ERROR] yt-dlp or HandBrakeCLI not found.\n")
                status_var.set("Error: tool not found.")
                download_btn.config(state=tk.NORMAL)
                return

        status_var.set(f"Done — {success} succeeded, {fail} failed.")
        download_btn.config(state=tk.NORMAL)

    threading.Thread(target=run, daemon=True).start()

# ── Log helpers ────────────────────────────────────────────────────────────
def log_write(text):
    log_text.config(state=tk.NORMAL)
    log_text.insert(tk.END, text)
    log_text.see(tk.END)
    log_text.config(state=tk.DISABLED)

def log_clear():
    log_text.config(state=tk.NORMAL)
    log_text.delete("1.0", tk.END)
    log_text.config(state=tk.DISABLED)

def smart_paste(text=None):
    if text is None:
        try:
            text = root.clipboard_get().strip()
        except tk.TclError:
            return
    if not text:
        return
    current = urls_text.get("1.0", tk.END).strip()
    urls_text.insert(tk.END, ("\n" if current else "") + text)

def handle_ctrl_v(event):
    smart_paste()
    return "break"

def clear_links():
    urls_text.delete("1.0", tk.END)

# ── Root window ────────────────────────────────────────────────────────────
root = tk.Tk()
root.title("Cove Video Downloader")
root.geometry("860x600")
root.configure(bg=BG)
root.resizable(True, True)
set_icon(root)

browser_var  = tk.StringVar(value="None (Default)")
compress_var = tk.BooleanVar(value=True)
status_var   = tk.StringVar(value="Checking yt-dlp...")

def lbl(parent, text, size=9, bold=False, color=TEXT, **kw):
    weight = "bold" if bold else "normal"
    return tk.Label(parent, text=text, bg=BG, fg=color,
                    font=("Consolas", size, weight), **kw)

def btn(parent, text, cmd, accent=False, **kw):
    bg_c = SURFACE2 if not accent else ORANGE_DIM
    fg_c = ORANGE   if not accent else BG
    b = tk.Button(parent, text=text, command=cmd,
                  bg=bg_c, fg=fg_c, activebackground=BORDER,
                  activeforeground=ORANGE, relief=tk.FLAT,
                  font=("Consolas", 9, "bold"), padx=10, pady=4,
                  cursor="hand2", **kw)
    b.bind("<Enter>", lambda e: b.config(bg=BORDER))
    b.bind("<Leave>", lambda e: b.config(bg=bg_c))
    return b

outer = tk.Frame(root, bg=BG, padx=14, pady=12)
outer.pack(fill=tk.BOTH, expand=True)

url_row = tk.Frame(outer, bg=BG)
url_row.pack(fill=tk.X)
lbl(url_row, "Video links:", bold=True).pack(side=tk.LEFT)
btn(url_row, "Clear", clear_links).pack(side=tk.RIGHT)
btn(url_row, "Paste", smart_paste).pack(side=tk.RIGHT, padx=(0, 6))

urls_text = tk.Text(
    outer, height=8,
    bg=SURFACE, fg=TEXT, insertbackground=ORANGE,
    relief=tk.FLAT, font=("Consolas", 9),
    padx=8, pady=6,
    highlightthickness=1, highlightbackground=BORDER,
    highlightcolor=ORANGE,
)
urls_text.pack(fill=tk.X, pady=(6, 10))
urls_text.bind("<Control-v>", handle_ctrl_v)
urls_text.bind("<Control-V>", handle_ctrl_v)

opts = tk.Frame(outer, bg=BG)
opts.pack(fill=tk.X, pady=(0, 10))
lbl(opts, "Unlock NSFW:").pack(side=tk.LEFT)
browser_menu = tk.OptionMenu(
    opts, browser_var,
    "None (Default)", "Firefox", "Chrome", "Brave", "Chromium", "Edge",
)
browser_menu.config(
    bg=SURFACE2, fg=TEXT, activebackground=BORDER,
    activeforeground=ORANGE, relief=tk.FLAT,
    font=("Consolas", 9), highlightthickness=0, padx=8,
)
browser_menu["menu"].config(
    bg=SURFACE2, fg=TEXT, activebackground=BORDER, activeforeground=ORANGE,
    font=("Consolas", 9),
)
browser_menu.pack(side=tk.LEFT, padx=(6, 20))

compress_cb = tk.Checkbutton(
    opts, text="Compress",
    variable=compress_var,
    bg=BG, fg=ORANGE, selectcolor=SURFACE2,
    activebackground=BG, activeforeground=ORANGE,
    font=("Consolas", 9, "bold"),
    cursor="hand2",
)
compress_cb.pack(side=tk.LEFT)

download_btn = btn(outer, "⬇  Download", download_videos, accent=True)
download_btn.config(font=("Consolas", 11, "bold"), pady=8)
download_btn.pack(anchor="center", pady=(4, 10), ipadx=30)

status_lbl = tk.Label(
    outer, textvariable=status_var,
    bg=BG, fg=TEXT_MUTED, anchor="w",
    font=("Consolas", 8),
)
status_lbl.pack(fill=tk.X, pady=(0, 6))

lbl(outer, "Log:", bold=True).pack(anchor="w")

log_text = tk.Text(
    outer,
    state=tk.DISABLED,
    bg=LOG_BG, fg=ORANGE,
    insertbackground=ORANGE,
    relief=tk.FLAT,
    font=("Consolas", 8),
    padx=8, pady=6,
    highlightthickness=1, highlightbackground=BORDER,
    highlightcolor=ORANGE,
)
log_text.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

# ── Kick off yt-dlp auto-update in background on startup ───────────────────
def _status_cb(msg):
    root.after(0, lambda: status_var.set(msg))

def _log_cb(msg):
    root.after(0, lambda: log_write(msg))

threading.Thread(
    target=ensure_ytdlp,
    args=(_status_cb, _log_cb),
    daemon=True,
).start()

root.mainloop()
