'use strict';
/**
 * Unit tests for the checksum gate in scripts/fetch-runtimes.js (audit L6).
 *
 * Only the pure decision function is exercised here: no network, no
 * filesystem writes, no large downloads. verifyChecksum(key, actualHash,
 * manifest) is the single place that decides whether a fetched runtime
 * archive may be shipped, so pinning its contract is the point.
 */
const test = require('node:test');
const assert = require('node:assert');
const { verifyChecksum } = require('../../scripts/fetch-runtimes');

const KEY = 'python-3.11.9-embed-amd64.zip';
const HASH_A = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
const HASH_B = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';

test('matching entry verifies and does not throw', () => {
  const verdict = verifyChecksum(KEY, HASH_A, { [KEY]: HASH_A });
  assert.strictEqual(verdict.pinned, true, 'an existing entry means pinned');
  assert.strictEqual(verdict.verified, true, 'a matching entry counts as verified');
});

test('mismatch throws naming the key and both hashes', () => {
  assert.throws(
    () => verifyChecksum(KEY, HASH_B, { [KEY]: HASH_A }),
    (err) => {
      assert.ok(err.message.includes(KEY), `message must name the key, got: ${err.message}`);
      assert.ok(err.message.includes(HASH_A), `message must contain the expected hash, got: ${err.message}`);
      assert.ok(err.message.includes(HASH_B), `message must contain the actual hash, got: ${err.message}`);
      return true;
    },
    'a silent checksum mismatch is worse than no check at all',
  );
});

test('missing entry reports unpinned, distinguishable from verified', () => {
  const verdict = verifyChecksum(KEY, HASH_A, {});
  // Both flags asserted so a regression that treats unknown files as
  // verified cannot pass this test by merely not throwing.
  assert.strictEqual(verdict.pinned, false, 'no entry must not claim the key is pinned');
  assert.strictEqual(verdict.verified, false, 'no entry must not claim verification');
});

test('hex comparison ignores case in manifest or computed hash', () => {
  const manifest = { [KEY]: HASH_A.toUpperCase() };
  const verdict = verifyChecksum(KEY, HASH_A.toUpperCase(), manifest);
  assert.strictEqual(verdict.verified, true, 'upper-case hex must still match');

  assert.throws(
    () => verifyChecksum(KEY, HASH_B, { [KEY]: HASH_A.toUpperCase() }),
    (err) => err.message.includes(HASH_B.toLowerCase()),
    'case-insensitive matching must not weaken mismatch detection',
  );
});
