# Cove Video Downloader

<p align="center">
  <img src="cove_icon.png" width="120" alt="Cove Video Downloader Icon" />
</p>

<p align="center">
  A dead-simple, dark-themed GUI video downloader.<br/>
  Paste a link. Hit Download. Done.
</p>

One codebase, one repository, native builds for both platforms: a Windows
installer + portable exe, and a Linux AppImage + .deb. Every `v*` tag cuts
all four artifacts via GitHub Actions.

![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)
![Platforms](https://img.shields.io/badge/platforms-Windows%20%7C%20Linux-informational?style=flat-square)

---

## Install a prebuilt release

Head to the [Releases page](https://github.com/Sin213/cove-video-downloader/releases):

| OS      | Artifact                                          | Notes                                            |
| ------- | ------------------------------------------------- | ------------------------------------------------ |
| Windows | `cove-video-downloader-<version>-Setup.exe`       | Inno Setup installer (Start Menu + Desktop)      |
| Windows | `cove-video-downloader-<version>-Portable.exe`    | Single-file portable                             |
| Linux   | `Cove-Video-Downloader-<version>-x86_64.AppImage` | `chmod +x` and run                               |
| Linux   | `cove-video-downloader_<version>_amd64.deb`       | `sudo apt install ./cove-video-downloader_*.deb` |

`ffmpeg` is bundled inside every artifact. `yt-dlp` is fetched on first
launch and auto-updated thereafter. `HandBrakeCLI` is bundled on Windows;
on Linux it's a Recommends-only dependency.

> **Windows SmartScreen** may warn on first launch because the exe isn't
> signed. Click **More info → Run anyway**.

---

## Features

### Highest quality, automatically
No format pickers, no resolution menus. The app always grabs the best video
and audio available — 4K, 1440p, 1080p, whatever the site offers — and
merges them into a single MP4.

### One-click downloading
Paste one or more video links and click **⬇ Download**. That's it.

### Smart paste — one link per line
Add links via the **Paste** button or **Ctrl+V** — each new link lands on
its own line automatically.

### Bulk downloads
Paste as many links as you want — one per line — and hit Download once. The
app processes them in order with live progress in the log.

### Optional H.265 compression
The **Compress** checkbox runs the downloaded video through HandBrakeCLI
using H.265 (HEVC) at a web-balanced quality setting with AAC 192k audio.
If the compressed file ends up *larger* than the original, it's automatically
discarded and the original is kept.

### Auto-detected browser cookies
The app automatically detects cookies from installed browsers (Firefox,
Chrome, Brave, Chromium, Edge) for age-restricted or login-gated content.

### Custom output folder
Choose exactly where your videos save with the **Browse** button. Defaults
to your Downloads folder.

### Works on 1,000+ sites
Powered by `yt-dlp` — YouTube, Reddit, X (Twitter), Instagram, TikTok,
Facebook, Twitch, Vimeo, Dailymotion, Bilibili, Tumblr, and hundreds more.

### Auto-updating yt-dlp
Every launch silently checks GitHub for a newer `yt-dlp` and downloads it in
the background if one is available.

---

## Usage

1. **Run** the app.
2. **Copy** a video link, paste it in with **Ctrl+V** or the **Paste** button.
3. *(Optional)* Click **Browse** to change the save folder.
4. Click **⬇ Download**.
5. Click **📂 Open Folder** to see your files.

---

## Running from source

Python 3.10+, tkinter (stdlib — included on Windows; `python3-tk` on
Debian/Ubuntu; bundled with Python on Arch).

```bash
# Linux
sudo apt install python3-tk ffmpeg handbrake-cli   # or pacman / dnf equivalent
python3 cove_video_downloader.py
```

```powershell
# Windows
py cove_video_downloader.py
```

yt-dlp is downloaded automatically into a per-user directory
(`~/.cove-video-downloader` on Linux, `%APPDATA%\CoveVideoDownloader` on
Windows).

---

## Building release artifacts

PyInstaller can't cross-compile, so each platform has its own script.

### Linux — AppImage + .deb

```bash
bash scripts/build-release.sh
# Output in release/:
#   Cove-Video-Downloader-1.0.0-x86_64.AppImage
#   cove-video-downloader_1.0.0_amd64.deb
```

Override the version with `VERSION=1.2.0 bash scripts/build-release.sh`.

### Windows — Setup.exe + Portable.exe

Requires [Inno Setup 6](https://jrsoftware.org/isdl.php) and Chocolatey
(HandBrakeCLI comes from `choco install handbrake-cli`). Both are
pre-installed on GitHub Actions' `windows-latest`.

```powershell
.\build.ps1 -Version 1.0.0
# Output in release\:
#   cove-video-downloader-1.0.0-Setup.exe
#   cove-video-downloader-1.0.0-Portable.exe
```

### Automated release via GitHub Actions

Push a tag matching `v*` (e.g. `v1.0.0`) and `.github/workflows/release.yml`
runs the Linux + Windows jobs in parallel and attaches all four artifacts to
the GitHub Release created for the tag.

---

## Built with

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — download engine (auto-updated)
- [FFmpeg](https://ffmpeg.org) — audio/video merging (bundled)
- [HandBrakeCLI](https://handbrake.fr) — optional H.265 compression
- [Tkinter](https://docs.python.org/3/library/tkinter.html) — GUI
- [PyInstaller](https://pyinstaller.org) — packaging
- [Inno Setup](https://jrsoftware.org/isinfo.php) — the Windows installer

---

## Notes

- DRM-protected content (Netflix, Disney+, etc.) cannot be downloaded —
  platform-level restriction.
- Videos save to your **Downloads** folder by default.
