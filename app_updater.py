"""Self-updater for cove-video-downloader.

Separate from the yt-dlp updater already in the main script. This one polls
this app's own GitHub releases and offers the user an update.

AppImage installs get an in-place download + swap + relaunch. Other
packagings open the release page in the browser.

Uses tkinter (no Qt dependency) because the main app is tkinter-based.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import tkinter as tk
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk


REPO = "Sin213/cove-video-downloader"
APP_DISPLAY_NAME = "Cove Video Downloader"
CACHE_SUBDIR = "cove-video-downloader"


@dataclass
class UpdateInfo:
    latest_version: str
    release_url: str
    asset_name: str | None = None
    asset_url: str | None = None
    asset_size: int = 0


def _parse_version(v: str) -> tuple[int, int, int]:
    v = v.strip().lstrip("vV")
    out: list[int] = []
    for part in v.split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits) if digits else 0)
        if len(out) == 3:
            break
    while len(out) < 3:
        out.append(0)
    return (out[0], out[1], out[2])


def version_newer(latest: str, current: str) -> bool:
    return _parse_version(latest) > _parse_version(current)


def bundle_kind() -> str:
    if os.environ.get("APPIMAGE"):
        return "appimage"
    if sys.platform == "win32":
        if not getattr(sys, "frozen", False):
            return "source"
        exe_str = str(Path(sys.executable).resolve())
        if "Program Files" in exe_str or r"AppData\Local" in exe_str:
            return "win-setup"
        return "win-portable"
    if sys.platform.startswith("linux") and getattr(sys, "frozen", False):
        return "deb"
    return "source"


def _preferred_asset(kind: str, assets: list[dict]) -> dict | None:
    def first_match(predicate) -> dict | None:
        return next((a for a in assets if predicate(a["name"].lower())), None)

    if kind == "appimage":
        return first_match(lambda n: n.endswith(".appimage"))
    if kind == "deb":
        return first_match(lambda n: n.endswith(".deb"))
    if kind == "win-setup":
        return first_match(lambda n: "setup" in n and n.endswith(".exe"))
    if kind == "win-portable":
        return first_match(lambda n: "portable" in n and n.endswith(".exe"))
    return None


def _fetch_latest_release(timeout: float = 8.0) -> dict | None:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/releases/latest",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"{REPO.split('/')[-1]}-updater",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except Exception:  # noqa: BLE001
        return None


def _check_latest(current_version: str) -> UpdateInfo | None:
    data = _fetch_latest_release()
    if data is None:
        return None
    tag = data.get("tag_name") or ""
    if not tag:
        return None
    latest = tag.lstrip("vV")
    if not version_newer(latest, current_version):
        return None
    assets = data.get("assets") or []
    asset = _preferred_asset(bundle_kind(), assets)
    return UpdateInfo(
        latest_version=latest,
        release_url=(
            data.get("html_url")
            or f"https://github.com/{REPO}/releases/tag/{tag}"
        ),
        asset_name=asset["name"] if asset else None,
        asset_url=asset["browser_download_url"] if asset else None,
        asset_size=int(asset["size"]) if asset else 0,
    )


def _swap_in_appimage(new_path: Path) -> Path:
    current = os.environ.get("APPIMAGE")
    if not current:
        raise RuntimeError("APPIMAGE env var not set — not an AppImage install")
    target = Path(current).resolve()
    shutil.move(str(new_path), str(target))
    mode = os.stat(target).st_mode
    os.chmod(target, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def _relaunch(path: Path) -> None:
    subprocess.Popen(
        [str(path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )


def _download_with_progress(
    root: tk.Tk, url: str, dest: Path, title: str,
) -> bool:
    """Run a blocking download with a tkinter progress dialog. Returns True
    if the download completed, False on cancel/error."""
    win = tk.Toplevel(root)
    win.title(title)
    win.transient(root)
    win.geometry("420x120")
    win.resizable(False, False)

    label_var = tk.StringVar(value=f"Downloading {dest.name}…")
    label = tk.Label(win, textvariable=label_var, anchor="w")
    label.pack(fill="x", padx=14, pady=(14, 4))

    pct_var = tk.IntVar(value=0)
    bar = ttk.Progressbar(win, orient="horizontal", mode="determinate",
                          maximum=100, variable=pct_var)
    bar.pack(fill="x", padx=14)

    cancelled = {"flag": False}

    def _on_cancel() -> None:
        cancelled["flag"] = True

    cancel = tk.Button(win, text="Cancel", command=_on_cancel)
    cancel.pack(pady=8)

    result = {"ok": False, "error": None}

    def _worker() -> None:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            req = urllib.request.Request(
                url,
                headers={"User-Agent": f"{REPO.split('/')[-1]}-updater"},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                written = 0
                with open(dest, "wb") as f:
                    while True:
                        if cancelled["flag"]:
                            raise RuntimeError("cancelled")
                        chunk = resp.read(262144)
                        if not chunk:
                            break
                        f.write(chunk)
                        written += len(chunk)
                        if total > 0:
                            pct = int(written * 100 / total)
                            root.after(0, lambda p=pct: pct_var.set(p))
            result["ok"] = True
        except Exception as exc:  # noqa: BLE001
            try:
                dest.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
            result["error"] = str(exc)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    while t.is_alive():
        root.update()
        root.after(80)
        try:
            win.update()
        except tk.TclError:
            break
    try:
        win.destroy()
    except tk.TclError:
        pass
    if result["error"] and not cancelled["flag"]:
        messagebox.showwarning("Update failed",
                               f"The download didn't complete:\n{result['error']}")
    return result["ok"]


def _prompt_and_install(root: tk.Tk, info: UpdateInfo, current_version: str) -> None:
    kind = bundle_kind()
    can_auto_install = kind == "appimage" and bool(info.asset_url)

    size_note = ""
    if can_auto_install and info.asset_size:
        size_note = f"\n\n{info.asset_name} ({info.asset_size // (1024 * 1024)} MB)"

    body = (
        f"{APP_DISPLAY_NAME} v{info.latest_version} is available.\n"
        f"You're running v{current_version}.{size_note}"
    )
    if can_auto_install:
        choice = messagebox.askyesnocancel(
            f"{APP_DISPLAY_NAME} — update available",
            f"{body}\n\nYes: download and install now (the app will restart).\n"
            "No: view the release page.",
        )
        if choice is None:
            return
        if not choice:
            webbrowser.open(info.release_url)
            return
        cache = Path(os.path.expanduser(f"~/.cache/{CACHE_SUBDIR}"))
        dest = cache / (info.asset_name or "cove-video-downloader.AppImage")
        ok = _download_with_progress(
            root, info.asset_url, dest,
            title=f"Updating {APP_DISPLAY_NAME}",
        )
        if not ok:
            return
        try:
            new_path = _swap_in_appimage(dest)
        except Exception as exc:  # noqa: BLE001
            messagebox.showwarning("Update failed",
                                   f"Couldn't swap in the new AppImage:\n{exc}")
            return
        _relaunch(new_path)
        root.destroy()
    else:
        if messagebox.askyesno(
            f"{APP_DISPLAY_NAME} — update available",
            f"{body}\n\nOpen the release page to download the latest installer?",
        ):
            webbrowser.open(info.release_url)


def start_background_check(root: tk.Tk, current_version: str) -> None:
    """Kick off a background poll. When a newer release is found, the
    prompt is shown on the tk main loop via `root.after`."""
    shown = {"flag": False}

    def _worker() -> None:
        info = _check_latest(current_version)
        if info is None:
            return

        def _show() -> None:
            if shown["flag"]:
                return
            shown["flag"] = True
            _prompt_and_install(root, info, current_version)

        root.after(0, _show)

    threading.Thread(target=_worker, daemon=True).start()
