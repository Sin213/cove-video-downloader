const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('coveAPI', {
  init: () => ipcRenderer.invoke('cove:init'),

  folder: {
    browse: (initial) => ipcRenderer.invoke('cove:folder:browse', initial),
    open:   (p)       => ipcRenderer.invoke('cove:folder:open',   p),
  },

  download: {
    start:  (params) => ipcRenderer.invoke('cove:download:start', params),
    cancel: ()       => ipcRenderer.invoke('cove:download:cancel'),
  },

  tools: {
    checkUpdates: () => ipcRenderer.invoke('cove:tools:check'),
  },

  win: {
    close:           () => ipcRenderer.invoke('cove:window:close'),
    minimize:        () => ipcRenderer.invoke('cove:window:minimize'),
    maximizeToggle:  () => ipcRenderer.invoke('cove:window:maximizeToggle'),
    isMaximized:     () => ipcRenderer.invoke('cove:window:isMaximized'),
    onStateChanged:  (cb) => {
      const h = (_e, payload) => cb(payload);
      ipcRenderer.on('cove:window:stateChanged', h);
      return () => ipcRenderer.removeListener('cove:window:stateChanged', h);
    },
  },

  // Subscribe to backend events (log lines, item state, tools_ready, download_complete, ready).
  onEvent: (cb) => {
    const h = (_e, event) => cb(event);
    ipcRenderer.on('cove:event', h);
    return () => ipcRenderer.removeListener('cove:event', h);
  },
});
