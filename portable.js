const { app } = require('electron');
const path = require('node:path');
const fs = require('node:fs');

function setupPortableMode() {
    const exeDir = process.env.PORTABLE_EXECUTABLE_DIR || path.dirname(app.getPath('exe'));
    const portableDataDir = path.join(exeDir, 'cove-app-data');
    const markerFile = path.join(exeDir, 'portable.marker');

    // The marker file is the explicit opt-in. A bare `cove-app-data` directory
    // only counts when this build was actually launched as the portable target
    // - otherwise an installed copy that happens to sit next to a folder of
    // that name silently redirects all its state.
    const markerPresent = fs.existsSync(markerFile);
    const isPortableBuild = !!process.env.PORTABLE_EXECUTABLE_DIR;
    if (!markerPresent && !(isPortableBuild && fs.existsSync(portableDataDir))) {
        return false;
    }

    // Portable builds run from removable media and read-only locations
    // (Program Files, a mounted image). A throw here propagates out of
    // require('./portable') and kills the app before any window exists, so
    // fall back to the normal per-user data directory instead.
    try {
        fs.mkdirSync(portableDataDir, { recursive: true });
        const appDataDir = path.join(portableDataDir, app.name);
        fs.mkdirSync(appDataDir, { recursive: true });

        for (const sub of ['temp', 'cache', 'logs']) {
            fs.mkdirSync(path.join(appDataDir, sub), { recursive: true });
        }

        app.setPath('userData', appDataDir);
        app.setPath('appData', portableDataDir);
        app.setPath('temp', path.join(appDataDir, 'temp'));
        app.setPath('sessionData', path.join(appDataDir, 'cache'));
        app.setPath('logs', path.join(appDataDir, 'logs'));
        return true;
    } catch (err) {
        console.error('[cove] portable mode unavailable, using default paths:', err.message);
        return false;
    }
}

module.exports = { setupPortableMode };
