'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const { deflateRawSync } = require('node:zlib');
const { execFileSync } = require('node:child_process');
const runtime = require('../../scripts/fetch-runtimes');
const { readCertifiWheel, crc32, installCertifiWheel, certifiCurrent, fetchCertifi,
  CERTIFI_KEY, CERTIFI_PINS } = runtime;
const scratch = path.resolve(process.env.COVE_TEST_SCRATCH || os.tmpdir());
const digest = data => crypto.createHash('sha256').update(data).digest('hex');
const base = [
  { name: 'certifi/cacert.pem', data: 'TEST ONLY synthetic CA bytes' },
  { name: 'certifi-2026.10.23.dist-info/licenses/LICENSE', data: 'TEST MPL-2.0' },
  { name: 'certifi-2026.10.23.dist-info/METADATA', data: 'Version: 2026.10.23' },
];

// Synthetic ZIPs only: no downloads and no third-party ZIP dependency.
function zip(entries = base) {
  const locals = [], centrals = [];
  let offset = 0;
  for (const entry of entries) {
    const name = Buffer.from(entry.name), data = Buffer.from(entry.data || 'test');
    const method = entry.method ?? 8, compressed = method === 8 ? deflateRawSync(data) : data;
    const crc = entry.crc ?? crc32(data), size = entry.size ?? data.length;
    const extra = entry.extra || Buffer.alloc(0), flags = entry.flags || 0;
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50); local.writeUInt16LE(20, 4);
    local.writeUInt16LE(flags, 6); local.writeUInt16LE(method, 8);
    local.writeUInt32LE(crc, 14); local.writeUInt32LE(compressed.length, 18);
    local.writeUInt32LE(size, 22); local.writeUInt16LE(name.length, 26); local.writeUInt16LE(extra.length, 28);
    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50); central.writeUInt16LE(0x314, 4); central.writeUInt16LE(20, 6);
    central.writeUInt16LE(flags, 8); central.writeUInt16LE(method, 10);
    central.writeUInt32LE(crc, 16); central.writeUInt32LE(compressed.length, 20);
    central.writeUInt32LE(size, 24); central.writeUInt16LE(name.length, 28);
    central.writeUInt16LE(extra.length, 30); central.writeUInt32LE((entry.mode || 0) * 65536, 38);
    central.writeUInt32LE(offset, 42);
    locals.push(local, name, extra, compressed); centrals.push(central, name, extra);
    offset += local.length + name.length + extra.length + compressed.length;
  }
  const directory = Buffer.concat(centrals), end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50); end.writeUInt16LE(entries.length, 8); end.writeUInt16LE(entries.length, 10);
  end.writeUInt32LE(directory.length, 12); end.writeUInt32LE(offset, 16);
  return Buffer.concat([...locals, directory, end]);
}

function fixture(t) {
  fs.mkdirSync(scratch, { recursive: true });
  const out = fs.mkdtempSync(path.join(scratch, 'certifi-test-'));
  t.after(() => {
    assert.equal(path.dirname(path.resolve(out)), scratch);
    assert.ok(path.basename(out).startsWith('certifi-test-'));
    fs.rmSync(out, { recursive: true, force: true });
  });
  assert.equal(path.dirname(out), scratch);
  const wheel = path.join(out, 'test.whl'), dir = path.join(out, 'certifi'), data = zip();
  const pins = { version: '2026.10.23', wheel_sha256: digest(data), cacert_sha256: digest(base[0].data),
    license_sha256: digest(base[1].data), metadata_sha256: digest(base[2].data) };
  fs.writeFileSync(wheel, data);
  return { out, wheel, dir, data, pins, manifest: { [CERTIFI_KEY]: pins.wheel_sha256 } };
}

test('certifi pins match reviewed immutable artifact and manifest', () => {
  assert.equal(CERTIFI_PINS.version, '2026.10.23');
  assert.equal(CERTIFI_PINS.wheel_sha256, 'e9b54de76e530bb6cf0ccd036787103acb58fd42ba930ea8bf948ddbaa2f61d1');
  assert.equal(CERTIFI_PINS.cacert_sha256, '2ef52ad56b4803e264045c915e55c236cbfa045b84b81efefc218a19eb44bd2b');
  assert.equal(CERTIFI_PINS.license_sha256, 'e93716da6b9c0d5a4a1df60fe695b370f0695603d21f6f83f053e42cfc10caf7');
  assert.equal(CERTIFI_PINS.metadata_sha256, 'e463e38f7fe7da36a11e6d3fecc5dfa5b97be8c8022620400750d79ae7d842bc');
  assert.equal(require('../../scripts/runtime-checksums.json')[CERTIFI_KEY], CERTIFI_PINS.wheel_sha256);
});

test('missing certifi pin fails and deletes wheel', t => {
  const f = fixture(t);
  assert.throws(() => installCertifiWheel(f.wheel, f.dir, {}, f.pins), /missing required checksum/);
  assert.equal(fs.existsSync(f.wheel), false);
  assert.equal(fs.existsSync(f.dir), false);
});

test('mismatched wheel hash fails and deletes wheel', t => {
  const f = fixture(t);
  fs.writeFileSync(f.wheel, zip([...base, { name: 'extra.txt', data: 'tampered but valid ZIP' }]));
  assert.throws(() => installCertifiWheel(f.wheel, f.dir, f.manifest, f.pins), /checksum mismatch/);
  assert.equal(fs.existsSync(f.wheel), false);
  assert.equal(fs.existsSync(f.dir), false);
});

test('mismatched CA hash fails before writes', t => {
  const f = fixture(t);
  assert.throws(() => installCertifiWheel(f.wheel, f.dir, f.manifest,
    { ...f.pins, cacert_sha256: 'bad' }), /CA checksum mismatch/);
  assert.equal(fs.existsSync(f.dir), false);
});

test('only allowlisted members returned for stored and deflated ZIPs', () => {
  for (const method of [0, 8]) {
    const files = readCertifiWheel(zip([...base, { name: 'certifi/core.py', data: 'code' },
      { name: 'payload.exe', data: 'executable' }].map(e => ({ ...e, method }))));
    assert.deepEqual(Object.keys(files).sort(), ['LICENSE', 'METADATA', 'cacert.pem']);
    assert.equal(files['cacert.pem'].toString(), base[0].data);
  }
});

for (const name of ['../escape', '/absolute', 'certifi\\evil', 'C:/drive', 'a/../b', './dot']) {
  test(`reject unsafe path ${name}`, () => {
    assert.throws(() => readCertifiWheel(zip([...base, { name }])), /unsafe\/duplicate name/);
  });
}
for (const [label, entry] of [
  ['symlink', { name: 'link', mode: 0o120777 }],
  ['duplicate', base[0]],
  ['oversize', { name: 'huge', size: 3 * 1024 * 1024 }],
  ['zip bomb', { name: 'bomb', data: 'x'.repeat(3 * 1024 * 1024), size: 1 }],
  ['CRC mismatch', { name: 'crc', crc: 1 }],
  ['declared size mismatch', { name: 'size', size: 1 }],
  ['encryption', { name: 'encrypted', flags: 1 }],
  ['unsupported compression', { name: 'method', method: 99 }],
  ['zip64', { name: 'zip64', extra: Buffer.from([1, 0, 0, 0]) }],
]) {
  test(`reject ${label}`, () => assert.throws(() => readCertifiWheel(zip([...base, entry]))));
}

test('reject truncated archive and missing required members', () => {
  assert.throws(() => readCertifiWheel(zip().subarray(0, -1)));
  assert.throws(() => readCertifiWheel(zip(base.slice(1))), /missing required/);
});

test('install writes only fixed outputs and replaces stale directory', t => {
  const f = fixture(t);
  fs.mkdirSync(f.dir); fs.writeFileSync(path.join(f.dir, 'stale.py'), 'old');
  installCertifiWheel(f.wheel, f.dir, f.manifest, f.pins);
  assert.deepEqual(fs.readdirSync(f.dir).sort(), ['LICENSE', 'METADATA', 'PROVENANCE.json', 'cacert.pem']);
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(f.dir, 'PROVENANCE.json'))), f.pins);
  assert.equal(certifiCurrent(f.dir, f.pins), true);
  assert.equal(certifiCurrent(f.dir, { ...f.pins, version: 'old' }), false);
  fs.writeFileSync(path.join(f.dir, 'cacert.pem'), 'tampered');
  assert.equal(certifiCurrent(f.dir, f.pins), false);
});

test('fetch skips only matching provenance and refreshes stale or missing files', async t => {
  const f = fixture(t); let calls = 0;
  const fetch = async (url, dest, key) => {
    calls++;
    assert.match(url, /^https:\/\/files\.pythonhosted\.org\/packages\/32\/d1\//);
    assert.equal(key, CERTIFI_KEY);
    assert.equal(fs.existsSync(f.dir), false);
    fs.writeFileSync(dest, f.data);
  };
  await fetchCertifi(f.out, f.manifest, fetch, f.pins);
  await fetchCertifi(f.out, f.manifest, fetch, f.pins);
  assert.equal(calls, 1);
  fs.writeFileSync(path.join(f.dir, 'PROVENANCE.json'), '{}');
  await fetchCertifi(f.out, f.manifest, fetch, f.pins);
  fs.unlinkSync(path.join(f.dir, 'LICENSE'));
  await fetchCertifi(f.out, f.manifest, fetch, f.pins);
  assert.equal(calls, 3);
  fs.writeFileSync(path.join(f.out, CERTIFI_KEY), 'partial');
  await assert.rejects(fetchCertifi(f.out, {}, fetch, f.pins), /missing required checksum/);
  assert.equal(fs.existsSync(path.join(f.out, CERTIFI_KEY)), false);
  assert.equal(calls, 3);
});

test('requiring module has no writes, subprocesses, downloads or exits', () => {
  const modulePath = require.resolve('../../scripts/fetch-runtimes');
  const code = `const fail=()=>{throw Error('side effect')};
    const fs=require('node:fs');
    for(const name of ['writeFileSync','mkdirSync','rmSync','createWriteStream']) fs[name]=fail;
    require('node:https').get=fail; require('node:child_process').execSync=fail;
    process.exit=fail; require(${JSON.stringify(modulePath)});`;
  execFileSync(process.execPath, ['-e', code]);
});

for (const [name, alter] of [
  ['extra file', f => fs.writeFileSync(path.join(f.dir, 'stale.py'), 'stale')],
  ['subdirectory', f => fs.mkdirSync(path.join(f.dir, 'extra'))],
  ['empty LICENSE', f => fs.writeFileSync(path.join(f.dir, 'LICENSE'), '')],
  ['altered LICENSE', f => fs.writeFileSync(path.join(f.dir, 'LICENSE'), 'wrong')],
  ['altered METADATA', f => fs.writeFileSync(path.join(f.dir, 'METADATA'), 'wrong')],
  ['altered PROVENANCE', f => fs.writeFileSync(path.join(f.dir, 'PROVENANCE.json'), '{}')],
  ['missing file', f => fs.unlinkSync(path.join(f.dir, 'METADATA'))],
]) {
  test(`cache refresh rejects ${name}`, async t => {
    const f = fixture(t);
    installCertifiWheel(f.wheel, f.dir, f.manifest, f.pins);
    alter(f);
    assert.equal(certifiCurrent(f.dir, f.pins), false);
    let calls = 0;
    await fetchCertifi(f.out, f.manifest, async (_, dest) => { calls++; fs.writeFileSync(dest, f.data); }, f.pins);
    assert.equal(calls, 1);
    assert.deepEqual(fs.readdirSync(f.dir).sort(), ['LICENSE', 'METADATA', 'PROVENANCE.json', 'cacert.pem']);
    for (let i = 0; i < base.length; i++) {
      assert.equal(fs.readFileSync(path.join(f.dir, ['cacert.pem', 'LICENSE', 'METADATA'][i]), 'utf8'), base[i].data);
    }
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(f.dir, 'PROVENANCE.json'))), f.pins);
  });
}

test('cache symlink rejected (skips if platform denies symlink creation)', async t => {
  const f = fixture(t);
  installCertifiWheel(f.wheel, f.dir, f.manifest, f.pins);
  const target = path.join(f.out, 'original-license');
  fs.renameSync(path.join(f.dir, 'LICENSE'), target);
  try { fs.symlinkSync(target, path.join(f.dir, 'LICENSE'), 'file'); }
  catch (err) {
    if (['EPERM', 'EACCES', 'ENOTSUP'].includes(err.code)) { t.skip(`symlink creation unavailable: ${err.code}`); return; }
    throw err;
  }
  assert.equal(certifiCurrent(f.dir, f.pins), false);
  let calls = 0;
  await fetchCertifi(f.out, f.manifest, async (_, dest) => { calls++; fs.writeFileSync(dest, f.data); }, f.pins);
  assert.equal(calls, 1);
  assert.equal(fs.lstatSync(path.join(f.dir, 'LICENSE')).isSymbolicLink(), false);
  assert.equal(certifiCurrent(f.dir, f.pins), true);
});

test('current exact cache removes leftover wheel without fetching', async t => {
  const f = fixture(t);
  installCertifiWheel(f.wheel, f.dir, f.manifest, f.pins);
  const leftover = path.join(f.out, CERTIFI_KEY);
  fs.writeFileSync(leftover, 'leftover');
  let calls = 0;
  await fetchCertifi(f.out, f.manifest, async () => { calls++; }, f.pins);
  assert.equal(calls, 0);
  assert.equal(fs.existsSync(leftover), false);
  assert.equal(certifiCurrent(f.dir, f.pins), true);
});

test('failed fetch removes partial leftover wheel', async t => {
  const f = fixture(t);
  const leftover = path.join(f.out, CERTIFI_KEY);
  await assert.rejects(fetchCertifi(f.out, f.manifest, async (_, dest) => {
    fs.writeFileSync(dest, 'partial'); throw Error('injected fetch failure');
  }, f.pins), /injected fetch failure/);
  assert.equal(fs.existsSync(leftover), false);
});

for (const key of ['license_sha256', 'metadata_sha256']) {
  test(`install rejects mismatched ${key} before writes`, t => {
    const f = fixture(t);
    assert.throws(() => installCertifiWheel(f.wheel, f.dir, f.manifest, { ...f.pins, [key]: 'bad' }), /checksum mismatch/);
    assert.equal(fs.existsSync(f.dir), false);
    assert.equal(fs.existsSync(f.wheel), false);
  });
}

function directoryEntries(data) {
  const end = data.length - 22, positions = [];
  let offset = data.readUInt32LE(end + 16);
  for (let i = 0; i < data.readUInt16LE(end + 10); i++) {
    positions.push(offset);
    offset += 46 + data.readUInt16LE(offset + 28) + data.readUInt16LE(offset + 30) + data.readUInt16LE(offset + 32);
  }
  return positions;
}

function unsafeWithoutWrites(data) {
  const names = ['writeFileSync', 'appendFileSync', 'mkdirSync', 'rmSync', 'createWriteStream'];
  const originals = names.map(n => fs[n]), writes = [];
  try {
    names.forEach(n => { fs[n] = () => { writes.push(n); throw Error('unexpected filesystem write'); }; });
    assert.throws(() => readCertifiWheel(data), /unsafe certifi wheel/);
  } finally { names.forEach((n, i) => { fs[n] = originals[i]; }); }
  assert.deepEqual(writes, []);
}

test('reject overlapping entry ranges without writes', () => {
  const inner = { name: 'inner.txt', data: 'nested', method: 0 };
  const innerZip = zip([inner]);
  const localBytes = innerZip.subarray(0, innerZip.readUInt32LE(innerZip.length - 6));
  const data = zip([...base, { name: 'carrier.txt', data: localBytes, method: 0 }, inner]);
  const central = directoryEntries(data), carrier = data.readUInt32LE(central[3] + 42);
  const nested = carrier + 30 + data.readUInt16LE(carrier + 26) + data.readUInt16LE(carrier + 28);
  data.writeUInt32LE(nested, central[4] + 42);
  unsafeWithoutWrites(data);
});

for (const mismatch of ['name', 'method', 'flags']) {
  test(`reject local central ${mismatch} mismatch without writes`, () => {
    const data = zip(), local = data.readUInt32LE(directoryEntries(data)[0] + 42);
    if (mismatch === 'name') data[local + 30] ^= 1;
    if (mismatch === 'method') data.writeUInt16LE(0, local + 8);
    if (mismatch === 'flags') data.writeUInt16LE(8, local + 6);
    unsafeWithoutWrites(data);
  });
}

for (const field of ['count', 'offset']) {
  test(`reject inconsistent EOCD ${field} without writes`, () => {
    const data = zip(), end = data.length - 22;
    if (field === 'count') data.writeUInt16LE(base.length + 1, end + 10);
    else data.writeUInt32LE(data.readUInt32LE(end + 16) + 1, end + 16);
    unsafeWithoutWrites(data);
  });
}
