'use strict';

// Called only by main.cjs --smoke-test with an explicit disposable --home.
// The test logs in through Chromium's own session; no credentials enter the
// renderer, screenshots or report, and no model provider is contacted.
const fs = require('node:fs/promises');
const path = require('node:path');
const { Menu, clipboard } = require('electron');

function sleep(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }
function requireCheck(condition, message) { if (!condition) throw new Error(message); }

async function waitFor(contents, expression, description, timeout = 20000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await contents.executeJavaScript(expression).catch(() => false)) return;
    await sleep(100);
  }
  throw new Error(`Timed out waiting for ${description}`);
}

async function runWorkspaceSmoke({ window, home, origin, output }) {
  const contents = window.webContents;
  const originalBackgroundThrottling = contents.getLastWebPreferences().backgroundThrottling !== false;
  contents.setBackgroundThrottling(false);
  const report = { checks: {}, screenshots: [], pageErrors: [], pages: {}, providerCalls: 0 };
  let username, password;
  const onConsole = (...args) => {
    const details = args[0];
    const level = details?.level ?? args[1];
    if (level !== 'error' && level !== 3) return;
    const message = String(details?.message ?? args[2] ?? '').slice(0, 1600);
    // Authentication values are only used by session.fetch, but also redact
    // them defensively if a future page starts logging its request payload.
    const redacted = [username, password].filter(Boolean).reduce((text, value) => text.split(value).join('[redacted]'), message);
    if (report.pageErrors.length < 30) report.pageErrors.push(redacted);
  };
  contents.on('console-message', onConsole);
  try {
    const parsedOrigin = new URL(origin);
    requireCheck(parsedOrigin.protocol === 'http:' && parsedOrigin.hostname === '127.0.0.1' && parsedOrigin.port, 'Smoke target must be the owned loopback service');
    const environment = await fs.readFile(path.join(home, '.env'), 'utf8');
    username = environment.match(/^ROOT_USERNAME\s*=\s*(.*)$/m)?.[1]?.trim();
    password = environment.match(/^ROOT_PASSWORD\s*=\s*(.*)$/m)?.[1]?.trim();
    requireCheck(username && password, 'Disposable smoke home has no initial credentials');
    await contents.loadURL(`${origin}/login`);
    await waitFor(contents, "Boolean(document.querySelector('.desktop-titlebar') && document.getElementById('login-btn'))", 'desktop login');

    const login = await contents.session.fetch(`${origin}/api/v1/auth/login`, {
      method: 'POST', credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    requireCheck(login.ok && (await login.json()).role === 'root', 'Initial login through Chromium session failed');
    const cookies = await contents.session.cookies.get({ url: origin });
    const cookie = cookies.find(item => item.name === 'harness_local_session');
    requireCheck(cookie && cookie.httpOnly && cookie.session, 'Expected HttpOnly session cookie was not established');
    report.checks.chromiumSessionLogin = true;
    report.checks.httpOnlySessionCookie = true;
    // Do not include cookie values, initial password or the .env content.

    async function setNativeTheme(theme) {
      const label = theme === 'dark' ? '深色外观' : '浅色外观';
      const menu = Menu.getApplicationMenu();
      const item = menu?.items.find(value => value.label === '视图')?.submenu?.items.find(value => value.label === label);
      requireCheck(item && typeof item.click === 'function', 'Native theme menu item is missing');
      item.click(item, window);
      await waitFor(contents, `document.documentElement.dataset.theme === ${JSON.stringify(theme)}`, `native ${theme} theme`);
      await waitFor(contents, `(() => {
        const color = getComputedStyle(document.body).backgroundColor.match(/\\d+/g)?.map(Number) || [];
        return color.length >= 3 && ${theme === 'dark' ? 'Math.max(...color.slice(0,3)) < 100' : 'Math.min(...color.slice(0,3)) > 180'};
      })()`, `visible ${theme} palette`);
      await waitFor(contents, `fetch('/api/v1/users/me/preferences', {credentials: 'same-origin'}).then(async response => response.ok && (await response.json()).theme === ${JSON.stringify(theme)})`, `saved ${theme} preference`);
      await waitFor(contents, `(() => {
        const appearance = document.getElementById('personal-theme');
        return !document.body.classList.contains('admin-page') || appearance?.value === ${JSON.stringify(theme)};
      })()`, `admin ${theme} form synchronization`);
      await waitFor(contents, `(() => {
        if (!document.body.classList.contains('chat-page')) return true;
        const welcome = document.getElementById('welcome');
        return welcome && welcome.getBoundingClientRect().height > 0 && getComputedStyle(welcome).visibility !== 'hidden' && Number(getComputedStyle(welcome).opacity) > .95 && Boolean(document.getElementById('welcome-title')?.textContent.trim());
      })()`, 'visible workspace welcome');
      await sleep(400);
    }

    async function capture(name) {
      // Hidden smoke windows otherwise capture a stale throttled frame even
      // after their DOM reports initialization/theme completion.
      await contents.executeJavaScript('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))');
      const image = await contents.capturePage();
      requireCheck(!image.isEmpty(), `Empty screenshot: ${name}`);
      const filename = `${name}.png`;
      await fs.writeFile(path.join(output, filename), image.toPNG());
      report.screenshots.push(filename);
    }

    for (const [page, route] of [['workspace', '/'], ['admin', '/admin']]) {
      await contents.loadURL(`${origin}${route}${page === 'admin' ? '#preferences' : ''}`);
      const ready = page === 'workspace'
        ? "document.body.classList.contains('chat-page') && !document.body.classList.contains('is-booting') && Boolean(document.getElementById('query')) && document.getElementById('account-name')?.textContent.trim() === 'root' && !document.querySelector('#history-panel .sidebar-loading')"
        : "document.body.classList.contains('admin-page') && !document.body.classList.contains('auth-pending') && Boolean(document.getElementById('personal-theme'))";
      await waitFor(contents, ready, `${page} ready`);
      report.pages[page] = await contents.executeJavaScript(`(() => ({
        path: location.pathname,
        desktop: document.documentElement.dataset.desktop === 'true',
        toolbarCount: document.querySelectorAll('.desktop-titlebar').length,
        fatalErrors: [...document.querySelectorAll('.fatal-state')].filter(el => el.getBoundingClientRect().height > 0).length,
        bodyWidth: document.body.scrollWidth,
        viewportWidth: innerWidth,
        userReady: typeof Auth !== 'undefined' && Auth.role() === 'root'
      }))()`);
      const state = report.pages[page];
      requireCheck(state.path === route && state.desktop && state.toolbarCount === 1 && !state.fatalErrors && state.userReady, `${page} did not initialize normally`);
      requireCheck(state.bodyWidth <= state.viewportWidth + 2, `${page} has horizontal page overflow`);
      report.checks[`${page}AuthenticatedReady`] = true;
      for (const theme of ['light', 'dark']) {
        await setNativeTheme(theme);
        await capture(`desktop-${page}-${theme}`);
      }
      if (page === 'workspace') {
        const previousBounds = window.getBounds();
        try {
          window.setSize(900, 600);
          await waitFor(contents, 'innerWidth >= 880 && innerWidth <= 904 && innerHeight >= 550 && innerHeight <= 610', 'minimum supported window size');
          await setNativeTheme('light');
          const minimum = await contents.executeJavaScript(`(() => {
            const rectangle = selector => { const item = document.querySelector(selector); const box = item?.getBoundingClientRect(); return box ? {left:box.left, top:box.top, right:box.right, bottom:box.bottom, width:box.width, height:box.height} : null; };
            return {width:innerWidth, height:innerHeight, scrollWidth:document.body.scrollWidth,
              composer:rectangle('.composer'), input:rectangle('#query'), account:rectangle('.sidebar-account'), titlebar:rectangle('.desktop-titlebar')};
          })()`);
          report.pages.minimumWorkspace = minimum;
          report.pages.minimumWorkspace.outerBounds = window.getBounds();
          requireCheck(minimum.scrollWidth <= minimum.width + 2, 'Minimum workspace has horizontal overflow');
          for (const item of [minimum.composer, minimum.input, minimum.account, minimum.titlebar]) {
            requireCheck(item && item.width > 0 && item.height > 0 && item.left >= -1 && item.right <= minimum.width + 1 && item.top >= -1 && item.bottom <= minimum.height + 1, 'Minimum workspace has clipped essential controls');
          }
          await capture('desktop-workspace-minimum-light');
          report.checks.minimumWindowEssentialControlsVisible = true;
        } finally {
          window.setBounds(previousBounds);
        }
      }
    }
    report.checks.nativeThemeMenuUpdatesRenderer = true;

    // Exercise the same blob/anchor path used by authenticated attachments.
    // Only this test's one owned download gets a predetermined test destination;
    // the production download/navigation policy remains installed unchanged.
    const downloadText = 'Harness Agent isolated blob attachment acceptance\n';
    const downloadName = `desktop-blob-download-${Date.now()}.txt`;
    const downloadPath = path.join(output, downloadName);
    let downloadListener;
    let downloadTimer;
    const downloaded = new Promise((resolve, reject) => {
      downloadTimer = setTimeout(() => reject(new Error('Blob attachment download timed out')), 15000);
      downloadListener = (event, item, sender) => {
        if (sender !== contents || item.getFilename() !== downloadName || !item.getURL().startsWith(`blob:${origin}/`)) return;
        item.setSavePath(downloadPath);
        item.once('done', (_event, state) => {
          if (state === 'completed') resolve();
          else reject(new Error(`Blob attachment download ${state}`));
        });
      };
      contents.session.on('will-download', downloadListener);
    });
    try {
      await contents.executeJavaScript(`(() => {
        const url = URL.createObjectURL(new Blob([${JSON.stringify(downloadText)}], {type: 'text/plain;charset=utf-8'}));
        const anchor = document.createElement('a');
        anchor.href = url; anchor.download = ${JSON.stringify(downloadName)};
        document.body.appendChild(anchor); anchor.click(); anchor.remove();
        setTimeout(() => URL.revokeObjectURL(url), 20000);
      })()`, true);
      await downloaded;
      requireCheck(await fs.readFile(downloadPath, 'utf8') === downloadText, 'Saved blob attachment content differs');
      report.download = { file: downloadName, bytes: Buffer.byteLength(downloadText) };
      report.checks.blobAttachmentDownloaded = true;
    } finally {
      clearTimeout(downloadTimer);
      contents.session.removeListener('will-download', downloadListener);
    }

    // Never discard an image/custom clipboard format to test a copy button.
    const clipboardItems = await clipboard.read();
    const clipboardFormats = [...new Set(clipboardItems.flatMap(item => item.types))];
    if (clipboardFormats.every(format => format === 'text/plain')) {
      const previousText = await clipboard.readText();
      const wasVisible = window.isVisible();
      try {
      // Chromium requires a focused document independently of the permission
      // handler. Focus only this disposable smoke window, then restore hidden
      // mode; never relax production permissions to accommodate a hidden test.
      if (!wasVisible) window.show();
      window.focus(); contents.focus();
      await waitFor(contents, 'document.hasFocus()', 'clipboard user-action focus', 5000);
      const probe = 'Harness standalone clipboard acceptance';
      const copied = await contents.executeJavaScript(`(async () => {
        const capabilities = {secureContext: window.isSecureContext, clipboardAvailable: Boolean(navigator.clipboard), focused: document.hasFocus()};
        try { await copyText(${JSON.stringify(probe)}); return {ok: true, ...capabilities}; }
        catch (error) { return {ok: false, error: error.name, message: error.message, ...capabilities}; }
      })()`, true);
      // Chromium and the main process can observe the native clipboard at
      // different times. Verify the completed copy, allowing only a bounded
      // native commit delay; never report the user's clipboard contents.
      let observedText, readCount = 0;
      const readStarted = Date.now();
      do {
        observedText = await clipboard.readText();
        readCount += 1;
        if (observedText === probe || !copied.ok) break;
        await sleep(50);
      } while (Date.now() - readStarted < 1500);
      report.clipboard = {
        ok: copied.ok && observedText === probe,
        error: copied.error || null, message: copied.message || null, focusedProbe: true,
        secureContext: copied.secureContext, clipboardAvailable: copied.clipboardAvailable,
        focused: copied.focused, readType: typeof observedText,
        readLength: typeof observedText === 'string' ? observedText.length : null,
        expectedLength: probe.length, readCount, readDelayMs: Date.now() - readStarted,
      };
      if (copied.ok && observedText !== probe) {
        const textItem = (await clipboard.read()).find(item => item.types.includes('text/plain'));
        const textPayload = textItem ? await textItem.getType('text/plain') : null;
        const itemText = textPayload && typeof textPayload.text === 'function' ? await textPayload.text() : null;
        report.clipboard.itemRead = { available: Boolean(textItem), payloadType: typeof textPayload,
          textType: typeof itemText, textLength: typeof itemText === 'string' ? itemText.length : null,
          equalsProbe: itemText === probe };
      }
      report.checks.existingCopyAction = report.clipboard.ok;
      } finally {
        if (clipboardFormats.length) await clipboard.writeText(previousText);
        else clipboard.clear();
        if (!wasVisible) window.hide();
      }
    } else {
      report.clipboard = { skipped: true, reason: 'Preserved existing non-text clipboard formats' };
    }

    // Cookie must also be honored by the renderer after navigation, not merely
    // by a privileged session.fetch request in the main process.
    report.checks.rendererSessionRequest = await contents.executeJavaScript("fetch('/api/v1/auth/me', {credentials: 'same-origin'}).then(async response => response.ok && (await response.json()).role === 'root')");
    const logout = await contents.session.fetch(`${origin}/api/v1/auth/logout`, { method: 'POST', credentials: 'include' });
    requireCheck(logout.status === 204, 'Logout failed');
    await contents.loadURL(`${origin}/`);
    await waitFor(contents, "location.pathname === '/login' && Boolean(document.getElementById('login-btn'))", 'logout redirect');
    report.checks.logoutRevokesSession = true;
    report.checks.noPageErrors = report.pageErrors.length === 0;
    report.ok = Object.values(report.checks).every(Boolean);
  } catch (error) {
    report.ok = false;
    report.error = { name: error.name, message: error.message };
    report.checks.workspaceSmokeCompleted = false;
  } finally {
    contents.removeListener('console-message', onConsole);
    contents.setBackgroundThrottling(originalBackgroundThrottling);
    username = undefined; password = undefined;
  }
  return report;
}

module.exports = { runWorkspaceSmoke };
