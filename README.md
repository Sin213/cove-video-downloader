# Cove Video Downloader

<p align="center">
  <img src="cove_icon.png" width="120" alt="Cove Video Downloader Icon" />
</p>

<p align="center">
  A dead-simple, dark-themed GUI video downloader for Windows.<br/>
  Paste a link. Hit Download. Done.
</p>

---

## Download

Grab the latest **CoveVideoDownloader.exe** from the [Releases](../../releases/latest) page. No install required — just run it.

---

## Features

### Highest quality, automatically
No format pickers, no resolution menus. The app always grabs the best video and audio available — 4K, 1440p, 1080p, whatever the site offers — and merges them into a single MP4.

### One-click downloading
Paste one or more video links and click **⬇ Download**. That's it.

### Smart paste — one link per line
Add links two ways, both behave the same:
- Click the **Paste** button
- Use **Ctrl+V**

Each new link lands on its own line automatically. No accidentally dumping a URL mid-line.

### Bulk downloads
Paste as many links as you want — one per line — and hit Download once. The app processes them all in order with live progress in the log.

### Optional H.265 compression
The **Compress** checkbox (on by default) runs the downloaded video through HandBrake after downloading using H.265 (HEVC) at a web-balanced quality setting with AAC 192k audio — meaningfully smaller files with no visible quality loss. If the compressed file ends up *larger* than the original, it's automatically discarded and the original is kept.

### Auto-detected browser cookies
The app automatically detects cookies from your installed browsers (Firefox, Chrome, Brave, Chromium, Edge) so it can access age-restricted or login-gated content without you doing anything. If a video still can't be accessed, a message will tell you to make sure you're logged in on a supported browser.

### Custom output folder
Choose exactly where your videos save with the **Browse** button. Defaults to your Downloads folder.

### Open Folder button
After a download completes, click **📂 Open Folder** to jump straight to where your files were saved.

### Works on 1,000+ sites
Powered by `yt-dlp` — YouTube, Reddit, X (Twitter), Instagram, TikTok, Facebook, Twitch, Vimeo, Dailymotion, Bilibili, Tumblr, and hundreds more. Just paste the link.

### Auto-updating yt-dlp
Every time the app launches it silently checks for a newer version of `yt-dlp` and downloads it in the background if one is available. You're always on the latest extractor without doing anything.

### Live log output
A scrolling log shows real-time output from both the download and compression stages so you always know exactly what's happening.

---

## Usage

1. **Run** `CoveVideoDownloader.exe`
2. **Copy** a video link from any site
3. **Ctrl+V** or click **Paste**
4. Repeat for as many links as you want
5. *(Optional)* Click **Browse** to change the save folder
6. Click **⬇ Download**
7. Click **📂 Open Folder** to see your files

---

## Notes

- All tools (yt-dlp, ffmpeg, HandBrakeCLI) are **bundled inside the EXE** — nothing to install.
- yt-dlp is kept up to date automatically on every launch.
- Browser cookies are detected automatically — no browser selection needed.
- DRM-protected content (Netflix, Disney+, etc.) cannot be downloaded — platform-level restriction.
- Videos save to your **Downloads** folder by default, or wherever you set with Browse.

---

## Built with

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — download engine
- [FFmpeg](https://ffmpeg.org) — audio/video merging
- [HandBrake CLI](https://handbrake.fr) — optional H.265 compression
- [Tkinter](https://docs.python.org/3/library/tkinter.html) — GUI (Python standard library)
- [PyInstaller](https://pyinstaller.org) — Windows EXE packaging
