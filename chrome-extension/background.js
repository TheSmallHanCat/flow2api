/**
 * Flow2API reCAPTCHA Helper — Background Service Worker
 *
 * 职责：
 * 1. 管理 WebSocket 长连接（连接/重连/心跳）
 * 2. 管理后台标签页（自动创建并维护 labs.google 标签页）
 * 3. 接收服务端 solve 请求 → 注入脚本获取 token → 返回结果
 * 4. 日志记录（供 popup 查看排查问题）
 */

/* 日志系统 */
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

/* 状态 */
let ws = null;
// Docker 环境由 sed 注入，手动安装时为空（通过 popup 配置）
let wsUrl = '';
let authKey = '';
let isConnected = false;
let reconnectTimer = null;
let stats = { solved: 0, errors: 0, connected: false };

// 标签页预热池：每个 tab 在后台积累行为信号，solve 时选取最"成熟"的
const TAB_POOL_SIZE = 3;
let tabPool = []; // [{ id, createdAt, solveCount, refreshing }]

// 标签页刷新策略：每个 tab 累计 N 次 solve 后刷新
let tabRefreshInterval = 2;

// solve 请求排队锁：确保同一时刻只有一个 solve 在执行
let solveQueue = Promise.resolve();

/* 配置加载 */
async function loadConfig() {
  const data = await chrome.storage.local.get(['wsUrl', 'authKey', 'proxyScheme', 'proxyHost', 'proxyPort', 'proxyUser', 'proxyPass']);

  if (data.wsUrl) {
    wsUrl = data.wsUrl;
    authKey = data.authKey || '';
  } else if (wsUrl) {
    // 全局变量已有值（Docker 环境 sed 注入），写入 storage
    await chrome.storage.local.set({ wsUrl, authKey });
    addLog('INFO', `使用内置配置: ${wsUrl}`);
  }

  // 应用代理配置
  if (data.proxyScheme && data.proxyHost && data.proxyPort) {
    applyProxy(data.proxyScheme, data.proxyHost, data.proxyPort, data.proxyUser, data.proxyPass);
  }

  return { wsUrl, authKey };
}

/* 代理管理 */
function applyProxy(scheme, host, port, user, pass) {
  if (!scheme || !host || !port) {
    clearProxy();
    return;
  }

  // 自动将 WS 服务器地址加入绕过列表，确保控制通道直连
  const bypassList = ['localhost', '127.0.0.1', '<local>'];
  try {
    if (wsUrl) {
      const wsHost = new URL(wsUrl.replace('ws://', 'http://').replace('wss://', 'https://')).hostname;
      if (wsHost && !bypassList.includes(wsHost)) {
        bypassList.push(wsHost);
      }
    }
  } catch (e) { /* 解析失败忽略 */ }
  // Docker 内网地址也绕过
  bypassList.push('10.*', '172.16.*', '172.17.*', '172.18.*', '192.168.*');

  const config = {
    mode: 'fixed_servers',
    rules: {
      singleProxy: {
        scheme: scheme === 'socks5' ? 'socks5' : 'http',
        host: host,
        port: parseInt(port),
      },
      bypassList: bypassList,
    }
  };

  chrome.proxy.settings.set({ value: config, scope: 'regular' }, () => {
    addLog('SUCCESS', `代理已设置: ${scheme}://${host}:${port}, 绕过: ${bypassList.join(', ')}`);
  });

  // 需要认证的代理
  if (user && pass) {
    chrome.webRequest?.onAuthRequired?.addListener?.(
      (details, callback) => {
        callback({ authCredentials: { username: user, password: pass } });
      },
      { urls: ['<all_urls>'] },
      ['blocking']
    );
  }
}

function clearProxy() {
  chrome.proxy.settings.clear({ scope: 'regular' }, () => {
    addLog('INFO', '代理已清除');
  });
}

/* WebSocket 管理 */
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
    initTabPool();
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

/* 心跳保活 */
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
chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === 'keepAlive') {
    if (wsUrl && (!ws || ws.readyState > 1)) {
      connect();
    }
    // 确保标签页池已初始化
    if (tabPool.length < TAB_POOL_SIZE) {
      await initTabPool();
    }
  }
});

/* 标签页预热池管理 */

// 等待标签页加载完成
function waitTabLoad(tabId, timeout = 15000) {
  return new Promise((resolve) => {
    const listener = (tid, changeInfo) => {
      if (tid === tabId && changeInfo.status === 'complete') {
        chrome.tabs.onUpdated.removeListener(listener);
        resolve(true);
      }
    };
    chrome.tabs.onUpdated.addListener(listener);
    setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      resolve(false);
    }, timeout);
  });
}

// 创建一个新的池标签页
async function createPoolTab() {
  const tab = await chrome.tabs.create({ url: 'https://labs.google/fx', active: false });
  await waitTabLoad(tab.id);
  const entry = { id: tab.id, createdAt: Date.now(), solveCount: 0, refreshing: false };
  addLog('INFO', `创建池标签页 (tabId=${tab.id})`);
  return entry;
}

// 初始化标签页池
async function initTabPool() {
  // 复用已有的 labs.google 标签页
  const existing = await chrome.tabs.query({ url: 'https://labs.google/*' });
  for (const tab of existing) {
    if (tabPool.length >= TAB_POOL_SIZE) break;
    if (!tabPool.find(t => t.id === tab.id)) {
      tabPool.push({ id: tab.id, createdAt: Date.now(), solveCount: 0, refreshing: false });
      addLog('INFO', `复用已有标签页入池 (tabId=${tab.id})`);
    }
  }

  // 补齐到目标数量
  while (tabPool.length < TAB_POOL_SIZE) {
    try {
      const entry = await createPoolTab();
      tabPool.push(entry);
    } catch (e) {
      addLog('WARN', `创建池标签页失败: ${e.message}`);
      break;
    }
  }

  addLog('INFO', `标签页池就绪: ${tabPool.length} 个`);
}

// 监听标签页关闭事件，自动从池中移除并补充
chrome.tabs.onRemoved.addListener((tabId) => {
  const idx = tabPool.findIndex(t => t.id === tabId);
  if (idx === -1) return;
  tabPool.splice(idx, 1);
  addLog('WARN', `池标签页被关闭 (tabId=${tabId})，剩余 ${tabPool.length} 个，补充中...`);
  // 异步补充新标签页
  createPoolTab().then(entry => {
    tabPool.push(entry);
    addLog('INFO', `池标签页已补充 (tabId=${entry.id})，当前 ${tabPool.length} 个`);
  }).catch(e => {
    addLog('ERROR', `补充标签页失败: ${e.message}`);
  });
});

// 从池中选取标签页：优先选预热时间最长且未在刷新的
function pickTab() {
  const available = tabPool.filter(t => !t.refreshing);
  if (available.length === 0) return null;

  // 按预热时长排序（createdAt 最小 = 预热最久），加随机因子
  available.sort((a, b) => {
    const ageA = Date.now() - a.createdAt;
    const ageB = Date.now() - b.createdAt;
    // 随机扰动 ±30%，避免总是选同一个
    return (ageB * (0.7 + Math.random() * 0.6)) - (ageA * (0.7 + Math.random() * 0.6));
  });

  return available[0];
}

// 异步刷新一个池标签页（不阻塞当前 solve）
async function refreshPoolTab(entry) {
  if (entry.refreshing) return;
  entry.refreshing = true;

  try {
    // 验证标签页是否还存在
    await chrome.tabs.get(entry.id);
    await chrome.tabs.reload(entry.id);
    await waitTabLoad(entry.id);
    entry.createdAt = Date.now();
    entry.solveCount = 0;
    addLog('SUCCESS', `池标签页刷新完成 (tabId=${entry.id})`);
  } catch (e) {
    // 标签页已关闭，从池中移除并创建新的
    addLog('WARN', `池标签页 ${entry.id} 失效，替换...`);
    const idx = tabPool.indexOf(entry);
    if (idx !== -1) tabPool.splice(idx, 1);
    try {
      const newEntry = await createPoolTab();
      tabPool.push(newEntry);
    } catch (err) {
      addLog('ERROR', `替换标签页失败: ${err.message}`);
    }
  } finally {
    entry.refreshing = false;
  }
}

/* 串行 solve 处理 */
async function processSolve(msg) {
  const startTime = Date.now();

  // 确保标签页池已初始化
  if (tabPool.length === 0) {
    await initTabPool();
  }

  // 从池中选取预热最充分的标签页
  let tab = pickTab();
  if (!tab) {
    sendResult(msg.id, null, '无可用标签页');
    stats.errors++;
    updateStatus(isConnected);
    return;
  }

  // 验证标签页是否还存在（防止 onRemoved 还没来得及触发）
  try {
    await chrome.tabs.get(tab.id);
  } catch (e) {
    addLog('WARN', `标签页 ${tab.id} 不存在，移除并重试`);
    const idx = tabPool.indexOf(tab);
    if (idx !== -1) tabPool.splice(idx, 1);
    // 尝试补充并重新选取
    try { const entry = await createPoolTab(); tabPool.push(entry); } catch (_) {}
    tab = pickTab();
    if (!tab) {
      sendResult(msg.id, null, '无可用标签页');
      stats.errors++;
      updateStatus(isConnected);
      return;
    }
  }

  try {
    const token = await solveCaptcha(tab.id, msg.siteKey, msg.action);
    const elapsed = Date.now() - startTime;
    sendResult(msg.id, token, null);
    stats.solved++;
    addLog('SUCCESS', `Token 获取成功 (tabId=${tab.id}, ${elapsed}ms, 长度=${token ? token.length : 0})`);
  } catch (e) {
    const elapsed = Date.now() - startTime;
    sendResult(msg.id, null, e.message);
    stats.errors++;
    addLog('ERROR', `Token 获取失败 (tabId=${tab.id}, ${elapsed}ms): ${e.message}`);
  }
  updateStatus(isConnected);

  // solve 完成后更新计数，达到阈值则异步刷新（不阻塞后续请求）
  tab.solveCount++;
  if (tab.solveCount >= tabRefreshInterval) {
    addLog('INFO', `tabId=${tab.id} 已 solve ${tab.solveCount} 次，异步刷新...`);
    refreshPoolTab(tab); // 不 await，后台刷新
  }
}

/* 消息处理 */
async function handleMessage(msg) {
  if (msg.type === 'pong') return;

  if (msg.type === 'solve') {
    addLog('INFO', `收到 solve 请求: id=${msg.id}, action=${msg.action}`);
    // 排队串行执行，避免并发 execute 冲突
    solveQueue = solveQueue.then(() => processSolve(msg));
  }

  if (msg.type === 'auth_ok') {
    addLog('SUCCESS', '认证成功');
  }

  if (msg.type === 'auth_fail') {
    addLog('ERROR', `认证失败: ${msg.error || '未知错误'}`);
  }

  // 后端下发配置更新
  if (msg.type === 'config') {
    if (typeof msg.refreshInterval === 'number' && msg.refreshInterval >= 1) {
      tabRefreshInterval = msg.refreshInterval;
      addLog('INFO', `收到后端配置: 刷新间隔=${tabRefreshInterval}`);
    }
  }
}

function sendResult(id, token, error) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'result', id, token, error }));
  }
}

/* reCAPTCHA 求解 */
async function solveCaptcha(tabId, siteKey, action) {
  addLog('INFO', `注入脚本 (tabId=${tabId}, action=${action})`);

  // 唤醒标签页（安卓锁屏后标签页 JS 引擎可能被冻结）
  try {
    await chrome.tabs.update(tabId, { active: true });
  } catch (e) { /* 忽略 */ }

  // 检测标签页是否被重定向到非目标页面（如 Google 登录页）
  try {
    const tab = await chrome.tabs.get(tabId);
    if (tab.url && !tab.url.startsWith('https://labs.google/')) {
      addLog('WARN', `标签页被重定向到 ${tab.url}，重新导航到目标页面`);
      await chrome.tabs.update(tabId, { url: 'https://labs.google/fx' });
      // 等待页面加载完成
      await new Promise((resolve) => {
        const listener = (tid, info) => {
          if (tid === tabId && info.status === 'complete') {
            chrome.tabs.onUpdated.removeListener(listener);
            resolve();
          }
        };
        chrome.tabs.onUpdated.addListener(listener);
        // 超时保护
        setTimeout(() => { chrome.tabs.onUpdated.removeListener(listener); resolve(); }, 15000);
      });
      // 等待页面 reCAPTCHA 脚本加载
      await new Promise(r => setTimeout(r, 3000));
    }
  } catch (e) {
    addLog('WARN', `检测标签页 URL 失败: ${e.message}`);
  }

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

/* 状态同步 */
function updateStatus(connected) {
  stats.connected = connected;
  chrome.storage.local.set({ stats: { ...stats } });
  chrome.action.setBadgeText({ text: connected ? 'ON' : 'OFF' });
  chrome.action.setBadgeBackgroundColor({ color: connected ? '#22c55e' : '#ef4444' });
}

/* popup 消息处理 */
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
      proxyScheme: msg.proxyScheme || '',
      proxyHost: msg.proxyHost || '',
      proxyPort: msg.proxyPort || '',
      proxyUser: msg.proxyUser || '',
      proxyPass: msg.proxyPass || '',
    }).then(() => {
      wsUrl = msg.wsUrl;
      authKey = msg.authKey;

      // 应用或清除代理
      if (msg.proxyScheme && msg.proxyHost && msg.proxyPort) {
        applyProxy(msg.proxyScheme, msg.proxyHost, msg.proxyPort, msg.proxyUser, msg.proxyPass);
      } else {
        clearProxy();
      }

      disconnect();
      connect();
      sendResponse({ ok: true });
    });
    return true;
  }
});

/* 启动 */
addLog('INFO', '扩展 Service Worker 启动');
loadConfig().then(() => {
  if (wsUrl) {
    addLog('INFO', `自动连接: ${wsUrl}`);
    connect();
  } else {
    addLog('WARN', '未配置 WebSocket 地址，等待用户配置');
  }
});
