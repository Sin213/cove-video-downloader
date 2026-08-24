const { app, BrowserWindow, ipcMain, shell, dialog, nativeImage } = require('electron');
const path = require('node:path');
const fs = require('node:fs');
const { spawn } = require('node:child_process');
const readline = require('node:readline');
const { setupPortableMode } = require('./portable');

const APP_ID = 'cove-video-downloader';
app.setName('Cove Video Downloader');
const _isPortable = setupPortableMode();
if (!_isPortable) app.setPath('userData', path.join(app.getPath('appData'), APP_ID));

// The AppImage launcher exports LD_LIBRARY_PATH pointing at the bundle's
// libraries. shell.openPath spawns xdg-open with our environment, so the
// opened file manager inherits it, loads the bundle's outdated libs, and
// crashes on startup (e.g. liblzma "version XZ_5.4 not found"). Returns
// null when not running from an AppImage.
function appImageChildEnv() {
  if (process.platform !== 'linux') return null;
  if (!process.env.APPIMAGE && !process.env.APPDIR) return null;
  const env = { ...process.env };
  delete env.LD_LIBRARY_PATH;
  delete env.LD_PRELOAD;
  delete env.GSETTINGS_SCHEMA_DIR;
  return env;
}

async function openPathExternal(target) {
  const env = appImageChildEnv();
  if (env) {
    try {
      spawn('xdg-open', [target], { env, detached: true, stdio: 'ignore' }).unref();
      return '';
    } catch {
      // fall through to shell.openPath
    }
  }
  return shell.openPath(target);
}

let mainWindow = null;
let pyProc = null;        // Python backend child process
let pyReady = false;      // flips true when tools_ready fires
let isQuitting = false;      // app is quitting outright
let backendStopping = false; // backend was stopped deliberately (incl. macOS)
let pendingRestart = false;  // 'activate' arrived while a stop was still in flight
let backendRestarts = 0;
const MAX_BACKEND_RESTARTS = 3;
// The backend emits the first few events (ready / tools_ready) within a few
// hundred ms of spawn, which is often before the renderer has mounted React
// and subscribed to cove:event. Events arriving before subscription are
// dropped by Electron's IPC, so we buffer them here and flush once the
// renderer signals it's ready via cove:ready.
let rendererReady = false;
let eventBuffer = [];
// If the renderer never signals ready (crash, load failure) the buffer would
// otherwise grow for the life of the process.
const EVENT_BUFFER_MAX = 500;

function deliverEvent(event) {
  if (rendererReady && mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('cove:event', event);
  } else {
    eventBuffer.push(event);
    if (eventBuffer.length > EVENT_BUFFER_MAX) {
      eventBuffer.splice(0, eventBuffer.length - EVENT_BUFFER_MAX);
    }
  }
}

function flushEventBuffer() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  for (const event of eventBuffer) {
    mainWindow.webContents.send('cove:event', event);
  }
  eventBuffer = [];
}

// ───────────────────────────── Python backend ─────────────────────────────

function resolveBackendCommand() {
  // When packaged, the embeddable Python runtime + backend.py live under
  // resources/. When running from source (npm start), call the system
  // Python against python/backend.py in the repo.
  const script = app.isPackaged
    ? path.join(process.resourcesPath, 'app.asar.unpacked', 'python', 'backend.py')
    : path.join(__dirname, 'python', 'backend.py');

  if (process.platform === 'win32') {
    // Packaged: use the bundled embeddable python.exe
    const bundled = path.join(process.resourcesPath, 'runtime', 'python.exe');
    if (fs.existsSync(bundled)) return { cmd: bundled, args: [script] };
    // Fallback: system python on PATH
    return { cmd: 'python', args: [script] };
  }

  // macOS / Linux: probe the usual interpreters instead of assuming `python3`.
  // A clean macOS has no `python3` on PATH, and minimal Linux images often
  // don't either - and spawn's ENOENT is async, so assuming one crashed the
  // main process instead of reporting a missing dependency.
  const candidates = [
    'python3',
    '/usr/bin/python3',
    '/usr/local/bin/python3',
    '/opt/homebrew/bin/python3',
    'python',
  ];
  for (const cand of candidates) {
    if (cand.includes(path.sep)) {
      if (fs.existsSync(cand)) return { cmd: cand, args: [script] };
    } else if (whichSync(cand)) {
      return { cmd: cand, args: [script] };
    }
  }
  return null;
}

// Minimal PATH lookup - avoids a dependency and keeps resolution synchronous
// so spawnBackend can report a missing interpreter before it ever calls spawn.
function whichSync(name) {
  const dirs = (process.env.PATH || '').split(path.delimiter).filter(Boolean);
  for (const dir of dirs) {
    const full = path.join(dir, name);
    try {
      fs.accessSync(full, fs.constants.X_OK);
      return full;
    } catch { /* keep looking */ }
  }
  return null;
}

const PY_MISSING_MSG =
  'Python 3 was not found. Install Python 3.9 or newer and restart the app ' +
  '(Linux: `sudo apt install python3`, macOS: `brew install python3`).';

function reportBackendFailure(msg) {
  pyProc = null;
  pyReady = false;
  deliverEvent({ type: 'log', tag: 'backend', tone: 'err', msg });
  deliverEvent({ type: 'backend_down', msg });
}

function spawnBackend() {
  const resolved = resolveBackendCommand();
  if (!resolved) {
    console.error('[cove] no Python interpreter found');
    reportBackendFailure(PY_MISSING_MSG);
    return;
  }
  const { cmd, args } = resolved;
  try {
    pyProc = spawn(cmd, args, {
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
      env: {
        ...process.env,
        PYTHONIOENCODING: 'utf-8',
        PYTHONUNBUFFERED:  '1',
        COVE_NODE_BIN: process.execPath,
        ...(_isPortable ? { COVE_PORTABLE_DATA: path.join(
          process.env.PORTABLE_EXECUTABLE_DIR || path.dirname(app.getPath('exe')),
          'cove-app-data'
        ) } : {}),
      },
    });
  } catch (err) {
    console.error('[cove] failed to spawn backend:', err);
    reportBackendFailure(`Could not start the Python backend: ${err.message}`);
    return;
  }

  // spawn() reports ENOENT asynchronously via 'error', not by throwing. With no
  // handler Node re-throws it as an uncaught exception and the app dies with no
  // message, which is exactly what a missing `python3` used to do.
  pyProc.on('error', (err) => {
    console.error('[cove] backend spawn error:', err);
    reportBackendFailure(
      err.code === 'ENOENT' ? PY_MISSING_MSG
                            : `Python backend error: ${err.message}`);
  });

  const rl = readline.createInterface({ input: pyProc.stdout, crlfDelay: Infinity });
  rl.on('line', (line) => {
    let event;
    try { event = JSON.parse(line); } catch { return; }
    if (event.type === 'tools_ready') pyReady = true;
    deliverEvent(event);
  });

  pyProc.stderr.on('data', (buf) => {
    const msg = buf.toString('utf8').trim();
    if (!msg) return;
    console.error('[cove.py]', msg);
    deliverEvent({ type: 'log', tag: 'backend', tone: 'err', msg });
  });

  pyProc.on('exit', (code, sig) => {
    console.warn(`[cove] backend exited (code=${code}, sig=${sig})`);
    pyProc = null;
    pyReady = false;
    if (isQuitting) return;
    if (backendStopping) {
      backendStopping = false;
      // An 'activate' landed mid-stop and asked for the backend back.
      if (pendingRestart) {
        pendingRestart = false;
        backendRestarts = 0;
        spawnBackend();
      }
      return;
    }

    // Unexpected death. Restart once with backoff, then tell the renderer so
    // it can unstick the queue instead of showing "fetching" forever.
    if (backendRestarts < MAX_BACKEND_RESTARTS) {
      backendRestarts += 1;
      deliverEvent({
        type: 'log', tag: 'backend', tone: 'warn',
        msg: `Backend stopped unexpectedly - restarting (${backendRestarts}/${MAX_BACKEND_RESTARTS})...`,
      });
      setTimeout(() => { if (!isQuitting && !pyProc) spawnBackend(); }, 500 * backendRestarts);
    } else {
      reportBackendFailure(
        'The Python backend stopped and could not be restarted. Restart the app.');
    }
  });
}

function sendCommand(obj) {
  if (!pyProc || pyProc.killed || !pyProc.stdin.writable) return false;
  try {
    pyProc.stdin.write(JSON.stringify(obj) + '\n');
    return true;
  } catch {
    return false;
  }
}

// ───────────────────────────── Main window ────────────────────────────────

function createWindow() {
  const iconPath = path.join(__dirname, 'build', 'icon.png');
  const win = new BrowserWindow({
    width: 1320,
    height: 820,
    minWidth: 900,
    minHeight: 600,
    backgroundColor: '#0b1013',
    show: false,
    title: '',
    frame: false,
    icon: iconPath,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  // Linux window managers often ignore BrowserWindow.icon; set it explicitly
  // so GNOME/KDE taskbars show the cove icon instead of the default Electron one.
  if (process.platform === 'linux') {
    try {
      const img = nativeImage.createFromPath(iconPath);
      if (!img.isEmpty()) win.setIcon(img);
    } catch {}
  }

  win.setMenuBarVisibility(false);
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  win.once('ready-to-show', () => win.show());
  mainWindow = win;
  win.on('page-title-updated', (e) => e.preventDefault());
  win.on('closed', () => { if (mainWindow === win) mainWindow = null; });
  win.on('maximize',   () => win.webContents.send('cove:window:stateChanged', { maximized: true  }));
  win.on('unmaximize', () => win.webContents.send('cove:window:stateChanged', { maximized: false }));
}

// ───────────────────────────── App lifecycle ──────────────────────────────

app.whenReady().then(() => {
  spawnBackend();
  createWindow();
  app.on('activate', () => {
    // Reopening from the dock must bring the backend back too: window-all-closed
    // kills it on darwin, and without this the restored window had no tool
    // versions, no downloads and no search.
    // A stop may still be in flight: shutdownBackend() has signalled the child
    // but its 'exit' has not fired yet, so pyProc is still truthy. Spawning
    // now would be skipped and the exit handler would then consume
    // backendStopping and suppress the restart, leaving the window backendless.
    // Flag the intent instead and let the exit handler honour it.
    if (backendStopping && pyProc) {
      pendingRestart = true;
    } else if (!pyProc) {
      backendRestarts = 0; backendStopping = false; spawnBackend();
    }
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

// Ask the backend to shut down cleanly. Closing stdin makes it drop out of its
// read loop and kill its own yt-dlp / HandBrakeCLI children; a bare kill() only
// signals Python and leaves those grandchildren running. SIGKILL is a backstop
// if it does not exit in time.
function shutdownBackend() {
  const proc = pyProc;
  if (!proc || proc.killed) return;
  // Mark the stop as deliberate *before* signalling, so the exit handler does
  // not classify it as an unexpected death and respawn what we just killed.
  backendStopping = true;
  try { proc.stdin.end(); } catch {}
  try { proc.kill('SIGTERM'); } catch {}
  const t = setTimeout(() => { try { proc.kill('SIGKILL'); } catch {} }, 3000);
  if (typeof t.unref === 'function') t.unref();
}

app.on('window-all-closed', () => {
  // Any restart requested by a previous 'activate' is void now that no window
  // exists. Without this, a window opened and closed during an in-flight stop
  // leaves pendingRestart set and the exit handler spawns a backend for a UI
  // that is no longer there.
  pendingRestart = false;
  if (process.platform !== 'darwin') {
    isQuitting = true;
    shutdownBackend();
    app.quit();
  } else {
    // macOS keeps the app alive with no windows. The backend is stopped to free
    // resources and respawned from 'activate'; it must NOT be auto-restarted
    // here, or closing the last window immediately resurrects it.
    shutdownBackend();
  }
});

app.on('before-quit', () => {
  isQuitting = true;
  shutdownBackend();
});

// ───────────────────────────── Settings persistence ──────────────────────

const settingsPath = path.join(app.getPath('userData'), 'settings.json');

function loadSettings() {
  try {
    return JSON.parse(fs.readFileSync(settingsPath, 'utf8'));
  } catch {
    return {};
  }
}

function saveSettings(settings) {
  try {
    fs.writeFileSync(settingsPath, JSON.stringify(settings, null, 2), 'utf8');
  } catch {}
}

// ───────────────────────────── IPC handlers ───────────────────────────────

// Window controls
ipcMain.handle('cove:window:close',          () => mainWindow?.close());
ipcMain.handle('cove:window:minimize',       () => mainWindow?.minimize());
ipcMain.handle('cove:window:maximizeToggle', () => {
  if (!mainWindow) return;
  if (mainWindow.isMaximized()) mainWindow.unmaximize();
  else mainWindow.maximize();
});
ipcMain.handle('cove:window:isMaximized',    () => !!mainWindow?.isMaximized());

// Folder dialogs
ipcMain.handle('cove:folder:browse', async (_, initialPath) => {
  if (!mainWindow) return null;
  const res = await dialog.showOpenDialog(mainWindow, {
    title: 'Select output folder',
    defaultPath: initialPath || app.getPath('downloads'),
    properties: ['openDirectory', 'createDirectory'],
  });
  if (res.canceled || !res.filePaths.length) return null;
  return res.filePaths[0];
});
// The save path is a free-text field, so whatever the renderer sends could be
// a typo, a URL, or a non-directory. Hand only a real, existing directory to
// the desktop handler.
ipcMain.handle('cove:folder:open', async (_, folderPath) => {
  if (typeof folderPath !== 'string' || !folderPath.trim()) return 'No folder set.';
  const resolved = path.resolve(folderPath);
  let st;
  try {
    st = fs.statSync(resolved);
  } catch {
    return 'That folder does not exist yet.';
  }
  if (!st.isDirectory()) return 'That path is not a folder.';
  try { await openPathExternal(resolved); } catch { return 'Could not open that folder.'; }
  return '';
});

// Cookies file picker (Netscape cookies.txt exported from a browser extension).
ipcMain.handle('cove:file:browseCookies', async () => {
  if (!mainWindow) return null;
  const res = await dialog.showOpenDialog(mainWindow, {
    title: 'Select cookies.txt',
    defaultPath: app.getPath('downloads'),
    properties: ['openFile'],
    filters: [
      { name: 'Cookies', extensions: ['txt'] },
      { name: 'All Files', extensions: ['*'] },
    ],
  });
  if (res.canceled || !res.filePaths.length) return null;
  return res.filePaths[0];
});

// Downloads route to Python. yt-dlp auto-updates on backend startup, so
// there's no separate "check for updates" IPC path.
ipcMain.handle('cove:download:start',  (_, params) => sendCommand({ cmd: 'start_download',  params }));
ipcMain.handle('cove:download:cancel', ()          => sendCommand({ cmd: 'cancel_download'         }));
ipcMain.handle('cove:search:run',      (_, query, start) => sendCommand({ cmd: 'search', params: { query, start } }));

// Initial state: default save path + app version + saved settings
ipcMain.handle('cove:init', () => ({
  version:      app.getVersion(),
  savePath:     app.getPath('downloads'),
  backendReady: pyReady,
  settings:     loadSettings(),
}));

ipcMain.handle('cove:settings:save', (_, settings) => saveSettings(settings));

// Renderer calls this AFTER it has subscribed to cove:event via onEvent,
// so we flush any events the backend emitted during the React mount delay.
ipcMain.handle('cove:ready', () => {
  rendererReady = true;
  flushEventBuffer();
});
