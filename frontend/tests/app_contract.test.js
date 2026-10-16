const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "..", "app.js"), "utf8");

test("罗盘页面使用全局入口并在详情加载时查询", () => {
  assert.match(source, /api\("\/api\/compass\/setup"\)/);
  assert.doesNotMatch(source, /sessions\/\$\{id\}\/compass\/setup/);
  assert.match(source, /loadCompass\(id\);\s*renderDetailFor/);
  assert.match(source, /尚未导入/);
  assert.match(source, /记录数量/);
});

test("罗盘导入状态查询由请求门保护", () => {
  assert.match(source, /compassGate\.has\(`compass-\$\{id\}`\)/);
  assert.match(source, /compassGate\.run\(`compass-\$\{id\}`/);
  assert.match(source, /导入中/);
  assert.match(source, /Client\.compassStatus\(null, false, true\)/);
});

test("建场、停止与终态轮询均有客户端保护", () => {
  assert.match(source, /Client\.normalizeLiveLink/);
  assert.match(source, /findExistingSession/);
  assert.match(source, /createGate\.has\("start"\)/);
  assert.match(source, /stopGate\.has\(`stop:\$\{curSession\}`\)/);
  assert.match(source, /正在结束直播/);
  assert.match(source, /Client\.isTerminal\(session\.status\)/);
  assert.match(source, /stopSessionPolling\(\)/);
});

test("拉流、录音与点评状态文案完整", () => {
  assert.match(source, /直播已结束或拉流地址失效/);
  assert.match(source, /录音不可用/);
  assert.match(source, /播放录音/);
  assert.match(source, /降级结果，仅供参考/);
  assert.match(source, /模型点评/);
});
