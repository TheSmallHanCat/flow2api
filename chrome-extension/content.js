// Content script — labs.google 页面注入
// 用于确保 reCAPTCHA 脚本已加载，并保持页面活跃防止被冻结
(function() {
  console.log('[Flow2API Helper] Content script 已注入');

  // 页面保活机制（防止安卓锁屏后标签页 JS 引擎被冻结）
  // 通过 Web Lock API 请求一个持久锁，阻止浏览器冻结此标签页
  if (navigator.locks) {
    navigator.locks.request('flow2api_keepalive', { mode: 'exclusive' }, () => {
      // 永远不 resolve，保持锁持有
      return new Promise(() => {});
    }).catch(() => {});
  }

  // 备用方案：定时微任务防止 JS 引擎休眠
  let keepAliveTimer = setInterval(() => {
    // 空操作，仅维持 JS 定时器活跃
    void 0;
  }, 25000);

  // 页面卸载时清理
  window.addEventListener('beforeunload', () => {
    clearInterval(keepAliveTimer);
  });
})();
