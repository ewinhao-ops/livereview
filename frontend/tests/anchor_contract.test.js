const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const anchor = fs.readFileSync(path.join(__dirname, "..", "anchor.js"), "utf8");
const admin = fs.readFileSync(path.join(__dirname, "..", "app.js"), "utf8");

test("401清空页面并跳转主播登录态", () => {
  assert.match(anchor, /response\.status === 401/);
  assert.match(anchor, /showLogin\("登录会话已过期，请重新登录"\)/);
  assert.match(anchor, /alertStore\.clear\(\)/);
});

test("重连成功补查未读且提醒按ID去重", () => {
  assert.match(anchor, /if \(reconnected\) await refreshAlerts/);
  assert.match(anchor, /alertStore\.merge/);
});

test("空数据和无效音频均真实兜底", () => {
  for (const label of ["尚无逐字稿", "尚无段级点评", "整场总结尚未生成", "尚未录入", "尚未接入", "录音不可用"])
    assert.match(anchor, new RegExp(label));
  assert.match(anchor, /media\.playable \?/);
});

test("逐字稿点评与音频联动并释放监听器", () => {
  assert.match(anchor, /player\.currentTime/);
  assert.match(anchor, /timeupdate/);
  assert.match(anchor, /removeEventListener\("timeupdate"/);
  assert.match(anchor, /pagehide/);
});

test("客户端不把密码或Token写入LocalStorage", () => {
  assert.doesNotMatch(anchor, /localStorage/);
});

test("管理端读取ready并在未就绪时拦截建场", () => {
  assert.match(admin, /api\("\/api\/ready"\)/);
  assert.match(admin, /if \(!\(await ensureReady\(\)\)\) return/);
  assert.match(admin, /系统未就绪，暂不能新建场次/);
});
