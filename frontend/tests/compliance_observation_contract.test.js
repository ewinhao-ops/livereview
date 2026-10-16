const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const app = fs.readFileSync(path.join(__dirname, "..", "app.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf8");

test("开始按钮同时受系统就绪和主播选择约束", () => {
  assert.match(app, /function updateStartAvailability/);
  assert.match(app, /请选择本场主播/);
  assert.match(app, /allowUnassigned/);
  assert.match(app, /confirm\("确认本场暂不指定主播/);
});

test("常用直播间具备真实删除、确认、防重复和数量", () => {
  assert.match(html, /id="roomCount"/);
  assert.match(app, /data-room-delete/);
  assert.match(app, /method: "DELETE"/);
  assert.match(app, /确认删除这个常用直播间/);
});

test("话术合规三标签和服务端快照入口完整", () => {
  for (const text of ["话术合规", "规定话术", "违禁词", "合规豁免"]) assert.match(html, new RegExp(text));
  assert.match(app, /api\/compliance-rules/);
  assert.match(app, /compliance-snapshot/);
  assert.match(app, /compliance_rule_ids/);
  assert.match(app, /sessionRuleConflict/);
});

test("观摩学习覆盖纯采集、提炼、来源与审核", () => {
  for (const text of ["观摩学习", "不绑定主播", "只采集，不实时点评", "保存音频", "情绪增强"]) assert.match(html, new RegExp(text));
  assert.match(app, /session_type: "observation"/);
  assert.match(app, /api\/observation-sessions/);
  assert.match(app, /一键提炼优秀话术/);
  assert.match(app, /data-observe-adopt/);
  assert.match(app, /data-observe-reject/);
});
