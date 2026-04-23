#!/usr/bin/env node
/**
 * Fetch the Python runtime + ffmpeg bundled with each platform's build.
 *
 * Usage:  node scripts/fetch-runtimes.js [linux|win]
 *
 * Runtime layout that main.js + python/backend.py expect at runtime:
 *
 *   {resourcesPath}/runtime/
 *     ├ python.exe          (Windows only — embed runtime)
 *     ├ ffmpeg(.exe)        (both platforms)
 *     ├ ffprobe(.exe)       (both platforms — yt-dlp needs it for audio)
 *     ├ HandBrakeCLI.exe    (Windows only — Linux uses system install)
 *     └ ...
 *
 * The Electron binary itself doubles as yt-dlp's Node runtime (via
 * ELECTRON_RUN_AS_NODE=1) for YouTube EJS sig/n-challenge solving, so we
 * don't bundle a separate JS runtime.
 */
const fs = require('node:fs');
const path = require('node:path');
const https = require('node:https');
const { execSync } = require('node:child_process');

const ROOT = path.resolve(__dirname, '..');
const target = process.argv[2];

if (!target || !['linux', 'win'].includes(target)) {
  console.error('usage: fetch-runtimes.js <linux|win>');
  process.exit(1);
}

const OUT = path.join(ROOT, 'runtimes', target);
fs.mkdirSync(OUT, { recursive: true });

function download(url, dest) {
  return new Promise((resolve, reject) => {
    const f = fs.createWriteStream(dest);
    const go = (u) => {
      https.get(u, { headers: { 'User-Agent': 'cove-video-downloader-builder' } }, (res) => {
        if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
          res.resume();
          return go(res.headers.location);
        }
        if (res.statusCode !== 200) {
          reject(new Error(`GET ${u} → ${res.statusCode}`));
          return;
        }
        res.pipe(f);
        f.on('finish', () => f.close(resolve));
      }).on('error', reject);
    };
    go(url);
  });
}

function sh(cmd) { execSync(cmd, { stdio: 'inherit' }); }

// ─── Linux ────────────────────────────────────────────────────────────────
async function fetchLinux() {
  const ffmpegBin  = path.join(OUT, 'ffmpeg');
  const ffprobeBin = path.join(OUT, 'ffprobe');
  if (fs.existsSync(ffmpegBin) && fs.existsSync(ffprobeBin)) {
    console.log('[fetch-runtimes] linux: ffmpeg/ffprobe already present');
    return;
  }

  const url = 'https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz';
  const tar = path.join(OUT, 'ffmpeg.tar.xz');
  console.log(`[fetch-runtimes] linux: downloading ffmpeg static build`);
  await download(url, tar);
  const extractDir = path.join(OUT, 'ff-extract');
  fs.mkdirSync(extractDir, { recursive: true });
  sh(`tar -xf "${tar}" -C "${extractDir}" --strip-components=1`);
  fs.copyFileSync(path.join(extractDir, 'ffmpeg'),  ffmpegBin);
  fs.copyFileSync(path.join(extractDir, 'ffprobe'), ffprobeBin);
  fs.chmodSync(ffmpegBin,  0o755);
  fs.chmodSync(ffprobeBin, 0o755);
  fs.rmSync(extractDir, { recursive: true, force: true });
  fs.unlinkSync(tar);
  console.log('[fetch-runtimes] linux: ffmpeg + ffprobe ready');
}

// ─── Windows ──────────────────────────────────────────────────────────────
async function fetchWin() {
  // 1. Python embeddable runtime
  const PY_VER  = '3.11.9';
  const PY_URL  = `https://www.python.org/ftp/python/${PY_VER}/python-${PY_VER}-embed-amd64.zip`;
  const PY_ZIP  = path.join(OUT, 'python-embed.zip');
  const PY_EXE  = path.join(OUT, 'python.exe');
  const PTH_FILE = path.join(OUT, 'python311._pth');

  if (!fs.existsSync(PY_EXE)) {
    console.log(`[fetch-runtimes] win: downloading Python ${PY_VER}`);
    await download(PY_URL, PY_ZIP);
    sh(`unzip -o "${PY_ZIP}" -d "${OUT}"`);
    fs.unlinkSync(PY_ZIP);
    if (fs.existsSync(PTH_FILE)) {
      let c = fs.readFileSync(PTH_FILE, 'utf8');
      if (c.includes('#import site')) {
        fs.writeFileSync(PTH_FILE, c.replace('#import site', 'import site'));
      }
    }
  } else {
    console.log('[fetch-runtimes] win: Python already present');
  }

  // 2. ffmpeg + ffprobe — GPL SHARED build: thin .exe wrappers (~5 MB)
  // plus shared DLLs (~50 MB total). Static build is 193 MB per binary
  // which balloons the portable.
  const ffmpegExe  = path.join(OUT, 'ffmpeg.exe');
  const ffprobeExe = path.join(OUT, 'ffprobe.exe');
  if (!fs.existsSync(ffmpegExe) || !fs.existsSync(ffprobeExe)) {
    const url = 'https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl-shared.zip';
    const zip = path.join(OUT, 'ffmpeg.zip');
    console.log('[fetch-runtimes] win: downloading ffmpeg shared build');
    await download(url, zip);
    const extractDir = path.join(OUT, 'ff-extract');
    fs.mkdirSync(extractDir, { recursive: true });
    sh(`unzip -q -o "${zip}" -d "${extractDir}"`);
    const top = fs.readdirSync(extractDir).find(
      (n) => fs.statSync(path.join(extractDir, n)).isDirectory()
    );
    const binDir = path.join(extractDir, top, 'bin');
    // Copy every file in bin/ — the thin .exes rely on sibling DLLs to run.
    for (const f of fs.readdirSync(binDir)) {
      fs.copyFileSync(path.join(binDir, f), path.join(OUT, f));
    }
    fs.rmSync(extractDir, { recursive: true, force: true });
    fs.unlinkSync(zip);
    console.log('[fetch-runtimes] win: ffmpeg + ffprobe ready (shared DLLs)');
  } else {
    console.log('[fetch-runtimes] win: ffmpeg/ffprobe already present');
  }

  // 3. HandBrakeCLI — bundled only on Windows. Linux users install via system
  // package manager (apt/pacman/dnf); backend.py surfaces a clear error if
  // compression is requested without it on PATH.
  const HB_VER = '1.11.1';
  const hbExe = path.join(OUT, 'HandBrakeCLI.exe');
  if (!fs.existsSync(hbExe)) {
    const hbUrl = `https://github.com/HandBrake/HandBrake/releases/download/${HB_VER}/HandBrakeCLI-${HB_VER}-win-x86_64.zip`;
    const hbZip = path.join(OUT, 'handbrake.zip');
    console.log(`[fetch-runtimes] win: downloading HandBrakeCLI ${HB_VER}`);
    await download(hbUrl, hbZip);
    sh(`unzip -q -o "${hbZip}" -d "${OUT}"`);
    fs.unlinkSync(hbZip);
    console.log('[fetch-runtimes] win: HandBrakeCLI ready');
  } else {
    console.log('[fetch-runtimes] win: HandBrakeCLI already present');
  }
}

(async () => {
  try {
    if (target === 'linux') await fetchLinux();
    else                    await fetchWin();
    console.log(`[fetch-runtimes] ${target}: runtime ready at ${OUT}`);
  } catch (err) {
    console.error(`[fetch-runtimes] ${target}: FAILED — ${err.message}`);
    process.exit(1);
  }
})();
