# Cove Video Downloader

<p align="center">
  <img src="cove_icon.png" width="120" alt="Cove Video Downloader Icon" />
</p>

<p align="center">
  A dead-simple, dark-themed GUI video downloader for Linux.<br/>
  Paste a link. Hit Download. Done.
</p>

---

## Features

### One-click downloading
No settings menus, no format pickers, no rabbit holes. Paste one or more video links and click **Download**. The app automatically grabs the highest quality MP4 available.

### Smart paste — one link per line, automatically
You can add links two ways and both behave the same:
- Click the **Paste** button
- Use **Ctrl+V** on your keyboard

Either way, each new link is placed on its own line. No more accidentally dumping a URL in the middle of an existing one when you Ctrl+V out of habit. Every paste is clean and stacked.

### Bulk downloads
Paste as many links as you want — one per line — and hit Download once. The app processes them all in order and shows live progress in the log for each one.

### Optional compression
Check the **Compress** checkbox (on by default) to run the downloaded video through HandBrake after downloading. It uses H.265 (HEVC) encoding at a web-balanced quality setting with AAC 192k audio — meaningfully smaller files with no visible quality loss in most cases. If the compressed version somehow ends up *larger* than the original, it's automatically discarded and the original is kept.

### NSFW / 18+ content
The **Unlock NSFW** dropdown lets you pass your browser cookies to `yt-dlp` so it can access age-restricted or login-gated content — without you ever entering credentials into the app. Select the browser you're already logged into (Firefox, Chrome, Brave, Chromium, or Edge) and it handles the rest.

### Works on 1,000+ sites
Powered by `yt-dlp`, the app works on virtually any site that hosts public video — Reddit, X (Twitter), Instagram, YouTube, TikTok, Facebook, Twitch, Vimeo, Dailymotion, Bilibili, Tumblr, and hundreds more. Just paste the link; the app figures out the site automatically.

### Live log output
A scrolling log at the bottom of the window shows real-time output from both the download and compression stages so you always know exactly what's happening.

---

## Requirements

**Install these before running the app:**

```bash
# yt-dlp (required)
sudo pacman -S yt-dlp

# Tkinter GUI (required)
sudo pacman -S tk

# HandBrakeCLI (required only if you use the Compress option)
sudo pacman -S handbrake-cli
```

> These instructions are for **Arch-based distros** (EndeavourOS, Manjaro, etc.).  
> For Debian/Ubuntu: replace `sudo pacman -S` with `sudo apt install`.  
> For Fedora: use `sudo dnf install`.

Python 3 is required and comes pre-installed on most Linux distros.

---

## Setup

1. Download `cove_video_downloader.py` and `cove_icon.png`
2. Place both files in the **same folder**
3. Run the app:

```bash
python cove_video_downloader.py
```

---

## Optional: Desktop launcher

To add Cove to your app menu on Linux, create a `.desktop` file:

```bash
nano ~/.local/share/applications/cove-video-downloader.desktop
```

Paste the following (update the paths to match where you saved the files):

```ini
[Desktop Entry]
Name=Cove Video Downloader
Comment=Download videos from any site
Exec=python /home/YOUR_USERNAME/Apps/cove_video_downloader.py
Icon=/home/YOUR_USERNAME/Apps/cove_icon.png
Terminal=false
Type=Application
Categories=AudioVideo;Network;
```

Then reload your app menu or log out and back in.

---

## Usage

1. **Copy** a video link from any site
2. **Ctrl+V** or click **Paste** — the link is added on a new line automatically
3. Repeat for as many links as you want
4. Click **⬇ Download**
5. Videos save as MP4 in the **same folder you launched the script from**

---

## Notes

- Videos save to whichever directory you ran the script from. To control where files go, `cd` to your desired folder before launching.
- DRM-protected content (Netflix, Disney+, etc.) cannot be downloaded — this is a platform-level restriction, not a limitation of the app.
- The NSFW/cookie feature only works for sites where you're already logged in on the selected browser.

---

## Built with

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — the engine behind all downloads
- [HandBrake CLI](https://handbrake.fr) — optional compression
- [Tkinter](https://docs.python.org/3/library/tkinter.html) — GUI framework (Python standard library)
