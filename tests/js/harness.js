'use strict';
/**
 * Loads the real main.js against stubbed Electron and child_process.
 *
 * main.js has no exports and does all its work at module load plus via event
 * handlers, so the only way to exercise it is to intercept its requires. This
 * tests the code that actually ships rather than a refactored copy of it.
 */
const Module = require('node:module');
const path = require('node:path');
const fs = require('node:fs');
const { EventEmitter } = require('node:events');
const { Readable } = require('node:stream');

const ROOT = path.join(__dirname, '..', '..');
const MAIN = path.join(ROOT, 'main.js');

const ORIGINAL_PLATFORM = Object.getOwnPropertyDescriptor(process, 'platform');

/** Restore the real process.platform. Safe to call repeatedly. */
function restorePlatform() {
  if (ORIGINAL_PLATFORM) Object.defineProperty(process, 'platform', ORIGINAL_PLATFORM);
}

// Backstop only. Every test disposes its own harness via t.after(); this exists
// so a thrown assertion cannot leave the override set for a later file sharing
// the runner process.
process.on('exit', restorePlatform);

/** Minimal Popen-alike: what main.js touches on a spawned backend. */
class FakeChild extends EventEmitter {
  constructor() {
    super();
    // Real Readables: main.js pipes stdout through readline.createInterface,
    // which requires an actual stream, not a bare emitter.
    this.stdout = new Readable({ read() {} });
    this.stderr = new Readable({ read() {} });
    this.killed = false;
    this.signals = [];
    this.writes = [];
    // Ordered trace of shutdown-relevant calls. Without it, removing
    // stdin.end() or reversing the end/kill order goes undetected - and
    // closing stdin first is the whole point of H3: it lets the Python side
    // drop out of its read loop and reap its own yt-dlp / HandBrake children,
    // which a bare kill() would orphan.
    this.calls = [];
    const child = this;
    this.stdin = {
      writable: true,
      write(chunk) { child.writes.push(String(chunk)); child.calls.push('write'); return true; },
      end() { this.writable = false; child.calls.push('stdin.end'); },
    };
  }

  /** Feed a JSON-lines event to main.js exactly as the Python backend would. */
  emitLine(obj) {
    this.stdout.push(JSON.stringify(obj) + '\n');
  }
  kill(sig) {
    this.signals.push(sig || 'SIGTERM');
    this.calls.push(`kill:${sig || 'SIGTERM'}`);
    this.killed = true;
    return true;
  }
  /** Simulate the OS reaping the process. */
  exitWith(code = 0, sig = null) {
    this.killed = true;
    this.emit('exit', code, sig);
  }
}

function makeApp() {
  const app = new EventEmitter();
  let readyResolve;
  const readyPromise = new Promise((r) => { readyResolve = r; });
  Object.assign(app, {
    name: 'Cove Video Downloader',
    isPackaged: false,
    quitCalls: 0,
    setName(n) { this.name = n; },
    setPath() {},
    getPath(k) { return path.join('/tmp/cove-test', k); },
    getVersion() { return '0.0.0-test'; },
    whenReady() { return readyPromise; },
    quit() { this.quitCalls += 1; },
    _fireReady() { readyResolve(); return readyPromise; },
  });
  return app;
}

function makeBrowserWindow(state) {
  class BW extends EventEmitter {
    constructor() {
      super();
      this.webContents = Object.assign(new EventEmitter(), {
        send() {}, setWindowOpenHandler() {}, openDevTools() {},
      });
      state.windows.push(this);
    }
    loadFile() { return Promise.resolve(); }
    isDestroyed() { return false; }
    isMaximized() { return false; }
    maximize() {} unmaximize() {} minimize() {} close() {} show() {}
    setMenuBarVisibility() {}
    once() {}
  }
  BW.getAllWindows = () => state.windows;
  return BW;
}

/**
 * @param {object} opts
 * @param {string} opts.platform     value to report as process.platform
 * @param {boolean} opts.spawnFails  make spawn emit ENOENT asynchronously
 * @param {boolean} opts.failLoad    throw during main.js IPC registration
 * @param {'present'|'missing'} opts.interpreter simulated POSIX discovery
 * @returns harness handle
 */
function loadMain(opts = {}) {
  const platform = opts.platform || 'linux';
  const state = { windows: [], children: [], spawnCalls: [], openedPaths: [], ipc: new Map(), events: [] };
  const app = makeApp();
  const BrowserWindow = makeBrowserWindow(state);

  const electron = {
    app,
    BrowserWindow,
    ipcMain: { handle(ch, fn) {
      if (opts.failLoad) throw new Error('forced main load failure');
      state.ipc.set(ch, fn);
    } },
    shell: { openPath: async (target) => { state.openedPaths.push(target); return ''; } },
    dialog: { showOpenDialog: async () => ({ canceled: true, filePaths: [] }) },
    nativeImage: { createFromPath: () => ({ isEmpty: () => true }) },
  };

  const fakeSpawn = (cmd, args) => {
    state.spawnCalls.push({ cmd, args: [...args] });
    const child = new FakeChild();
    state.children.push(child);
    if (opts.spawnFails) {
      // ENOENT is delivered asynchronously by real spawn, never thrown.
      queueMicrotask(() => {
        const err = new Error('spawn python3 ENOENT');
        err.code = 'ENOENT';
        child.emit('error', err);
      });
    }
    return child;
  };

  const origLoad = Module._load;
  // Only main.js sees this facade. Folder validation and all other filesystem
  // work still use the host filesystem, even when simulating another platform.
  const absoluteInterpreters = new Set([
    '/usr/bin/python3', '/usr/local/bin/python3', '/opt/homebrew/bin/python3',
  ]);
  const discoveryFs = new Proxy(fs, {
    get(target, key) {
      if (key === 'accessSync') return (file, ...args) => {
        const name = typeof file === 'string' ? path.basename(file) : '';
        if (name === 'python3' || name === 'python') {
          if (name === 'python3' && opts.interpreter !== 'missing') return;
          const error = new Error(`ENOENT: ${file}`);
          error.code = 'ENOENT';
          throw error;
        }
        return target.accessSync(file, ...args);
      };
      if (key === 'existsSync') return (file) => (
        absoluteInterpreters.has(file) ? opts.interpreter !== 'missing' : target.existsSync(file)
      );
      return Reflect.get(target, key);
    },
  });
  // main.js branches on process.platform inside event handlers, not only at
  // load time, so the override must outlive the require. dispose() reverts it.
  Object.defineProperty(process, 'platform', { value: platform, configurable: true });

  Module._load = function (request, parent, isMain) {
    // Only intercept requires coming from main.js itself. Matching './portable'
    // globally would rewrite the same specifier for any other module.
    const fromMain = parent && parent.filename === MAIN;
    if (fromMain) {
      if (request === 'electron') return electron;
      if (platform !== 'win32' && (request === 'path' || request === 'node:path')) {
        // Absolute POSIX candidates contain '/', even on a Windows test host.
        // Keep host path operations for actual backend and folder paths.
        return { ...path, sep: '/' };
      }
      if (platform !== 'win32' && (request === 'fs' || request === 'node:fs')) {
        return discoveryFs;
      }
      if (request === 'node:child_process' || request === 'child_process') {
        return { ...origLoad.call(this, 'node:child_process', parent, isMain), spawn: fakeSpawn };
      }
      if (request === './portable') return { setupPortableMode: () => false };
    }
    return origLoad.call(this, request, parent, isMain);
  };

  delete require.cache[MAIN];
  try {
    require(MAIN);
  } catch (error) {
    restorePlatform();
    throw error;
  } finally {
    Module._load = origLoad;
    delete require.cache[MAIN];
  }

  // Capture every event main.js pushes at the renderer.
  const sent = [];
  const patchWindow = (w) => { w.webContents.send = (ch, ev) => sent.push({ ch, ev }); };

  return {
    app, BrowserWindow, state, sent, discoveryFs,
    get child() { return state.children[state.children.length - 1]; },
    get childCount() { return state.children.length; },
    get windowCount() { return state.windows.length; },
    patchWindow,
    /** Revert the process.platform override this harness installed. */
    dispose() { restorePlatform(); },
    /**
     * Model the user closing every window: real BrowserWindow.getAllWindows()
     * stops reporting closed windows, and main.js only emits window-all-closed
     * once that list is empty. Leaving the fake list populated meant the
     * "window opened and closed" test never actually closed anything.
     */
    closeAllWindows() {
      state.windows.length = 0;
      app.emit('window-all-closed');
    },
    /**
     * Fire app-ready, then signal renderer-ready so main.js flushes its event
     * buffer through webContents.send. Without the cove:ready call every event
     * accumulates in eventBuffer and `sent` stays empty.
     */
    async ready() {
      await app._fireReady();
      await tick();
      state.windows.forEach(patchWindow);
      const readyHandler = state.ipc.get('cove:ready');
      if (readyHandler) await readyHandler({});
      await tick();
      return this;
    },
  };
}

const tick = (n = 3) => new Promise((r) => {
  let i = 0;
  const step = () => (++i >= n ? r() : setImmediate(step));
  setImmediate(step);
});

module.exports = { loadMain, tick, FakeChild };
