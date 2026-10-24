const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { spawnSync } = require('node:child_process');

test('bundled Electron provides Node >= 22 for yt-dlp EJS', () => {
  const executable = require('electron');
  assert.ok(fs.existsSync(executable), `Missing Electron executable: ${executable}`);
  const result = spawnSync(executable, ['-p', 'process.versions.node'], {
    env: { ...process.env, ELECTRON_RUN_AS_NODE: '1' },
    encoding: 'utf8',
    timeout: 10000,
  });
  assert.ifError(result.error);
  assert.equal(result.status, 0, result.stderr);
  const version = result.stdout.trim();
  assert.match(version, /^v?\d+\.\d+\.\d+/);
  assert.ok(Number(version.replace(/^v/, '').split('.')[0]) >= 22,
    `yt-dlp EJS requires Node >= 22; bundled Electron reports Node ${version}`);
});
