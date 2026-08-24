'use strict';
/**
 * Backend lifecycle tests for main.js.
 *
 * These cover the exact paths that produced review findings and that no test
 * previously touched: the macOS stop/restart interaction, the pendingRestart
 * flag, and asynchronous spawn failure.
 */
const test = require('node:test');
const assert = require('node:assert');
const { loadMain, tick } = require('./harness');

// ── C2: a missing interpreter must report, not crash ──────────────────────
// NOTE: loadMain() overrides process.platform on EVERY call, including when it
// defaults to 'linux'. So every test needs dispose(), not only the darwin ones -
// on a non-Linux machine the default would otherwise pin platform for the rest
// of the run.
test('async spawn ENOENT is reported to the renderer, not thrown as uncaught', async (t) => {
  const h = await loadMain({ spawnFails: true }).ready();
  t.after(() => h.dispose());
  await tick(10);
  const down = h.sent.filter(s => s.ev && s.ev.type === 'backend_down');
  assert.strictEqual(down.length >= 1, true,
    'a missing interpreter must surface as a backend_down event');
  assert.match(down[0].ev.msg, /Python 3 was not found/i,
    'the message must tell the user what to install');
});

test('spawn is attempted exactly once at startup', async (t) => {
  const h = await loadMain().ready();
  t.after(() => h.dispose());
  assert.strictEqual(h.childCount, 1);
});

// ── H4: unexpected death restarts, with bounded backoff ───────────────────
test('unexpected backend exit restarts after a backoff, not immediately', async (t) => {
  const h = await loadMain().ready();
  t.after(() => h.dispose());
  t.mock.timers.enable({ apis: ['setTimeout'] });
  assert.strictEqual(h.childCount, 1);

  h.child.exitWith(1, null);          // crash, nobody asked for it
  await tick();
  assert.strictEqual(h.childCount, 1, 'restart must be deferred, not synchronous');

  t.mock.timers.tick(499);
  await tick();
  assert.strictEqual(h.childCount, 1, 'must still be waiting out the 500ms backoff');

  t.mock.timers.tick(2);
  await tick();
  assert.strictEqual(h.childCount, 2, 'a crashed backend should be respawned');
});

test('restarts are capped at 3 and then report backend_down', async (t) => {
  const h = await loadMain().ready();
  t.after(() => h.dispose());
  t.mock.timers.enable({ apis: ['setTimeout'] });

  const downCount = () => h.sent.filter(s => s.ev && s.ev.type === 'backend_down').length;
  // Backoff grows 500/1000/1500ms; 5s clears all three.
  const crash = async () => {
    if (h.child && !h.child.killed) h.child.exitWith(1, null);
    await tick();
    t.mock.timers.tick(5000);
    await tick();
  };

  // The first three crashes are each recoverable. Reporting backend_down here
  // would tell the user the app is dead while it is in fact recovering, so the
  // absence of the event during this phase is part of the requirement - not
  // just its presence at the end.
  for (let i = 1; i <= 3; i++) {
    await crash();
    assert.strictEqual(h.childCount, i + 1,
      `crash ${i} should have produced restart ${i}`);
    assert.strictEqual(downCount(), 0,
      `backend_down must not be reported while restart ${i} is still available`);
  }

  // The fourth crash exhausts the budget.
  await crash();
  assert.strictEqual(h.childCount, 4,
    `expected 1 initial + 3 restarts and no more, got ${h.childCount}`);
  assert.strictEqual(downCount(), 1,
    'exhausting the restart budget must report backend_down exactly once');
});

// ── H5 / round-1 P1: macOS must not resurrect a deliberately stopped backend ──
test('darwin: closing the last window stops the backend and does NOT restart it', async (t) => {
  const h = await loadMain({ platform: 'darwin' }).ready();
  t.after(() => h.dispose());
  t.mock.timers.enable({ apis: ['setTimeout'] });
  assert.strictEqual(h.childCount, 1);
  const first = h.child;

  h.closeAllWindows();
  await tick();
  assert.strictEqual(h.windowCount, 0, 'the scenario requires no windows to remain');
  assert.ok(first.calls.includes('stdin.end'), 'stdin must be closed on stop');
  assert.ok(first.signals.length > 0, 'backend should be signalled on window-all-closed');

  first.exitWith(0, 'SIGTERM');       // the deliberate stop completes
  await tick();
  // Advance well past the restart backoff: without this the assertion passes
  // vacuously, because a wrongly-scheduled restart simply had not fired yet.
  t.mock.timers.tick(5000);
  await tick();

  assert.strictEqual(h.childCount, 1,
    'a deliberate stop must not be classified as an unexpected death');
  assert.strictEqual(h.app.quitCalls, 0, 'darwin must not quit the app');
});

test('non-darwin: closing the last window quits the app', async (t) => {
  const h = await loadMain({ platform: 'linux' }).ready();
  t.after(() => h.dispose());
  h.closeAllWindows();
  await tick();
  assert.strictEqual(h.windowCount, 0, 'the scenario requires no windows to remain');
  assert.ok(h.app.quitCalls >= 1);
  assert.ok(h.child.calls.includes('stdin.end'), 'stdin must be closed before quitting');
});

// ── round-2 P1: activate during an in-flight stop ──────────────────────────
test('darwin: activate during an in-flight stop restarts once the child exits', async (t) => {
  const h = await loadMain({ platform: 'darwin' }).ready();
  t.after(() => h.dispose());
  const first = h.child;

  h.closeAllWindows();
  await tick();
  // Activate arrives BEFORE the child's exit lands: pyProc is still truthy.
  h.app.emit('activate');
  await tick();
  assert.strictEqual(h.childCount, 1, 'must not double-spawn while the old child lives');

  first.exitWith(0, 'SIGTERM');
  await tick(10);
  assert.strictEqual(h.childCount, 2,
    'pendingRestart must be honoured once the stopping child exits');
});

test('darwin: activate after the child already exited spawns immediately', async (t) => {
  const h = await loadMain({ platform: 'darwin' }).ready();
  t.after(() => h.dispose());
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const first = h.child;
  h.closeAllWindows();
  await tick();
  first.exitWith(0, 'SIGTERM');
  await tick();
  t.mock.timers.tick(5000);   // prove no delayed restart was queued either
  await tick();
  assert.strictEqual(h.childCount, 1, 'a deliberate stop must not queue a restart');

  h.app.emit('activate');
  // Asserted with no await: awaiting first would let a microtask- or
  // setImmediate-deferred spawn satisfy the check, so "immediately" would
  // not actually be tested.
  assert.strictEqual(h.childCount, 2,
    'activate with no live backend must spawn one synchronously, not deferred');
});

// ── round-3 P2: pendingRestart must not survive into a windowless state ────
test('darwin: a window opened and closed during a stop does not leave a stray backend', async (t) => {
  const h = await loadMain({ platform: 'darwin' }).ready();
  t.after(() => h.dispose());
  const first = h.child;
  const windowsAtStart = h.windowCount;

  h.closeAllWindows();
  await tick();
  assert.strictEqual(h.windowCount, 0, 'all windows should be closed');

  h.app.emit('activate');             // sets pendingRestart, opens a window
  await tick();
  assert.strictEqual(h.windowCount, windowsAtStart,
    'activate must actually create a window, or this test proves nothing');

  h.closeAllWindows();                // that window really closes again
  await tick();
  assert.strictEqual(h.windowCount, 0);

  first.exitWith(0, 'SIGTERM');
  await tick();
  t.mock.timers.enable({ apis: ['setTimeout'] });
  t.mock.timers.tick(5000);           // a queued restart would fire by now
  await tick();

  assert.strictEqual(h.childCount, 1,
    'pendingRestart must be cleared when all windows close, or the exit handler ' +
    'spawns a backend for a UI that no longer exists');
});

// ── before-quit must never restart ─────────────────────────────────────────
test('before-quit closes stdin before killing, and suppresses restart', async (t) => {
  const h = await loadMain().ready();
  t.after(() => h.dispose());
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const first = h.child;

  h.app.emit('before-quit');
  await tick();

  // Assert the shutdown actually happened and in the right order. Checking only
  // the process count would pass even if shutdownBackend were deleted outright,
  // because isQuitting alone suppresses the restart.
  assert.ok(first.calls.includes('stdin.end'),
    'stdin must be closed so Python can reap its own yt-dlp/HandBrake children');
  assert.ok(first.signals.includes('SIGTERM'), 'backend must be signalled');
  assert.ok(
    first.calls.indexOf('stdin.end') < first.calls.indexOf('kill:SIGTERM'),
    'stdin.end() must come before kill(), or the graceful path is skipped');

  first.exitWith(0, 'SIGTERM');
  await tick();
  t.mock.timers.tick(5000);           // a wrong restart would fire by now
  await tick();
  assert.strictEqual(h.childCount, 1, 'quitting must not respawn the backend');
});

// ── L10: folder open must validate its argument ───────────────────────────
test('cove:folder:open rejects a non-directory path', async (t) => {
  const h = await loadMain().ready();
  t.after(() => h.dispose());
  const handler = h.state.ipc.get('cove:folder:open');
  assert.ok(handler, 'cove:folder:open handler should be registered');
  assert.match(await handler({}, ''), /No folder set/i);
  assert.match(await handler({}, '/definitely/not/a/real/path'), /does not exist/i);
  assert.match(await handler({}, __filename), /not a folder/i);
});
