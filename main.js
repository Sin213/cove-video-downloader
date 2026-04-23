const { app, BrowserWindow, ipcMain, shell, dialog, nativeImage } = require('electron');
const path = require('node:path');
const fs = require('node:fs');
const { spawn } = require('node:child_process');
const readline = require('node:readline');

const APP_ID = 'cove-video-downloader';
app.setName('Cove Video Downloader');
app.setPath('userData', path.join(app.getPath('appData'), APP_ID));

let mainWindow = null;
let pyProc = null;     // Python backend child process
let pyReady = false;   // flips true on first "ready" event

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

  // macOS / Linux: system python3 always available (AppImage inherits user PATH)
  return { cmd: 'python3', args: [script] };
}

function spawnBackend() {
  const { cmd, args } = resolveBackendCommand();
  try {
    pyProc = spawn(cmd, args, {
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
      env: {
        ...process.env,
        PYTHONIOENCODING: 'utf-8',
        PYTHONUNBUFFERED:  '1',
        // Used by backend.py as yt-dlp's JavaScript runtime for YouTube's
        // signature / n-challenge solving. ELECTRON_RUN_AS_NODE=1 makes the
        // Electron binary behave as plain Node, saving us from bundling a
        // separate ~120 MB Deno.
        COVE_NODE_BIN: process.execPath,
      },
    });
  } catch (err) {
    console.error('[cove] failed to spawn backend:', err);
    return;
  }

  const rl = readline.createInterface({ input: pyProc.stdout, crlfDelay: Infinity });
  rl.on('line', (line) => {
    let event;
    try { event = JSON.parse(line); } catch { return; }
    if (event.type === 'ready') pyReady = true;
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('cove:event', event);
    }
  });

  pyProc.stderr.on('data', (buf) => {
    const msg = buf.toString('utf8').trim();
    if (!msg) return;
    console.error('[cove.py]', msg);
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('cove:event', {
        type: 'log', tag: 'backend', tone: 'err', msg,
      });
    }
  });

  pyProc.on('exit', (code, sig) => {
    console.warn(`[cove] backend exited (code=${code}, sig=${sig})`);
    pyProc = null;
    pyReady = false;
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
    height: 920,
    minWidth: 900,
    minHeight: 640,
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
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (pyProc && !pyProc.killed) {
    try { pyProc.stdin.end(); } catch {}
    try { pyProc.kill(); } catch {}
  }
  if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', () => {
  if (pyProc && !pyProc.killed) {
    try { pyProc.stdin.end(); } catch {}
    try { pyProc.kill(); } catch {}
  }
});

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
ipcMain.handle('cove:folder:open', async (_, folderPath) => {
  try { await shell.openPath(folderPath); } catch {}
});

// Downloads and tool checks route to Python
ipcMain.handle('cove:download:start',  (_, params) => sendCommand({ cmd: 'start_download',  params }));
ipcMain.handle('cove:download:cancel', ()          => sendCommand({ cmd: 'cancel_download'         }));
ipcMain.handle('cove:tools:check',     ()          => sendCommand({ cmd: 'check_updates'          }));

// Initial state: default save path + app version + pending ready flag
ipcMain.handle('cove:init', () => ({
  version:   app.getVersion(),
  savePath:  app.getPath('downloads'),
  backendReady: pyReady,
}));
