const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const C = require("../client_state.js");

test("前后端共享直播链接规范化契约", () => {
  const cases = JSON.parse(fs.readFileSync(
    path.join(__dirname, "..", "..", "test-data", "live-link-normalization.json"), "utf8"));
  assert.deepEqual(cases.map(item => C.normalizeLiveLink(item.input)),
    cases.map(item => item.expected));
});

test("罗盘状态覆盖空、导入中、成功和失败", () => {
  assert.equal(C.compassStatus({}, false, false).label, "尚未导入");
  assert.equal(C.compassStatus({}, true, false).label, "导入中");
  assert.deepEqual(C.compassStatus({ today_points: 7 }, false, false),
    { key: "success", label: "导入成功", count: 7 });
  assert.equal(C.compassStatus({}, false, true).label, "导入失败");
});

test("RequestGate 对连续点击只执行一次请求", async () => {
  const gate = new C.RequestGate();
  let calls = 0;
  const task = () => new Promise(resolve => setTimeout(() => resolve(++calls), 10));
  const [a, b] = await Promise.all([gate.run("import", task), gate.run("import", task)]);
  assert.equal(calls, 1);
  assert.equal(a, 1);
  assert.equal(b, 1);
});

test("同一抖音直播链接忽略分享参数并拦截未结束场次", () => {
  const sessions = [{ id: 8, status: "live", ended_at: null,
    share_link: "https://live.douyin.com/789?enter_method=copy_link_share" }];
  const found = C.findDuplicateSession(sessions,
    " https://live.douyin.com/789?action_type=click&from=web_code_link ");
  assert.equal(found.id, 8);
  sessions[0].status = "done";
  assert.equal(C.findDuplicateSession(sessions, "https://live.douyin.com/789"), null);
});

test("录音不可用不显示播放，正常音频允许播放", () => {
  assert.equal(C.audioPresentation({ status: "done", audio: null }).playable, false);
  assert.equal(C.audioPresentation({ status: "done", audio: { status: "available", size_bytes: 0 } }).playable, false);
  assert.equal(C.audioPresentation({ status: "done", audio: { status: "available", size_bytes: 128 } }).playable, true);
  assert.equal(C.audioPresentation({ status: "error", error_msg: "合并失败：分片缺失" }).reason, "合并失败：分片缺失");
});

test("降级点评有明确标识，真实结果不混淆", () => {
  assert.equal(C.fallbackMeta(true).label, "降级结果，仅供参考");
  assert.equal(C.fallbackMeta(false).label, "模型点评");
});

test("错误文本隐藏密钥、签名和内部地址", () => {
  const text = C.safeError({ detail: "请求失败 api_key=secret https://internal/api?sign=abc" });
  assert.doesNotMatch(text, /secret|internal|abc/);
});

test("终态识别用于停止轮询", () => {
  assert.equal(C.isTerminal("done"), true);
  assert.equal(C.isTerminal("error"), true);
  assert.equal(C.isTerminal("processing"), false);
});

test("系统未就绪时返回明确建场拦截原因", () => {
  const state = C.readinessState({ ready: false, blockers: ["ASR：未配置", "LLM：未配置"] });
  assert.equal(state.ready, false);
  assert.match(state.message, /ASR.*LLM/);
});

test("40分钟规定话术联调结果完整展示时间、间隔、时长、相似度和原因", () => {
  const occurrences = [
    { status: "matched", issues: [], actual_start_ms: 120000,
      actual_duration_sec: 30, interval_from_previous_sec: null, similarity_percent: 92 },
    { status: "too_short", issues: ["too_short", "late"], actual_start_ms: 900000,
      actual_duration_sec: 15, interval_from_previous_sec: 780, similarity_percent: 100 },
    { status: "matched", issues: [], actual_start_ms: 1500000,
      actual_duration_sec: 30, interval_from_previous_sec: 600, similarity_percent: 86 },
    { status: "low_similarity", issues: ["low_similarity"], actual_start_ms: 1800000,
      actual_duration_sec: 30, interval_from_previous_sec: 300, similarity_percent: 65 },
    { status: "missing", issues: ["missing"], actual_start_ms: null,
      actual_duration_sec: null, interval_from_previous_sec: null, similarity_percent: null },
  ].map(C.scriptOccurrencePresentation);

  assert.deepEqual(occurrences.map(item => item.actualTime),
    ["02:00", "15:00", "25:00", "30:00", "—"]);
  assert.deepEqual(occurrences.map(item => item.intervalText),
    ["—", "780秒", "600秒", "300秒", "—"]);
  assert.deepEqual(occurrences.map(item => item.durationText),
    ["30秒", "15秒", "30秒", "30秒", "—"]);
  assert.deepEqual(occurrences.map(item => item.similarityText),
    ["92%", "100%", "86%", "65%", "—"]);
  assert.deepEqual(occurrences.map(item => item.reasonText),
    ["达标", "时长不足、过晚", "达标", "相似度不足", "漏讲"]);
});
