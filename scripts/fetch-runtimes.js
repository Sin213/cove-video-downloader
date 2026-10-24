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
 * ELECTRON_RUN_AS_NODE=1) for YouTube EJS sig/n-challenge solving. Electron
 * 44 supplies Node >= 22; the backend probes compatibility before advertising
 * it to yt-dlp. We don't bundle a separate JS runtime.
 */
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const https = require('node:https');
const { inflateRawSync } = require('node:zlib');
const { execSync } = require('node:child_process');

const ROOT = path.resolve(__dirname, '..');
const CHECKSUM_FILE = path.join(__dirname, 'runtime-checksums.json');

// target/OUT are only meaningful when run as a CLI; requiring this file
// from tests must not exit or touch the filesystem.
let target;
let OUT;

const CERTIFI_VER = '2026.10.23';
const CERTIFI_KEY = `certifi-${CERTIFI_VER}-py3-none-any.whl`;
const CERTIFI_URL = 'https://files.pythonhosted.org/packages/32/d1/a3652fe2c19134d0d15922177821d863cbd7e842d53a21bbe7e8e3a1b5ac/certifi-2026.10.23-py3-none-any.whl';
const CERTIFI_PINS = Object.freeze({
  version: CERTIFI_VER,
  wheel_sha256: 'e9b54de76e530bb6cf0ccd036787103acb58fd42ba930ea8bf948ddbaa2f61d1',
  cacert_sha256: '2ef52ad56b4803e264045c915e55c236cbfa045b84b81efefc218a19eb44bd2b',
  license_sha256: 'e93716da6b9c0d5a4a1df60fe695b370f0695603d21f6f83f053e42cfc10caf7',
  metadata_sha256: 'e463e38f7fe7da36a11e6d3fecc5dfa5b97be8c8022620400750d79ae7d842bc',
});
const CERTIFI_FILES = { 'cacert.pem': 'cacert_sha256', LICENSE: 'license_sha256', METADATA: 'metadata_sha256' };
const sha256 = data => crypto.createHash('sha256').update(data).digest('hex');

function crc32(data) {
  let crc = 0xffffffff;
  for (const byte of data) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

/** Bounded ZIP reader: validate every entry, return only three fixed outputs.
 * Archive paths never become filesystem destinations. No zip64 or encryption.
 */
function readCertifiWheel(zip) {
  const fail = message => { throw new Error(`unsafe certifi wheel: ${message}`); };
  const cap = 2 * 1024 * 1024;
  if (zip.length < 22 || zip.length > 4 * cap) fail('archive size');
  let end = -1;
  for (let i = zip.length - 22; i >= Math.max(0, zip.length - 65557); i--) {
    if (zip.readUInt32LE(i) === 0x06054b50 && i + 22 + zip.readUInt16LE(i + 20) === zip.length) {
      end = i; break;
    }
  }
  if (end < 0) fail('end directory');
  const count = zip.readUInt16LE(end + 10), size = zip.readUInt32LE(end + 12);
  const start = zip.readUInt32LE(end + 16);
  if (zip.readUInt16LE(end + 4) || zip.readUInt16LE(end + 6) || !count || count > 128 ||
      zip.readUInt16LE(end + 8) !== count || start + size !== end) fail('directory/zip64');
  const allowed = new Map([
    ['certifi/cacert.pem', 'cacert.pem'],
    [`certifi-${CERTIFI_VER}.dist-info/licenses/LICENSE`, 'LICENSE'],
    [`certifi-${CERTIFI_VER}.dist-info/METADATA`, 'METADATA'],
  ]);
  const output = {}, seen = new Set(), ranges = [];
  let offset = start, total = 0;
  const extras = data => {
    for (let i = 0; i < data.length;) {
      if (i + 4 > data.length) fail('extra field');
      const id = data.readUInt16LE(i), length = data.readUInt16LE(i + 2);
      if (id === 1 || i + 4 + length > data.length) fail('zip64/extra field');
      i += 4 + length;
    }
  };
  for (let n = 0; n < count; n++) {
    if (offset + 46 > end || zip.readUInt32LE(offset) !== 0x02014b50) fail('central header');
    const flags = zip.readUInt16LE(offset + 8), method = zip.readUInt16LE(offset + 10);
    const crc = zip.readUInt32LE(offset + 16), packed = zip.readUInt32LE(offset + 20);
    const unpacked = zip.readUInt32LE(offset + 24), nameLen = zip.readUInt16LE(offset + 28);
    const extraLen = zip.readUInt16LE(offset + 30), commentLen = zip.readUInt16LE(offset + 32);
    const local = zip.readUInt32LE(offset + 42), next = offset + 46 + nameLen + extraLen + commentLen;
    if (next > end || zip.readUInt16LE(offset + 6) >= 45 || zip.readUInt16LE(offset + 34) ||
        flags & ~0x808 || ![0, 8].includes(method)) fail('unsupported/encrypted entry');
    const nameBytes = zip.subarray(offset + 46, offset + 46 + nameLen), name = nameBytes.toString('utf8');
    if (!/^[\x20-\x7e]+$/.test(name) || name.startsWith('/') || name.includes('\\') ||
        name.includes(':') || name.split('/').some(p => p === '..' || p === '.') || seen.has(name)) fail('unsafe/duplicate name');
    seen.add(name);
    if (((zip.readUInt32LE(offset + 38) >>> 16) & 0o170000) === 0o120000) fail('symlink');
    extras(zip.subarray(offset + 46 + nameLen, offset + 46 + nameLen + extraLen));
    total += unpacked;
    if (unpacked > cap || packed > cap || total > 4 * cap || local + 30 > start ||
        zip.readUInt32LE(local) !== 0x04034b50) fail('size/local header');
    const localName = zip.readUInt16LE(local + 26), localExtra = zip.readUInt16LE(local + 28);
    const dataStart = local + 30 + localName + localExtra, dataEnd = dataStart + packed;
    if (dataEnd > start || !zip.subarray(local + 30, local + 30 + localName).equals(nameBytes) ||
        zip.readUInt16LE(local + 8) !== method || zip.readUInt16LE(local + 6) !== flags ||
        zip.readUInt16LE(local + 4) >= 45) fail('local mismatch');
    extras(zip.subarray(local + 30 + localName, dataStart));
    if (!(flags & 8) && (zip.readUInt32LE(local + 14) !== crc ||
        zip.readUInt32LE(local + 18) !== packed || zip.readUInt32LE(local + 22) !== unpacked)) fail('local sizes/CRC');
    if (ranges.some(([a, b]) => local < b && dataEnd > a)) fail('overlapping entry');
    ranges.push([local, dataEnd]);
    const compressed = zip.subarray(dataStart, dataEnd);
    const data = method === 0 ? compressed : inflateRawSync(compressed, { maxOutputLength: cap });
    if (data.length !== unpacked || crc32(data) !== crc) fail('size/CRC mismatch');
    if (allowed.has(name)) output[allowed.get(name)] = data;
    offset = next;
  }
  if (offset !== end || Object.keys(output).length !== allowed.size) fail('missing required members');
  return output;
}

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

function requireCertifiPin(manifest, pins = CERTIFI_PINS) {
  if (!verifyChecksum(CERTIFI_KEY, pins.wheel_sha256, manifest).verified) {
    throw new Error(`missing required checksum pin for ${CERTIFI_KEY}`);
  }
}

function certifiCurrent(dir, pins = CERTIFI_PINS) {
  try {
    if (!fs.lstatSync(dir).isDirectory() || fs.lstatSync(dir).isSymbolicLink()) return false;
    const names = [...Object.keys(CERTIFI_FILES), 'PROVENANCE.json'].sort();
    if (JSON.stringify(fs.readdirSync(dir).sort()) !== JSON.stringify(names)) return false;
    if (!names.every(n => fs.lstatSync(path.join(dir, n)).isFile())) return false;
    const provenance = JSON.parse(fs.readFileSync(path.join(dir, 'PROVENANCE.json'), 'utf8'));
    return JSON.stringify(Object.keys(provenance).sort()) === JSON.stringify(Object.keys(pins).sort()) &&
      Object.keys(pins).every(k => provenance[k] === pins[k]) &&
      Object.entries(CERTIFI_FILES).every(([name, pin]) => sha256(fs.readFileSync(path.join(dir, name))) === pins[pin]);
  } catch { return false; }
}

function installCertifiWheel(wheel, dir, manifest, pins = CERTIFI_PINS) {
  try {
    requireCertifiPin(manifest, pins);
    const data = fs.readFileSync(wheel);
    verifyChecksum(CERTIFI_KEY, sha256(data), manifest);
    const files = readCertifiWheel(data);
    for (const [name, pin] of Object.entries(CERTIFI_FILES)) {
      if (sha256(files[name]) !== pins[pin]) {
        throw new Error(`certifi ${name === 'cacert.pem' ? 'CA' : name} checksum mismatch`);
      }
    }
    fs.rmSync(dir, { recursive: true, force: true });
    fs.mkdirSync(dir, { recursive: true });
    for (const [name, content] of Object.entries(files)) fs.writeFileSync(path.join(dir, name), content);
    fs.writeFileSync(path.join(dir, 'PROVENANCE.json'), JSON.stringify(pins, null, 2) + '\n');
  } catch (err) {
    fs.rmSync(wheel, { force: true });
    throw err;
  }
}

async function fetchCertifi(out, manifest = loadChecksums(), fetch = download, pins = CERTIFI_PINS) {
  const wheel = path.join(out, CERTIFI_KEY), dir = path.join(out, 'certifi');
  try {
    requireCertifiPin(manifest, pins);
    if (certifiCurrent(dir, pins)) return;
    fs.rmSync(dir, { recursive: true, force: true });
    await fetch(CERTIFI_URL, wheel, CERTIFI_KEY);
    installCertifiWheel(wheel, dir, manifest, pins);
  } finally {
    fs.rmSync(wheel, { force: true });
  }
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

  await fetchCertifi(OUT);

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

module.exports = { verifyChecksum, readCertifiWheel, crc32, requireCertifiPin,
  certifiCurrent, installCertifiWheel, fetchCertifi, CERTIFI_PINS, CERTIFI_KEY };
