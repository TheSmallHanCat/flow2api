/**
 * Flow2API reCAPTCHA Helper — Background Service Worker
 *
 * 职责：
 * 1. 管理 WebSocket 长连接（连接/重连/心跳）
 * 2. 管理后台标签页（自动创建并维护 labs.google 标签页）
 * 3. 接收服务端 solve 请求 → 注入脚本获取 token → 返回结果
 * 4. 日志记录（供 popup 查看排查问题）
 */

// ==================== 日志系统 ====================
const MAX_LOGS = 200;
let logs = [];

function addLog(level, message) {
  const entry = {
    time: new Date().toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' }),
    level,
    message,
  };
  logs.push(entry);
  if (logs.length > MAX_LOGS) logs = logs.slice(-MAX_LOGS);
  chrome.storage.local.set({ logs });
  const fn = level === 'ERROR' ? console.error : level === 'WARN' ? console.warn : console.log;
  fn(`[${entry.time}] [${level}] ${message}`);
}

// ==================== 状态 ====================
let ws = null;
// Docker 环境由 sed 注入，手动安装时为空（通过 popup 配置）
let wsUrl = '';
let authKey = '';
let isConnected = false;
let reconnectTimer = null;
let stats = { solved: 0, errors: 0, connected: false };
let captchaTabId = null;

// ==================== 配置加载 ====================
async function loadConfig() {
  const data = await chrome.storage.local.get(['wsUrl', 'authKey']);

  if (data.wsUrl) {
    wsUrl = data.wsUrl;
    authKey = data.authKey || '';
  } else if (wsUrl) {
    // 全局变量已有值（Docker 环境 sed 注入），写入 storage
    await chrome.storage.local.set({ wsUrl, authKey });
    addLog('INFO', `使用内置配置: ${wsUrl}`);
  }

  return { wsUrl, authKey };
}

// ==================== WebSocket 管理 ====================
async function connect() {
  if (ws && ws.readyState <= 1) return;

  const cfg = await loadConfig();
  if (!cfg.wsUrl) {
    addLog('WARN', '未配置 WebSocket 地址');
    updateStatus(false);
    return;
  }

  addLog('INFO', `正在连接: ${cfg.wsUrl}`);

  try {
    ws = new WebSocket(cfg.wsUrl);
  } catch (e) {
    addLog('ERROR', `WebSocket 创建失败: ${e.message || e}`);
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    addLog('SUCCESS', 'WebSocket 已连接');
    isConnected = true;
    updateStatus(true);

    if (authKey) {
      ws.send(JSON.stringify({ type: 'auth', key: authKey }));
      addLog('INFO', '已发送认证请求');
    }

    startHeartbeat();
    ensureCaptchaTab();
  };

  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      handleMessage(msg);
    } catch (e) {
      addLog('ERROR', `消息解析失败: ${e.message}`);
    }
  };

  ws.onclose = (event) => {
    addLog('WARN', `WebSocket 断开 (code=${event.code}, reason=${event.reason || '无'})`);
    isConnected = false;
    updateStatus(false);
    stopHeartbeat();
    scheduleReconnect();
  };

  ws.onerror = () => {
    addLog('ERROR', 'WebSocket 连接错误');
    isConnected = false;
    updateStatus(false);
  };
}

function disconnect() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  stopHeartbeat();
  if (ws) {
    ws.onclose = null;
    ws.close();
    ws = null;
  }
  isConnected = false;
  updateStatus(false);
  addLog('INFO', '已手动断开连接');
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  addLog('INFO', '5秒后自动重连...');
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, 5000);
}

// ==================== 心跳保活 ====================
let heartbeatInterval = null;

function startHeartbeat() {
  stopHeartbeat();
  heartbeatInterval = setInterval(() => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'ping' }));
    }
  }, 25000);
}

function stopHeartbeat() {
  if (heartbeatInterval) {
    clearInterval(heartbeatInterval);
    heartbeatInterval = null;
  }
}

// 使用 chrome.alarms 防止 service worker 休眠
chrome.alarms.create('keepAlive', { periodInMinutes: 0.4 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === 'keepAlive') {
    if (wsUrl && (!ws || ws.readyState > 1)) {
      connect();
    }
  }
});

// ==================== 后台标签页管理 ====================
async function ensureCaptchaTab() {
  if (captchaTabId !== null) {
    try {
      const tab = await chrome.tabs.get(captchaTabId);
      if (tab && tab.url && tab.url.includes('labs.google')) {
        return captchaTabId;
      }
    } catch (e) {
      captchaTabId = null;
    }
  }

  const tabs = await chrome.tabs.query({ url: 'https://labs.google/*' });
  if (tabs.length > 0) {
    captchaTabId = tabs[0].id;
    addLog('INFO', `复用已有标签页 (tabId=${captchaTabId})`);
    return captchaTabId;
  }

  const tab = await chrome.tabs.create({
    url: 'https://labs.google/fx',
    active: false,
  });
  captchaTabId = tab.id;
  addLog('INFO', `创建后台标签页 (tabId=${captchaTabId})`);

  return new Promise((resolve) => {
    const listener = (tabId, changeInfo) => {
      if (tabId === captchaTabId && changeInfo.status === 'complete') {
        chrome.tabs.onUpdated.removeListener(listener);
        addLog('SUCCESS', `标签页加载完成 (tabId=${captchaTabId})`);
        resolve(captchaTabId);
      }
    };
    chrome.tabs.onUpdated.addListener(listener);
  });
}

// ==================== 消息处理 ====================
async function handleMessage(msg) {
  if (msg.type === 'pong') return;

  if (msg.type === 'solve') {
    addLog('INFO', `收到 solve 请求: id=${msg.id}, action=${msg.action}`);
    const startTime = Date.now();
    try {
      const token = await solveCaptcha(msg.siteKey, msg.action);
      const elapsed = Date.now() - startTime;
      sendResult(msg.id, token, null);
      stats.solved++;
      addLog('SUCCESS', `Token 获取成功 (${elapsed}ms, 长度=${token ? token.length : 0})`);
    } catch (e) {
      const elapsed = Date.now() - startTime;
      sendResult(msg.id, null, e.message);
      stats.errors++;
      addLog('ERROR', `Token 获取失败 (${elapsed}ms): ${e.message}`);
    }
    updateStatus(isConnected);
  }

  if (msg.type === 'auth_ok') {
    addLog('SUCCESS', '认证成功');
  }

  if (msg.type === 'auth_fail') {
    addLog('ERROR', `认证失败: ${msg.error || '未知错误'}`);
  }
}

function sendResult(id, token, error) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'result', id, token, error }));
  }
}

// ==================== reCAPTCHA 求解 ====================
async function solveCaptcha(siteKey, action) {
  const tabId = await ensureCaptchaTab();

  addLog('INFO', `注入脚本 (tabId=${tabId}, action=${action})`);

  // world: 'MAIN' 才能访问页面的 window.grecaptcha
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    world: 'MAIN',
    func: executeCaptchaInPage,
    args: [siteKey, action],
  });

  if (!results || results.length === 0) {
    throw new Error('executeScript 无返回');
  }

  const result = results[0].result;
  if (result.error) {
    throw new Error(result.error);
  }

  return result.token;
}

// 注入到页面中执行的函数（运行在页面上下文 MAIN world）
async function executeCaptchaInPage(siteKey, action) {
  try {
    let attempts = 0;
    while (
      (!window.grecaptcha || !window.grecaptcha.enterprise || typeof window.grecaptcha.enterprise.execute !== 'function')
      && attempts < 100
    ) {
      await new Promise(r => setTimeout(r, 100));
      attempts++;
    }

    if (!window.grecaptcha || !window.grecaptcha.enterprise) {
      return { error: 'grecaptcha.enterprise 未加载 (等待超时 10s)' };
    }

    const token = await window.grecaptcha.enterprise.execute(siteKey, { action });
    return { token, error: null };
  } catch (e) {
    return { error: e.message || String(e) };
  }
}

// ==================== 状态同步 ====================
function updateStatus(connected) {
  stats.connected = connected;
  chrome.storage.local.set({ stats: { ...stats } });
  chrome.action.setBadgeText({ text: connected ? 'ON' : 'OFF' });
  chrome.action.setBadgeBackgroundColor({ color: connected ? '#22c55e' : '#ef4444' });
}

// ==================== popup 消息处理 ====================
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === 'getStatus') {
    sendResponse({ ...stats, wsUrl });
    return true;
  }
  if (msg.type === 'getLogs') {
    sendResponse({ logs });
    return true;
  }
  if (msg.type === 'clearLogs') {
    logs = [];
    chrome.storage.local.set({ logs: [] });
    sendResponse({ ok: true });
    return true;
  }
  if (msg.type === 'connect') {
    connect();
    sendResponse({ ok: true });
    return true;
  }
  if (msg.type === 'disconnect') {
    disconnect();
    sendResponse({ ok: true });
    return true;
  }
  if (msg.type === 'updateConfig') {
    chrome.storage.local.set({
      wsUrl: msg.wsUrl,
      authKey: msg.authKey,
    }).then(() => {
      wsUrl = msg.wsUrl;
      authKey = msg.authKey;
      disconnect();
      connect();
      sendResponse({ ok: true });
    });
    return true;
  }
});

// ==================== 启动 ====================
addLog('INFO', '扩展 Service Worker 启动');
loadConfig().then(() => {
  if (wsUrl) {
    addLog('INFO', `自动连接: ${wsUrl}`);
    connect();
  } else {
    addLog('WARN', '未配置 WebSocket 地址，等待用户配置');
  }
});
