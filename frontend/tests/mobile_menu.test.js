const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const html = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf8");
const anchorHtml = fs.readFileSync(path.join(__dirname, "..", "anchor.html"), "utf8");

// 2026-10-01 全量手机巡检的适配规则：都在 ≤960px（侧栏变成顶部横排之后）/ <720px 的媒体查询里，桌面不受影响
function block(marker) {
  const start = html.indexOf(marker);
  assert.ok(start > 0, `缺少规则块：${marker}`);
  return html.slice(start, html.indexOf("\n}\n", start));
}

test("顶部栏目一行横排、不换行、可横向滑动，选中项清楚（≤960px）", () => {
  const b = block("/* ===== 手机与平板（≤960px");
  assert.ok(html.indexOf("/* ===== 手机与平板（≤960px") > html.indexOf(".v2seg{"), "须放在样式表最后，才能盖过深度复盘等组件自己的设置");
  assert.ok(html.indexOf("/* ===== 手机与平板（≤960px") > html.indexOf("@media(max-width:960px){\n    .cal-cell"), "须排在原 960px 规则之后才能覆盖它");
  assert.match(b, /\.sidebar,\.sidebar\.collapsed\{width:100%;[^}]*overflow-x:auto/);
  assert.match(b, /\.navitem\{flex:none;white-space:nowrap;/);
  assert.match(b, /\.navitem\.active\{background:[^}]*border-color:[^}]*\}/);
});

test("可点元素至少 32px、字至少 12px、表格可横向滑动、日历改显示场次数", () => {
  const b = block("/* ===== 手机与平板（≤960px");
  assert.match(b, /\.btn\.sm,\.fbtn,\.clip-btn,\.viewtoggle button,\.v2zoombar button\{min-height:32px\}/);
  assert.match(b, /\[style\*="font-size:11px"\][^{]*\{font-size:12px!important\}/);
  assert.match(b, /#view-streamers \.card,#view-kb \.card,#view-scripts \.card\{overflow-x:auto\}/);
  assert.match(b, /\.cal-pill,\.cal-more\{display:none\}/);
});

test("手机（<720px）：卡片标题换行、深度复盘问题卡按钮移到标题下", () => {
  const b = block("/* ===== 手机（<720px）");
  assert.match(b, /\.card-head\{flex-wrap:wrap;gap:8px\}/);
  assert.match(b, /\.v2clips\{flex-basis:100%;/);
});

test("返工：主内容栏跟屏幕同宽，指标卡两列、最近复盘和历史列表不再超出屏幕", () => {
  const b = block("/* ===== 手机与平板（≤960px");
  assert.match(b, /\.layout > main\{width:100%;min-width:0\}/, "主内容栏不能被不换行的长内容撑宽");
  assert.match(b, /\.kpi-row\{grid-template-columns:repeat\(2,minmax\(0,1fr\)\)\}/);
  assert.match(b, /\.home-grid\{grid-template-columns:minmax\(0,1fr\)\}/);
  assert.match(b, /\.v2seg\{display:block;[^}]*text-overflow:ellipsis\}/, "时间轴段名用省略号，不能被硬切");
  const m = block("/* ===== 手机（<720px）");
  assert.match(m, /\.recent-item > div\{order:2;flex-basis:100%!important\}/);
  assert.match(m, /#histListCard thead\{display:none\}/);
  assert.match(m, /#histRows tr\{display:grid;/);
  assert.match(m, /grid-template-areas:"title title" "status status" "time dur" "who who" "ops ops"/);
});

test("知识库切片表：来源列可换行（不挤窄内容），手机上一条切片一张卡", () => {
  const b = block("/* ===== 手机与平板（≤960px");
  assert.match(b, /#kbChunkRows td:nth-child\(1\)\{overflow-wrap:anywhere\}/);
  assert.doesNotMatch(b, /#kbChunkRows td:nth-child\(-n\+2\)/, "来源列不能再强制不换行");
  const m = block("/* ===== 手机（<720px）");
  assert.match(m, /#kbSceneBar \+ \.card thead\{display:none\}/);
  assert.match(m, /#kbChunkRows tr\{display:grid;/);
});

test("详情弹窗遮罩在平板以下不用毛玻璃（否则片段回放浮层的固定定位失效、滚动时漂到屏幕中间）", () => {
  const b = block("/* ===== 手机与平板（≤960px");
  assert.match(b, /\.modal-mask\{-webkit-backdrop-filter:none;backdrop-filter:none\}/);
  assert.match(html, /\n  \.modal-mask\{position:fixed;inset:58px 0 0 0;[^}]*backdrop-filter:blur\(3px\)/, "桌面保留原来的毛玻璃");
});

test("主播端：平板以下状态卡单列、通知开关够大", () => {
  assert.match(anchorHtml, /@media\(max-width:960px\)\{\.status-grid\{grid-template-columns:1fr\}label\.pill\{[^}]*min-height:32px/);
});

test("桌面（>960px）的基础样式不变", () => {
  assert.match(html, /\n  \.navitem\{display:flex;align-items:center;gap:11px;padding:9px 12px;border-radius:9px;color:var\(--dim\);\n    cursor:pointer;font-weight:500;font-size:14px;transition:\.15s;user-select:none\}/);
  assert.match(html, /\n  \.navitem\.active\{background:var\(--surface3\);color:var\(--text\)\}/);
  assert.match(html, /\n  \.tag\{font-size:10\.5px;padding:1px 7px;border-radius:5px;font-weight:600\}/);
  assert.match(html, /\n  \.btn\.sm\{padding:6px 12px;font-size:13px;border-radius:8px\}/);
  assert.match(html, /\n  \.layout > main\{flex:1;min-width:0;padding:22px 26px;max-width:none;margin:0\}/);
});
