'use strict';

const { app, BrowserWindow, Menu, ipcMain, dialog, shell, clipboard, nativeTheme } = require('electron');
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const path = require('node:path');
const http = require('node:http');
const { spawn } = require('node:child_process');
const { pathToFileURL } = require('node:url');

const APP_NAME = 'Harness Agent';
const CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'";
const options = parseOptions(process.argv.slice(app.isPackaged ? 1 : 2));
const home = path.resolve(options.home || path.join(process.env.LOCALAPPDATA || app.getPath('appData'), 'HarnessAgent'));
const packageRoot = app.isPackaged ? path.dirname(process.execPath) : path.resolve(__dirname, '..');
const python = path.join(packageRoot, 'runtime', process.platform === 'win32' ? 'python.exe' : 'python');
const runnerPath = app.isPackaged ? path.join(packageRoot, 'app', 'local_agent.py') : path.join(packageRoot, 'local_agent.py');
const statusPath = path.join(home, 'runtime.json');
const splashPath = path.join(__dirname, 'splash.html');
const splashUrl = pathToFileURL(splashPath).href;
const desktopHome = path.join(home, 'desktop');
const logsHome = path.join(home, 'logs');

let mainWin = null;
let service = null;
let serviceOrigin = '';
let serviceUrl = '';
let status = 'starting';
let statusMessage = '正在准备本地运行环境';
let activeLog = '';
let logStream = null;
let startPromise = null;
let stopPromise = null;
let restartPromise = null;
let quitPromise = null;
let quitting = false;
let theme = nativeTheme.shouldUseDarkColors ? 'dark' : 'light';
let pendingAction = null;
let credentialsDialogOpen = false;
let smokeStarted = false;
let healthTimer = null;
let blockedNavigations = 0;
let blockedWindows = 0;
let smokeExitCode = 0;

function parseOptions(args) {
  const result = {};
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--home' && args[i + 1]) result.home = args[++i];
    else if (args[i] === '--no-auto-open' || args[i] === '--no-browser') result.noAutoOpen = true;
    else if (args[i] === '--smoke-test') result.smokeTest = true;
    else if (args[i] === '--smoke-output' && args[i + 1]) result.smokeOutput = args[++i];
    else if (!args[i].startsWith('--')) continue;
    else if (args[i].startsWith('--allow-file-access') || args[i].startsWith('--disable-web-security') || args[i].startsWith('--no-sandbox')) {
      throw new Error('不支持关闭桌面安全边界的启动参数。');
    }
  }
  return result;
}

function sleep(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }
function isLive(child) { return !!child && Number.isInteger(child.pid) && child.pid > 0 && child.exitCode === null && child.signalCode === null; }
function log(message) {
  if (logStream && !logStream.destroyed) logStream.write(`${new Date().toISOString()} ${message}\n`);
}

function info() {
  return { name: APP_NAME, version: app.getVersion(), desktop: true, platform: process.platform,
    state: status, message: statusMessage, url: serviceUrl, home, theme };
}

function localPage(url) {
  try {
    const value = new URL(url);
    return value.href.split('#')[0].split('?')[0] === splashUrl;
  } catch { return false; }
}

function servicePage(url) {
  try {
    const value = new URL(url);
    return !!serviceOrigin && value.origin === serviceOrigin && !value.username && !value.password;
  } catch { return false; }
}

function isAppDocument(url) {
  if (!servicePage(url)) return false;
  const pathname = new URL(url).pathname;
  return pathname === '/' || pathname === '/login' || pathname === '/admin';
}

function validSender(event) {
  if (!mainWin || mainWin.isDestroyed() || event.sender !== mainWin.webContents ||
      event.senderFrame !== mainWin.webContents.mainFrame) return false;
  return localPage(event.senderFrame.url) || servicePage(event.senderFrame.url);
}

function handle(channel, callback) {
  ipcMain.handle(channel, async (event, ...args) => {
    if (!validSender(event)) throw new Error('Untrusted desktop IPC sender');
    return callback(...args);
  });
}

function safeHttpUrl(raw) {
  if (typeof raw !== 'string' || raw.length > 8192) return null;
  try {
    const value = new URL(raw);
    return ['http:', 'https:'].includes(value.protocol) && !value.username && !value.password ? value : null;
  } catch { return null; }
}

async function showInitialLogin() {
  if (credentialsDialogOpen || !mainWin || mainWin.isDestroyed()) return { shown: false };
  credentialsDialogOpen = true;
  try {
    let username = 'root';
    let password = '';
    // Read only this instance's explicitly chosen data home. Never read package
    // or repository configuration and never send secret values over IPC.
    const notePath = path.join(home, 'first-run.txt');
    if (fs.existsSync(notePath) && fs.statSync(notePath).size < 16384) {
      const text = await fsp.readFile(notePath, 'utf8');
      username = text.match(/^账号[：:]\s*(.*)$/m)?.[1]?.trim() || username;
      password = text.match(/^密码[：:]\s*(.*)$/m)?.[1]?.trim() || '';
    }
    if (!password) {
      const configPath = path.join(home, '.env');
      if (fs.existsSync(configPath) && fs.statSync(configPath).size < 65536) {
        const text = await fsp.readFile(configPath, 'utf8');
        username = text.match(/^ROOT_USERNAME\s*=\s*(.*)$/m)?.[1]?.trim() || username;
        password = text.match(/^ROOT_PASSWORD\s*=\s*(.*)$/m)?.[1]?.trim() || '';
      }
    }
    if (!password) {
      await dialog.showMessageBox(mainWin, { type: 'info', title: '首次登录凭据', message: '首次登录凭据尚未生成。', detail: '请等待本地服务完成初始化后重试。' });
      return { shown: false };
    }
    const answer = await dialog.showMessageBox(mainWin, { type: 'info', title: '首次登录凭据',
      message: `登录账号：${username}`, detail: `初始密码：${password}\n\n登录后请在账户设置中修改密码。修改后这里保存的初始密码将不再有效。`,
      buttons: ['复制初始密码', '关闭'], defaultId: 1, cancelId: 1, noLink: true });
    if (answer.response === 0) {
      await clipboard.writeText(password);
      // Avoid leaving the password on the clipboard indefinitely; preserve any
      // new clipboard text the user copies in the meantime.
      const copied = password;
      setTimeout(async () => {
        try { if (await clipboard.readText() === copied) await clipboard.clear(); }
        catch { /* Clipboard can be temporarily owned by another application. */ }
      }, 60000).unref();
    }
    return { shown: true };
  } finally { credentialsDialogOpen = false; }
}

function applyTheme(value) {
  if (value !== 'light' && value !== 'dark') throw new Error('Invalid theme');
  theme = value;
  nativeTheme.themeSource = value;
  if (mainWin && !mainWin.isDestroyed()) {
    mainWin.setBackgroundColor(value === 'dark' ? '#191919' : '#f6f6f5');
    if (process.platform === 'win32') mainWin.setTitleBarOverlay({ color: value === 'dark' ? '#191919' : '#f6f6f5', symbolColor: value === 'dark' ? '#ecebe7' : '#262622', height: 40 });
  }
  fsp.writeFile(path.join(desktopHome, 'preferences.json'), JSON.stringify({ theme }), 'utf8').catch(() => {});
  return { theme };
}

async function openLogs() {
  await fsp.mkdir(logsHome, { recursive: true });
  if (activeLog && fs.existsSync(activeLog)) shell.showItemInFolder(activeLog);
  else await shell.openPath(logsHome);
  return { opened: true };
}

function installIpc() {
  handle('harness:info', () => info());
  handle('harness:initial-login', () => showInitialLogin());
  handle('harness:data-directory', async () => { await shell.openPath(home); return { opened: true }; });
  handle('harness:logs', () => openLogs());
  handle('harness:restart', () => { void restartService(); return { requested: true }; });
  handle('harness:theme', value => applyTheme(value));
  handle('harness:external-link', async raw => {
    const url = safeHttpUrl(raw);
    if (!url || servicePage(url.href)) return { opened: false };
    await shell.openExternal(url.href, { activate: true });
    return { opened: true };
  });
}

function navigate(route) {
  if (status === 'ready' && mainWin && !mainWin.isDestroyed() && serviceOrigin) {
    mainWin.loadURL(new URL(route, serviceUrl).href).catch(() => {});
  }
}

function newTask() {
  if (!mainWin || mainWin.isDestroyed() || status !== 'ready') return;
  if (servicePage(mainWin.webContents.getURL()) && new URL(mainWin.webContents.getURL()).pathname === '/') {
    mainWin.webContents.send('harness:menu-action', 'new-task');
  } else { pendingAction = 'new-task'; navigate('/'); }
}

function buildMenu() {
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    { label: 'Harness Agent', submenu: [
      { label: '工作区', accelerator: 'CmdOrCtrl+1', click: () => navigate('/') },
      { label: '新建任务', accelerator: 'CmdOrCtrl+N', click: newTask },
      { label: '设置', accelerator: 'CmdOrCtrl+,', click: () => navigate('/admin') },
      { type: 'separator' },
      { label: '查看首次登录凭据', click: () => { void showInitialLogin(); } },
      { label: '打开数据目录', click: () => { void shell.openPath(home); } },
      { label: '查看运行日志', click: () => { void openLogs(); } },
      { type: 'separator' },
      { label: '重启本地服务', click: () => { void restartService(); } },
      { label: '退出 Harness Agent', accelerator: 'Alt+F4', click: () => { void requestQuit(); } },
    ] },
    { label: '编辑', submenu: [{ role: 'undo', label: '撤销' }, { role: 'redo', label: '重做' }, { type: 'separator' },
      { role: 'cut', label: '剪切' }, { role: 'copy', label: '复制' }, { role: 'paste', label: '粘贴' }, { role: 'selectAll', label: '全选' }] },
    { label: '视图', submenu: [
      { label: '重新载入界面', accelerator: 'CmdOrCtrl+R', click: () => { if (mainWin && !mainWin.isDestroyed()) mainWin.webContents.reload(); } },
      { role: 'resetZoom', label: '实际大小' }, { role: 'zoomIn', label: '放大' }, { role: 'zoomOut', label: '缩小' },
      { type: 'separator' }, { role: 'togglefullscreen', label: '全屏' },
      { label: '浅色外观', click: () => { applyTheme('light'); mainWin?.webContents.send('harness:theme-changed', 'light'); } },
      { label: '深色外观', click: () => { applyTheme('dark'); mainWin?.webContents.send('harness:theme-changed', 'dark'); } },
    ] },
  ]));
}

function createWindow() {
  const icon = [path.join(process.resourcesPath, 'HarnessAgent.ico'), path.join(packageRoot, 'HarnessAgent.ico'), path.join(packageRoot, 'packaging', 'windows', 'HarnessAgent.ico')].find(file => fs.existsSync(file));
  mainWin = new BrowserWindow({ width: 1280, height: 880, minWidth: 900, minHeight: 600,
    title: APP_NAME, icon, show: false, autoHideMenuBar: true,
    titleBarStyle: 'hidden', titleBarOverlay: { color: theme === 'dark' ? '#191919' : '#f6f6f5', symbolColor: theme === 'dark' ? '#ecebe7' : '#262622', height: 40 },
    backgroundColor: theme === 'dark' ? '#191919' : '#f6f6f5',
    webPreferences: { preload: path.join(__dirname, 'preload.cjs'), nodeIntegration: false, nodeIntegrationInWorker: false,
      nodeIntegrationInSubFrames: false, contextIsolation: true, sandbox: true, webSecurity: true,
      allowRunningInsecureContent: false, webviewTag: false, navigateOnDragDrop: false, devTools: false, spellcheck: false },
  });
  const contents = mainWin.webContents;
  const session = contents.session;
  const allowClipboardWrite = (webContents, permission, requestingOrigin, details) => {
    if (permission !== 'clipboard-sanitized-write' || webContents !== contents ||
        contents.isDestroyed() || details?.isMainFrame !== true || !serviceOrigin ||
        !isAppDocument(contents.mainFrame.url) || !isAppDocument(details.requestingUrl)) return false;
    try { return new URL(requestingOrigin).origin === serviceOrigin; } catch { return false; }
  };
  session.setPermissionRequestHandler((webContents, permission, callback, details) => {
    callback(allowClipboardWrite(webContents, permission, details?.requestingUrl, details));
  });
  session.setPermissionCheckHandler((webContents, permission, requestingOrigin, details) =>
    allowClipboardWrite(webContents, permission, requestingOrigin, details));
  session.setDevicePermissionHandler(() => false);
  session.on('select-usb-device', event => event.preventDefault());
  session.on('select-hid-device', event => event.preventDefault());
  session.on('select-serial-port', event => event.preventDefault());
  session.webRequest.onHeadersReceived((details, callback) => {
    const headers = { ...details.responseHeaders };
    if (servicePage(details.url)) {
      // Keep stricter backend policies on attachment/artifact responses intact.
      if (!Object.keys(headers).some(key => key.toLowerCase() === 'content-security-policy')) headers['Content-Security-Policy'] = [CSP];
      headers['X-Content-Type-Options'] = ['nosniff'];
    }
    callback({ responseHeaders: headers });
  });
  contents.on('will-attach-webview', event => event.preventDefault());
  contents.on('will-navigate', (event, legacyUrl) => {
    const url = event.url || legacyUrl;
    if (!servicePage(url) && !localPage(url)) { blockedNavigations++; event.preventDefault(); }
  });
  contents.on('will-frame-navigate', event => {
    // No frame may turn into a remote page with a desktop preload attached.
    const url = event.url;
    if (!servicePage(url) && !localPage(url)) { blockedNavigations++; event.preventDefault(); }
  });
  contents.on('will-redirect', (event, legacyUrl) => {
    const url = event.url || legacyUrl;
    if (!servicePage(url) && !localPage(url)) { blockedNavigations++; event.preventDefault(); }
  });
  contents.setWindowOpenHandler(details => {
    if (servicePage(details.url)) {
      // Preserve existing same-origin settings/preview links inside the desktop.
      setImmediate(() => { if (!contents.isDestroyed()) contents.loadURL(details.url).catch(() => {}); });
    } else blockedWindows++;
    return { action: 'deny' };
  });
  contents.on('page-title-updated', event => { event.preventDefault(); mainWin.setTitle(APP_NAME); });
  contents.on('did-finish-load', () => {
    if (pendingAction && isAppDocument(contents.getURL()) && new URL(contents.getURL()).pathname === '/') {
      const action = pendingAction; pendingAction = null;
      contents.send('harness:menu-action', action);
    }
    if (status === 'ready' && servicePage(contents.getURL()) && options.smokeTest && !smokeStarted) {
      smokeStarted = true;
      void runSmokeTest();
    }
  });
  contents.on('did-fail-load', (_event, code, description, _url, isMainFrame) => {
    if (isMainFrame && code !== -3 && status === 'ready') {
      log(`Renderer load failed (${code}): ${description}`);
      void showSplash('error', '界面加载未完成。请重试，或查看运行日志。');
    }
  });
  contents.on('render-process-gone', (_event, details) => {
    log(`Renderer exited: ${details.reason}`);
    if (!quitting) void showSplash('error', '界面进程已退出。点击重试恢复本地工作区。');
  });
  session.on('will-download', (event, item, webContents) => {
    const url = item.getURL();
    const allowedBlob = url.startsWith(`blob:${serviceOrigin}/`) && serviceOrigin;
    if (webContents !== contents || (!servicePage(url) && !allowedBlob)) { event.preventDefault(); return; }
    // Chromium's native Save dialog obtains the destination from the user.
    item.setSaveDialogOptions({ title: '保存 Agent 文件', buttonLabel: '保存',
      defaultPath: path.join(app.getPath('downloads'), path.basename(item.getFilename() || 'agent-file')) });
    item.once('done', (_event, state) => { if (state === 'interrupted') log('A user download was interrupted.'); });
  });
  mainWin.on('close', event => { if (!quitting) { event.preventDefault(); void requestQuit(); } });
  mainWin.on('closed', () => { mainWin = null; });
  mainWin.once('ready-to-show', () => { if (!options.noAutoOpen) mainWin.show(); });
  void mainWin.loadFile(splashPath);
}

async function showSplash(nextStatus, message) {
  status = nextStatus;
  statusMessage = message;
  if (mainWin && !mainWin.isDestroyed() && !localPage(mainWin.webContents.getURL())) {
    await mainWin.loadFile(splashPath).catch(() => {});
  }
}

function checkHealth(url) {
  return new Promise(resolve => {
    const request = http.get(new URL('/healthz', url), { timeout: 1800, agent: false }, response => {
      response.resume(); resolve(response.statusCode === 200);
    });
    request.on('timeout', () => { request.destroy(); resolve(false); });
    request.on('error', () => resolve(false));
  });
}

async function readyStatus(child) {
  try {
    const stat = await fsp.stat(statusPath);
    if (stat.size > 65536) return null;
    const current = JSON.parse((await fsp.readFile(statusPath, 'utf8')).replace(/^\uFEFF/, ''));
    const url = safeHttpUrl(current.url);
    if (!isLive(child) || current.state !== 'ready' || current.pid !== child.pid || !url ||
        url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port || url.pathname !== '/' || url.search || url.hash) return null;
    return await checkHealth(url.href) && isLive(child) ? url.href : null;
  } catch { return null; }
}

async function startService() {
  if (startPromise) return startPromise;
  startPromise = (async () => {
    await showSplash('starting', '正在准备本地运行环境');
    if (quitting || stopPromise || status !== 'starting') return false;
    if (!fs.existsSync(python) || !fs.existsSync(runnerPath)) {
      await showSplash('error', '程序文件不完整。请完整解压应用包，保留 runtime 与 app 目录。');
      return false;
    }
    await fsp.mkdir(logsHome, { recursive: true });
    if (quitting || stopPromise || status !== 'starting') return false;
    activeLog = path.join(logsHome, `desktop-${new Date().toISOString().replace(/[:.]/g, '-')}.log`);
    if (logStream) logStream.end();
    logStream = fs.createWriteStream(activeLog, { flags: 'a', encoding: 'utf8' });
    logStream.on('error', () => {});
    serviceOrigin = ''; serviceUrl = '';
    log('Starting isolated local service.');
    const child = spawn(python, ['-X', 'utf8', '-u', runnerPath, 'serve', '--managed', '--home', home,
      '--port', '17650', '--status-file', statusPath], { cwd: path.dirname(runnerPath), windowsHide: true,
      shell: false, stdio: ['pipe', 'pipe', 'pipe'], env: { ...process.env, PYTHONUTF8: '1', PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' } });
    service = child;
    let spawnError = null;
    child.stdout.setEncoding('utf8'); child.stderr.setEncoding('utf8');
    child.stdout.on('data', chunk => { if (logStream) logStream.write(chunk); });
    child.stderr.on('data', chunk => { if (logStream) logStream.write(chunk); });
    child.stdin.on('error', () => {});
    child.once('error', error => { spawnError = error; log(`Service start error: ${error.message}`); });
    child.once('exit', (code, signal) => {
      log(`Service exited: code=${code}, signal=${signal || 'none'}`);
      if (service === child && !stopPromise && !quitting && status === 'ready') {
        serviceOrigin = ''; serviceUrl = '';
        void showSplash('error', '本地服务已退出。你的数据仍保存在此电脑，可重试或查看日志。');
      }
    });
    const started = Date.now();
    while (service === child && !spawnError && isLive(child) && !quitting && status === 'starting') {
      const url = await readyStatus(child);
      if (stopPromise || quitting || status !== 'starting' || service !== child) return false;
      if (url) {
        serviceUrl = url; serviceOrigin = new URL(url).origin;
        status = 'ready'; statusMessage = '本地服务运行正常';
        log('Local service is ready; opening embedded workspace.');
        if (mainWin && !mainWin.isDestroyed()) await mainWin.loadURL(url).catch(() => {});
        beginHealthMonitor(child);
        return true;
      }
      if (Date.now() - started > 180000) {
        await showSplash('error', '启动耗时较长。请查看日志；点击重试会安全重启本地服务。');
        return false;
      }
      statusMessage = Date.now() - started > 15000 ? '首次初始化可能需要一些时间，正在准备工作区' : '正在启动本地服务与任务运行环境';
      await sleep(500);
    }
    if (!stopPromise && !quitting && status === 'starting') await showSplash('error', '本地服务未能完成启动。请查看日志，然后重试。');
    return false;
  })();
  try { return await startPromise; } finally { startPromise = null; }
}

function beginHealthMonitor(child) {
  if (healthTimer) clearInterval(healthTimer);
  let failures = 0;
  let checking = false;
  healthTimer = setInterval(async () => {
    if (checking || status !== 'ready' || service !== child || !isLive(child)) return;
    checking = true;
    try {
      const healthy = await checkHealth(serviceUrl);
      failures = healthy ? 0 : failures + 1;
      statusMessage = healthy ? '本地服务运行正常' : '正在等待本地服务响应';
      if (failures >= 5) await showSplash('error', '本地服务暂未响应。可以查看日志，或安全重启服务。');
    } finally { checking = false; }
  }, 5000);
  healthTimer.unref();
}

function waitForExit(child, milliseconds) {
  if (!isLive(child)) return Promise.resolve(true);
  return new Promise(resolve => {
    const finish = result => { clearTimeout(timer); child.removeListener('exit', exited); resolve(result); };
    const exited = () => finish(true);
    const timer = setTimeout(() => finish(false), milliseconds);
    child.once('exit', exited);
  });
}

async function stopService(reason) {
  if (stopPromise) return stopPromise;
  stopPromise = (async () => {
    if (healthTimer) { clearInterval(healthTimer); healthTimer = null; }
    const child = service;
    await showSplash('stopping', reason === 'restart' ? '正在安全重启，稍后返回工作区' : '正在结束本地任务并保存状态');
    if (!isLive(child)) { service = null; serviceOrigin = ''; serviceUrl = ''; return true; }
    log(`Graceful service stop requested (${reason}).`);
    try { child.stdin.end('stop\n', 'utf8'); } catch { }
    const stopped = await waitForExit(child, 40000);
    if (!stopped && isLive(child)) {
      const response = await dialog.showMessageBox(mainWin, { type: 'warning', title: '等待本地服务停止',
        message: '服务尚未完成退出', detail: '强制结束会中断正在执行的任务。只会结束本应用启动的服务。',
        buttons: ['继续等待', '强制结束'], defaultId: 0, cancelId: 0, noLink: true });
      if (response.response !== 1) {
        await showSplash('error', '服务仍在结束任务。可继续等待，或再次关闭应用。');
        return false;
      }
      log('User explicitly confirmed forced termination of this owned service.');
      if (isLive(child)) child.kill('SIGKILL');
      if (!await waitForExit(child, 5000)) {
        await showSplash('error', '无法确认服务已停止，请查看日志后重试。');
        return false;
      }
    }
    if (service === child) service = null;
    serviceOrigin = ''; serviceUrl = '';
    return true;
  })();
  try { return await stopPromise; } finally { stopPromise = null; }
}

async function restartService() {
  if (restartPromise || quitPromise) return restartPromise;
  restartPromise = (async () => {
    if (!await stopService('restart')) return false;
    if (startPromise) await startPromise;
    return startService();
  })();
  try { return await restartPromise; } catch (error) { log(`Restart error: ${error.message}`); await showSplash('error', '重启未完成，请查看日志后重试。'); }
  finally { restartPromise = null; }
}

async function requestQuit() {
  if (quitting) return;
  if (quitPromise) return quitPromise;
  quitPromise = (async () => {
    if (!await stopService('quit')) return;
    quitting = true;
    if (logStream) logStream.end();
    if (options.smokeTest) app.exit(smokeExitCode);
    else app.quit();
  })();
  try { await quitPromise; } finally { quitPromise = null; }
}

async function runSmokeTest() {
  const output = path.resolve(options.smokeOutput || path.join(home, 'desktop-smoke'));
  const report = { desktop: true, runtime: process.versions.electron, home, url: serviceUrl, checks: {} };
  try {
    await fsp.mkdir(output, { recursive: true });
    await sleep(700);
    const prefs = mainWin.webContents.getLastWebPreferences();
    report.checks.sandbox = prefs.sandbox === true;
    report.checks.contextIsolation = prefs.contextIsolation === true;
    report.checks.noNodeIntegration = prefs.nodeIntegration === false;
    report.checks.webSecurity = prefs.webSecurity === true;
    report.renderer = await mainWin.webContents.executeJavaScript(`({ hasRequire: typeof require !== 'undefined', hasProcess: typeof process !== 'undefined', bridge: typeof window.harnessDesktop?.getInfo === 'function', title: document.title, url: location.href })`);
    report.checks.rendererBoundary = !report.renderer.hasRequire && !report.renderer.hasProcess && report.renderer.bridge;
    const capture = await mainWin.webContents.capturePage();
    await fsp.writeFile(path.join(output, 'desktop-login.png'), capture.toPNG());
    const beforeWindows = blockedWindows;
    await mainWin.webContents.executeJavaScript(`window.open('https://example.invalid/desktop-smoke'); void 0;`);
    await sleep(200);
    report.checks.externalPopupDenied = blockedWindows > beforeWindows;
    const beforeNavigation = blockedNavigations;
    await mainWin.webContents.executeJavaScript(`location.href = 'https://example.invalid/desktop-smoke'; void 0;`).catch(() => {});
    await sleep(200);
    report.checks.remoteNavigationDenied = blockedNavigations > beforeNavigation && servicePage(mainWin.webContents.getURL());
    report.checks.readyOwnedPid = !!await readyStatus(service);
    if (options.home) {
      const { runWorkspaceSmoke } = require('./smoke-workspace.cjs');
      report.workspace = await runWorkspaceSmoke({ window: mainWin, home, origin: serviceOrigin, output });
      Object.assign(report.checks, report.workspace.checks);
    } else report.checks.explicitSmokeHome = false;
    report.ok = Object.values(report.checks).every(Boolean);
  } catch (error) { report.ok = false; report.error = error.message; }
  await fsp.writeFile(path.join(output, 'desktop-smoke.json'), JSON.stringify(report, null, 2), 'utf8');
  log(`Desktop smoke test ${report.ok ? 'passed' : 'failed'}.`);
  smokeExitCode = report.ok ? 0 : 1;
  await requestQuit();
}

try {
  if (options.smokeTest && !options.home) throw new Error('--smoke-test 必须同时指定独立的 --home 测试数据目录。');
  fs.mkdirSync(desktopHome, { recursive: true });
  fs.mkdirSync(logsHome, { recursive: true });
  app.setName(APP_NAME);
  app.setPath('userData', desktopHome);
  app.setPath('sessionData', path.join(desktopHome, 'session'));
  fs.mkdirSync(app.getPath('sessionData'), { recursive: true });
  try { const saved = JSON.parse(fs.readFileSync(path.join(desktopHome, 'preferences.json'), 'utf8')); if (['light', 'dark'].includes(saved.theme)) theme = saved.theme; } catch { }
  nativeTheme.themeSource = theme;
  // Single-instance identity is derived by Electron from this per-home userData.
  if (!app.requestSingleInstanceLock({ home })) app.quit();
  else {
    app.on('second-instance', () => { if (mainWin && !mainWin.isDestroyed()) { if (mainWin.isMinimized()) mainWin.restore(); mainWin.show(); mainWin.focus(); } });
    app.on('before-quit', event => { if (!quitting) { event.preventDefault(); void requestQuit(); } });
    app.on('window-all-closed', () => { if (!quitting) void requestQuit(); });
    app.on('web-contents-created', (_event, contents) => {
      contents.on('will-attach-webview', event => event.preventDefault());
    });
    app.whenReady().then(async () => {
      app.setAppUserModelId('HarnessAgent.Desktop');
      installIpc(); buildMenu(); createWindow();
      const ready = await startService();
      if (!ready && options.smokeTest && !smokeStarted) {
        smokeStarted = true;
        smokeExitCode = 1;
        const output = path.resolve(options.smokeOutput || path.join(home, 'desktop-smoke'));
        await fsp.mkdir(output, { recursive: true });
        await fsp.writeFile(path.join(output, 'desktop-smoke.json'), JSON.stringify({ ok: false, desktop: true, error: 'Local service failed to become ready', state: status }, null, 2));
        await requestQuit();
      }
    }).catch(async error => {
      log(`Desktop initialization error: ${error.message}`);
      if (mainWin && !mainWin.isDestroyed()) await showSplash('error', '应用初始化未完成，请查看日志后重试。');
      else { dialog.showErrorBox(APP_NAME, error.message); quitting = true; app.quit(); }
    });
  }
} catch (error) { dialog.showErrorBox(APP_NAME, `无法打开本地数据目录。\n${error.message}`); app.exit(1); }
