const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const html = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf8");
const js = fs.readFileSync(path.join(__dirname, "..", "app.js"), "utf8");
const anchorHtml = fs.readFileSync(path.join(__dirname, "..", "anchor.html"), "utf8");

test("顶栏最左边是回平台导航页的「系统导航」", () => {
  const topbar = html.slice(html.indexOf('<div class="topbar">'), html.indexOf('<div class="layout">'));
  const first = topbar.split("\n")[1].trim();
  assert.match(first, /^<a class="navhome" id="navHome" href="\/" title="回到办公平台的系统导航">/);
  assert.match(first, /<rect width="7" height="7" x="3" y="3" rx="1"\/>.*<rect width="7" height="7" x="3" y="14" rx="1"\/>/);
  assert.match(first, /<span class="full">系统导航<\/span><span class="short">导航<\/span><\/a>$/);
  assert.match(html, /\.navhome\{[^}]*height:32px[^}]*border:1px solid[^}]*border-radius:999px[^}]*font-size:13px;font-weight:700/);
  assert.match(html, /@media\(max-width:600px\)\{[^@]*\.navhome \.full\{display:none\}\.navhome \.short\{display:inline\}/);
});

test("详情等弹窗从顶栏下方开始，顶栏在详情里也看得见", () => {
  assert.match(html, /\.modal-mask\{position:fixed;inset:58px 0 0 0;/);
});

test("本机直连（应急口令会话）不显示，主播端页面没有", () => {
  assert.match(js, /if \(\/\^\(127\\\.0\\\.0\\\.1\|localhost\|\\\[::1\\\]\)\$\/\.test\(location\.hostname\)\) hideNavHome\(\);/);
  assert.match(js, /if \(\["emergency", "password"\]\.includes\(_authMe\.auth\)\) hideNavHome\(\);/);
  assert.doesNotMatch(anchorHtml, /navHome|navhome/);
  // 主播签名外链页整页换掉，不会带出顶栏
  assert.match(js, /async function renderViewerMode\(qs\) \{[\s\S]{0,200}document\.body\.innerHTML = /);
});
