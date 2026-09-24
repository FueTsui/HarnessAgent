'use strict';

const { contextBridge, ipcRenderer } = require('electron');

// The renderer receives narrowly scoped operations, never Node, IPC primitives,
// filesystem access, root credentials, or arbitrary shell/open-path methods.
contextBridge.exposeInMainWorld('harnessDesktop', Object.freeze({
  getInfo: () => ipcRenderer.invoke('harness:info'),
  showInitialLogin: () => ipcRenderer.invoke('harness:initial-login'),
  openDataDirectory: () => ipcRenderer.invoke('harness:data-directory'),
  openLogs: () => ipcRenderer.invoke('harness:logs'),
  restartService: () => ipcRenderer.invoke('harness:restart'),
  setTheme: theme => {
    if (theme !== 'light' && theme !== 'dark') return Promise.reject(new Error('Invalid theme'));
    return ipcRenderer.invoke('harness:theme', theme);
  },
}));

ipcRenderer.on('harness:menu-action', (_event, action) => {
  if (action === 'new-task') {
    window.dispatchEvent(new CustomEvent('harness-desktop-action', { detail: { action } }));
  }
});

ipcRenderer.on('harness:theme-changed', (_event, theme) => {
  if (theme !== 'light' && theme !== 'dark') return;
  document.documentElement.dataset.theme = theme;
  if (location.protocol === 'http:') localStorage.setItem('gca_theme', theme);
  window.dispatchEvent(new CustomEvent('harness-desktop-theme', { detail: { theme } }));
});

window.addEventListener('DOMContentLoaded', () => {
  document.documentElement.dataset.harnessDesktop = 'true';
  document.addEventListener('click', event => {
    if (!event.isTrusted || event.defaultPrevented || event.button !== 0) return;
    const anchor = event.target instanceof Element ? event.target.closest('a[href]') : null;
    if (!anchor || anchor.hasAttribute('download')) return;
    let target;
    try { target = new URL(anchor.href, location.href); } catch { return; }
    if (target.protocol !== 'http:' && target.protocol !== 'https:') return;
    if (target.origin !== location.origin) {
      event.preventDefault();
      event.stopImmediatePropagation();
      // This channel is not exposed in the bridge. Only a real user click in the
      // isolated preload reaches it; scripts cannot manufacture isTrusted events.
      ipcRenderer.invoke('harness:external-link', target.href).catch(() => {});
    } else if (anchor.target === '_blank') {
      event.preventDefault();
      location.assign(target.href);
    }
  }, true);
});
