#!/usr/bin/env node
/**
 * Fetch the Python runtime + ffmpeg bundled with each platform's build.
 *
 * Usage:  node scripts/fetch-runtimes.js [linux|win]
 *
 * Runtime layout that main.js + python/backend.py expect at runtime:
 *
 *   {resourcesPath}/runtime/
 *     ├ python.exe          (Windows only - embed runtime)
 *     ├ ffmpeg(.exe)        (both platforms)
 *     ├ ffprobe(.exe)       (both platforms - yt-dlp needs it for audio)
 *     ├ HandBrakeCLI.exe    (Windows only - Linux uses system install)
 *     └ ...
 *
 * Integrity (audit L6): every file download() fetches is SHA-256 hashed
 * while it streams and checked against scripts/runtime-checksums.json.
 * That manifest maps a stable key naming WHAT was fetched to the expected
 * lowercase hex digest. Keys embed the version for pinned downloads
 * (python-3.11.9-embed-amd64.zip, handbrakecli-1.11.1-win-x86_64.zip);
 * downloads that deliberately track a moving `latest` release use a fixed
 * versionless name (ffmpeg-yt-dlp-win64-gpl-shared-latest.zip,
 * ffmpeg-johnvansickle-amd64-static.tar.xz) and are expected to stay
 * unpinned. A build with no entry for a key logs an UNPINNED line with
 * the full hash; copy it into the manifest after a known-good build to
 * pin future ones. A pinned mismatch deletes the file and fails the
 * build loudly rather than shipping whatever arrived.
 *
 * The Electron binary itself doubles as yt-dlp's Node runtime (via
 * ELECTRON_RUN_AS_NODE=1) for YouTube EJS sig/n-challenge solving, so we
 * don't bundle a separate JS runtime.
 */
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const https = require('node:https');
const { execSync } = require('node:child_process');

const ROOT = path.resolve(__dirname, '..');
const CHECKSUM_FILE = path.join(__dirname, 'runtime-checksums.json');

// target/OUT are only meaningful when run as a CLI; requiring this file
// from tests must not exit or touch the filesystem.
let target;
let OUT;

/**
 * Pure checksum decision, exported so the gate is testable without
 * network. Comparison ignores hex case: the manifest is documented as
 * lowercase but a wrong-case entry must verify rather than spuriously
 * fail (while still failing on genuinely different hashes).
 *
 * @param {string} key stable manifest key for what was fetched
 * @param {string} actualHash computed SHA-256 hex digest of the download
 * @param {Record<string, string>} manifest key -> expected hash
 * @returns {{pinned: boolean, verified: boolean}} verified is true only
 *   for an existing, matching entry. pinned=false means no entry: report
 *   as UNPINNED, never as verified.
 * @throws {Error} on pinned mismatch, naming key, expected and actual hash.
 */
function verifyChecksum(key, actualHash, manifest) {
  const expected = manifest[key];
  if (!expected) return { pinned: false, verified: false };
  const exp = String(expected).toLowerCase();
  const act = String(actualHash).toLowerCase();
  if (exp !== act) {
    throw new Error(
      `checksum mismatch for ${key}: expected sha256=${exp}, got sha256=${act}. ` +
      'If this release is known good, update scripts/runtime-checksums.json.');
  }
  return { pinned: true, verified: true };
}

/** Read the manifest, tolerating absence so a fresh checkout still builds. */
function loadChecksums() {
  try {
    return JSON.parse(fs.readFileSync(CHECKSUM_FILE, 'utf8'));
  } catch {
    return {};
  }
}

/** Log the outcome of a finished download's integrity check. */
function reportChecksum(key, actualHash) {
  let verdict;
  try {
    verdict = verifyChecksum(key, actualHash, loadChecksums());
  } catch (err) {
    console.error(`[fetch-runtimes] CHECKSUM FAILED: ${err.message}`);
    throw err;
  }
  if (verdict.pinned) {
    console.log(`verified ${key} sha256=${actualHash.slice(0, 12)}`);
  } else {
    console.log(`UNPINNED ${key} sha256=${actualHash}  (add to scripts/runtime-checksums.json to pin)`);
  }
}

// The write stream is opened only after the response is known good, and any
// partial file is removed on failure. Creating it up-front left a zero-byte
// or half-written binary behind on every non-200, which the `existsSync`
// guards above then treated as a successful previous fetch.
//
// `key` is the manifest key for this download; the SHA-256 is computed by
// feeding the response stream through a hash (never buffering the archive)
// and checked once the file is fully written. A pinned mismatch deletes
// the downloaded file via cleanup() and rejects, failing the build.
function download(url, dest, key, redirects = 0) {
  return new Promise((resolve, reject) => {
    if (redirects > 5) return reject(new Error(`too many redirects for ${url}`));

    const cleanup = (err) => {
      fs.rm(dest, { force: true }, () => reject(err));
    };

    const req = https.get(url, {
      headers: { 'User-Agent': 'cove-video-downloader-builder' },
      timeout: 120000,
    }, (res) => {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        res.resume();
        return resolve(download(new URL(res.headers.location, url).href, dest, key, redirects + 1));
      }
      if (res.statusCode !== 200) {
        res.resume();
        return reject(new Error(`GET ${url} -> ${res.statusCode}`));
      }
      const hash = crypto.createHash('sha256');
      res.on('data', (chunk) => hash.update(chunk));
      const f = fs.createWriteStream(dest);
      f.on('error', cleanup);
      res.on('error', cleanup);
      res.pipe(f);
      f.on('finish', () => f.close((err) => {
        if (err) return cleanup(err);
        const digest = hash.digest('hex');
        try {
          reportChecksum(key, digest);
        } catch (mismatch) {
          return cleanup(mismatch);
        }
        resolve();
      }));
    });

    req.on('timeout', () => req.destroy(new Error(`timeout fetching ${url}`)));
    req.on('error', reject);
  });
}

function sh(cmd) { execSync(cmd, { stdio: 'inherit' }); }

// Cross-platform zip extraction. Windows has no `unzip`, so use PowerShell's
// Expand-Archive there; fall back to `unzip` on Linux/macOS.
function unzip(zip, dest) {
  if (process.platform === 'win32') {
    sh(`powershell -NoProfile -NonInteractive -Command "Expand-Archive -LiteralPath '${zip}' -DestinationPath '${dest}' -Force"`);
  } else {
    sh(`unzip -q -o "${zip}" -d "${dest}"`);
  }
}

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
  // Moving `latest` release: intentionally left unpinned.
  await download(url, tar, 'ffmpeg-johnvansickle-amd64-static.tar.xz');
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
  // Derived from PY_VER, not a literal: bumping PY_VER to 3.12 used to leave
  // this pointing at python311._pth, and the existsSync guard below turned the
  // resulting no-op `site` patch into a silent failure.
  const PY_TAG   = PY_VER.split('.').slice(0, 2).join('');   // "3.11.9" -> "311"
  const PTH_FILE = path.join(OUT, `python${PY_TAG}._pth`);

  if (!fs.existsSync(PY_EXE)) {
    console.log(`[fetch-runtimes] win: downloading Python ${PY_VER}`);
    await download(PY_URL, PY_ZIP, `python-${PY_VER}-embed-amd64.zip`);
    unzip(PY_ZIP, OUT);
    fs.unlinkSync(PY_ZIP);
    // Fail loudly rather than shipping a runtime that cannot import site.
    if (!fs.existsSync(PTH_FILE)) {
      const found = fs.readdirSync(OUT).filter(n => n.endsWith('._pth'));
      throw new Error(
        `expected ${path.basename(PTH_FILE)} in the Python embed zip, found: ` +
        `${found.join(', ') || '(none)'}. Update PY_VER handling in this script.`);
    }
    const c = fs.readFileSync(PTH_FILE, 'utf8');
    if (c.includes('#import site')) {
      fs.writeFileSync(PTH_FILE, c.replace('#import site', 'import site'));
    } else if (!/^import site$/m.test(c)) {
      throw new Error(`${path.basename(PTH_FILE)} has no 'import site' line to enable.`);
    }
  } else {
    console.log('[fetch-runtimes] win: Python already present');
  }

  // 2. ffmpeg + ffprobe - GPL SHARED build: thin .exe wrappers (~5 MB)
  // plus shared DLLs (~50 MB total). Static build is 193 MB per binary
  // which balloons the portable.
  const ffmpegExe  = path.join(OUT, 'ffmpeg.exe');
  const ffprobeExe = path.join(OUT, 'ffprobe.exe');
  if (!fs.existsSync(ffmpegExe) || !fs.existsSync(ffprobeExe)) {
    const url = 'https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl-shared.zip';
    const zip = path.join(OUT, 'ffmpeg.zip');
    console.log('[fetch-runtimes] win: downloading ffmpeg shared build');
    // Moving `latest` release: intentionally left unpinned.
    await download(url, zip, 'ffmpeg-yt-dlp-win64-gpl-shared-latest.zip');
    const extractDir = path.join(OUT, 'ff-extract');
    fs.mkdirSync(extractDir, { recursive: true });
    unzip(zip, extractDir);
    const top = fs.readdirSync(extractDir).find(
      (n) => fs.statSync(path.join(extractDir, n)).isDirectory()
    );
    const binDir = path.join(extractDir, top, 'bin');
    // Copy every file in bin/ - the thin .exes rely on sibling DLLs to run.
    for (const f of fs.readdirSync(binDir)) {
      fs.copyFileSync(path.join(binDir, f), path.join(OUT, f));
    }
    fs.rmSync(extractDir, { recursive: true, force: true });
    fs.unlinkSync(zip);
    console.log('[fetch-runtimes] win: ffmpeg + ffprobe ready (shared DLLs)');
  } else {
    console.log('[fetch-runtimes] win: ffmpeg/ffprobe already present');
  }

  // 3. HandBrakeCLI - bundled only on Windows. Linux users install via system
  // package manager (apt/pacman/dnf); backend.py surfaces a clear error if
  // compression is requested without it on PATH.
  const HB_VER = '1.11.1';
  const hbExe = path.join(OUT, 'HandBrakeCLI.exe');
  if (!fs.existsSync(hbExe)) {
    const hbUrl = `https://github.com/HandBrake/HandBrake/releases/download/${HB_VER}/HandBrakeCLI-${HB_VER}-win-x86_64.zip`;
    const hbZip = path.join(OUT, 'handbrake.zip');
    console.log(`[fetch-runtimes] win: downloading HandBrakeCLI ${HB_VER}`);
    await download(hbUrl, hbZip, `handbrakecli-${HB_VER}-win-x86_64.zip`);
    unzip(hbZip, OUT);
    fs.unlinkSync(hbZip);
    console.log('[fetch-runtimes] win: HandBrakeCLI ready');
  } else {
    console.log('[fetch-runtimes] win: HandBrakeCLI already present');
  }
}

// CLI entry point. Guarded so tests can require this module for
// verifyChecksum without argument parsing, exit() or side effects.
if (require.main === module) {
  target = process.argv[2];
  if (!target || !['linux', 'win'].includes(target)) {
    console.error('usage: fetch-runtimes.js <linux|win>');
    process.exit(1);
  }

  OUT = path.join(ROOT, 'runtimes', target);
  fs.mkdirSync(OUT, { recursive: true });

  (async () => {
    try {
      if (target === 'linux') await fetchLinux();
      else                    await fetchWin();
      console.log(`[fetch-runtimes] ${target}: runtime ready at ${OUT}`);
    } catch (err) {
      console.error(`[fetch-runtimes] ${target}: FAILED - ${err.message}`);
      process.exit(1);
    }
  })();
}

module.exports = { verifyChecksum };
