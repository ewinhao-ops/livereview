const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const Client = require("../client_state.js");

const adminJs = fs.readFileSync(path.join(__dirname, "..", "app.js"), "utf8");
const anchorJs = fs.readFileSync(path.join(__dirname, "..", "anchor.js"), "utf8");
const adminHtml = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf8");
const anchorHtml = fs.readFileSync(path.join(__dirname, "..", "anchor.html"), "utf8");

test("401、409、503和网络错误提供明确中文状态", () => {
  assert.match(adminJs, /status:\s*401/);
  assert.match(Client.safeError({ status: 409, detail: { message: "duplicate" } }), /原场次/);
  assert.match(Client.safeError({ status: 503, detail: "ASR：未配置" }), /ASR/);
  assert.match(Client.safeError({ name: "TypeError", message: "Failed to fetch" }), /网络连接失败/);
  assert.doesNotMatch(Client.safeError("merge failed C:/Users/test/private.m4a"), /Users|private/);
});

test("刷新只恢复状态而不重复建场、启动或停止", () => {
  assert.match(adminJs, /checkLiveNow\(\)/);
  assert.doesNotMatch(adminJs, /(?:DOMContentLoaded|window\.onload)[\s\S]{0,120}(?:createSession|startSession|stopSession)/);
});

test("多次断开只保留一个重连计时器并在离页时清理", () => {
  assert.match(anchorJs, /if \(retryTimer\) return/);
  assert.match(anchorJs, /wsGeneration/);
  assert.match(anchorJs, /cleanupRealtime/);
  assert.match(anchorJs, /socket\.onopen = socket\.onmessage = socket\.onerror = socket\.onclose = null/);
});

test("三类提醒完整展示严重程度、原话、建议和时间", () => {
  for (const token of ["severityLabel", "occurred_at", "item.quote", "item.suggestion"])
    assert.match(anchorJs, new RegExp(token.replace(".", "\\.")));
});

test("桌面端和窄屏关键布局具有响应式防溢出规则", () => {
  assert.match(adminHtml, /@media\s*\(max-width:\s*(?:900|960|980|1040)px\)/);
  assert.match(anchorHtml, /@media\(max-width:760px\)/);
  for (const source of [adminHtml, anchorHtml]) {
    assert.match(source, /overflow-x:hidden/);
    assert.match(source, /max-width:100%/);
  }
});

test("规定话术管理、场次快照、分析状态和来源定位契约完整", () => {
  assert.match(adminHtml, /data-view="scripts"/);
  assert.match(adminHtml, /id="sessionScriptPicker"/);
  assert.match(adminJs, /scriptSaveGate\.has\("rule"\)/);
  assert.match(adminJs, /频率、时长必须为正数/);
  assert.match(adminJs, /相似度只能填写0～100/);
  assert.match(adminJs, /script-compliance\/plan/);
  assert.match(adminJs, /data-pane="prescribed"/);
  assert.equal(Client.SCRIPT_STATUS.matched, "达标");
  assert.equal(Client.SCRIPT_STATUS.missing, "漏讲");
  assert.match(adminJs, /scriptOccurrencePresentation/);
  for (const label of ["实际时间", "时长", "距上次", "相似度", "reasonText"])
    assert.match(adminJs, new RegExp(label));
  assert.match(adminJs, /data-script-sources/);
  assert.match(adminJs, /data-transcript-id/);
});
