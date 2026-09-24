'use strict';

const titles = { starting: '正在准备工作区', ready: '工作区已就绪', stopping: '正在保存并结束运行', stopped: '本地服务已停止', error: '暂时无法进入工作区' };
let updating = false;
async function refresh() {
  if (updating || !window.harnessDesktop) return;
  updating = true;
  try {
    const info = await window.harnessDesktop.getInfo();
    document.documentElement.dataset.theme = info.theme;
    document.getElementById('heading').textContent = titles[info.state] || titles.starting;
    document.getElementById('message').textContent = info.message;
    const actionable = info.state === 'error' || info.state === 'stopped';
    document.getElementById('actions').hidden = !actionable;
    document.getElementById('progress').hidden = actionable;
    document.getElementById('footer-note').textContent = info.state === 'stopping' ? '请稍候，正在安全结束本次运行' : '服务与数据保存在此电脑';
  } catch { document.getElementById('message').textContent = '正在连接桌面应用…'; }
  finally { updating = false; }
}
document.getElementById('retry').addEventListener('click', async () => {
  document.getElementById('retry').disabled = true;
  try { await window.harnessDesktop.restartService(); } finally { document.getElementById('retry').disabled = false; }
});
document.getElementById('logs').addEventListener('click', () => window.harnessDesktop.openLogs());
document.getElementById('data').addEventListener('click', () => window.harnessDesktop.openDataDirectory());
window.addEventListener('harness-desktop-theme', event => { document.documentElement.dataset.theme = event.detail.theme; });
void refresh();
setInterval(refresh, 600);
