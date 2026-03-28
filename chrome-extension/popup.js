// popup.js — 弹出窗口脚本
document.addEventListener('DOMContentLoaded', async () => {
  const $ = id => document.getElementById(id);

  // 加载配置
  const data = await chrome.storage.local.get(['wsUrl', 'authKey', 'proxyScheme', 'proxyHost', 'proxyPort', 'proxyUser', 'proxyPass']);
  $('wsUrl').value = data.wsUrl || '';
  $('authKey').value = data.authKey || '';
  $('proxyScheme').value = data.proxyScheme || '';
  $('proxyHost').value = data.proxyHost || '';
  $('proxyPort').value = data.proxyPort || '';
  $('proxyUser').value = data.proxyUser || '';
  $('proxyPass').value = data.proxyPass || '';

  // 刷新状态
  function refresh() {
    chrome.runtime.sendMessage({ type: 'getStatus' }, (resp) => {
      if (!resp) return;
      $('statSolved').textContent = resp.solved || 0;
      $('statErrors').textContent = resp.errors || 0;
      $('statConnected').textContent = resp.connected ? '在线' : '离线';
      const dot = $('statusDot');
      dot.className = 'status-dot ' + (resp.connected ? 'on' : 'off');
    });
  }
  refresh();
  setInterval(refresh, 2000);

  // 刷新日志
  function refreshLogs() {
    chrome.runtime.sendMessage({ type: 'getLogs' }, (resp) => {
      if (!resp || !resp.logs) return;
      const container = $('logContainer');
      container.innerHTML = resp.logs.slice(-50).map(l =>
        `<div class="log-entry"><span class="log-time">${l.time}</span> <span class="log-${l.level}">[${l.level}]</span> ${l.message}</div>`
      ).join('');
      container.scrollTop = container.scrollHeight;
    });
  }
  refreshLogs();
  setInterval(refreshLogs, 3000);

  // 保存配置（含代理）
  $('btnSave').addEventListener('click', () => {
    chrome.runtime.sendMessage({
      type: 'updateConfig',
      wsUrl: $('wsUrl').value.trim(),
      authKey: $('authKey').value.trim(),
      proxyScheme: $('proxyScheme').value,
      proxyHost: $('proxyHost').value.trim(),
      proxyPort: $('proxyPort').value.trim(),
      proxyUser: $('proxyUser').value.trim(),
      proxyPass: $('proxyPass').value.trim(),
    });
  });

  // 连接/断开
  $('btnConnect').addEventListener('click', () => {
    chrome.runtime.sendMessage({ type: 'connect' });
  });
  $('btnDisconnect').addEventListener('click', () => {
    chrome.runtime.sendMessage({ type: 'disconnect' });
  });

  // 清空日志
  $('btnClearLogs').addEventListener('click', () => {
    chrome.runtime.sendMessage({ type: 'clearLogs' });
    $('logContainer').innerHTML = '';
  });
});
