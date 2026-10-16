const assert = require("node:assert/strict");
const test = require("node:test");
const A = require("../anchor_state.js");

test("登录按钮请求门防止重复提交", async () => {
  const gate = new A.RequestGate(); let calls = 0;
  const task = () => new Promise(resolve => setTimeout(() => resolve(++calls), 8));
  await Promise.all([gate.run("login", task), gate.run("login", task)]);
  assert.equal(calls, 1);
});

test("提醒ID全局去重并支持未读筛选", () => {
  const store = new A.AlertStore();
  store.merge([{ id: 1, read: false }, { id: 1, read: false }, { id: 2, read: true }]);
  assert.equal(store.list().length, 2); assert.equal(store.unreadCount(), 1);
  store.markRead(1); assert.equal(store.list(true).length, 0);
});

test("WebSocket重连采用有上限的指数退避", () => {
  assert.deepEqual([0, 1, 2, 3].map(value => A.retryDelay(value)), [1000, 2000, 4000, 8000]);
  assert.equal(A.retryDelay(99), 30000);
});

test("三类违规提醒中文标签和严重度不同", () => {
  assert.equal(A.alertMeta("compliance", "error").label, "合规用语");
  assert.equal(A.alertMeta("product_error", "warn").label, "产品纠错");
  assert.equal(A.alertMeta("emotion_mismatch", "info").label, "情绪语境");
  assert.equal(A.alertMeta("compliance", "error").severityLabel, "严重");
});

test("无效音频隐藏播放器并保留原因", () => {
  assert.equal(A.mediaState({ available: false, reason: "音频合并失败" }).playable, false);
  assert.equal(A.mediaState({ available: false, reason: "音频合并失败" }).reason, "音频合并失败");
  assert.equal(A.mediaState({ available: true, url: "/media" }).playable, true);
});

test("公开错误隐藏密钥、路径和堆栈", () => {
  assert.doesNotMatch(A.publicError("api_key=secret C:\\srv\\app\\x.py"), /secret|srv/);
  assert.equal(A.publicError('Traceback File "x.py", line 2'), "操作失败，请稍后重试");
});

test("桌面通知按场次和类型节流，严重提醒按事件去重", () => {
  const throttle = new A.NotificationThrottle(60000);
  assert.equal(throttle.allow({ id: 1, session_id: 8, kind: "compliance", severity: "warn" }, 1000), true);
  assert.equal(throttle.allow({ id: 2, session_id: 8, kind: "compliance", severity: "warn" }, 2000), false);
  assert.equal(throttle.allow({ id: 3, session_id: 8, kind: "product_error", severity: "warn" }, 2000), true);
  assert.equal(throttle.allow({ id: 4, session_id: 8, kind: "compliance", severity: "error" }, 3000), true);
  assert.equal(throttle.allow({ id: 4, session_id: 8, kind: "compliance", severity: "error" }, 4000), false);
  assert.equal(throttle.allow({ id: 5, session_id: 9, kind: "compliance", severity: "warn" }, 5000), true);
});
