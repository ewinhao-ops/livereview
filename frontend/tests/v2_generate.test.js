const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "app.js"), "utf8");

test("v2 生成按钮不用重复 id，五个页签都走事件委托", () => {
  assert.doesNotMatch(source, /id="v2genBtn/);
  assert.doesNotMatch(source, /getElementById\("v2genBtn/);
  assert.doesNotMatch(source, /detailState\.v2Generating/);
  assert.match(source, /data-v2genbox/);
  assert.match(source, /closest\("\[data-v2gen\]"\)/);
});

test("v2 生成按钮如实提示耗时", () => {
  assert.doesNotMatch(source, /生成 v2 深度复盘（约 1–2 分钟）/);
  assert.doesNotMatch(source, /一般要 10–20 分钟/);       // 2026-09-30 后端并行后不再要这么久
  assert.match(source, /一般要 3–6 分钟/);
});

// 把「生成任务」这一段单独拿出来，在假环境里跑状态流转
function loadJobs(opts = {}) {
  const begin = source.indexOf("// ---------- 生成任务 ----------");
  const end = source.indexOf("// ---------- 主入口 ----------");
  assert.ok(begin > 0 && end > begin, "找不到生成任务代码段");
  const store = new Map(Object.entries(opts.storage || {}));
  const env = {
    now: opts.now || 1_000_000_000_000, calls: [], rendered: [], pending: [],
    streamers: opts.streamers || [{ streamer_id: 4, name: "小陈", summary: { data: "{}" } }],
  };
  const ctx = {
    Number, String, Math, JSON, Map, Promise, URLSearchParams,
    Date: { now: () => env.now },
    localStorage: { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)) },
    document: { hidden: false, querySelectorAll: () => [], getElementById: () => null },
    setInterval: () => 1, clearInterval: () => { },
    _viewQS: new URLSearchParams(""),
    detailState: { id: 20, currentSid: "4", streamers: env.streamers },
    modalOpen: () => true,
    parseJSON: s => { try { return s ? JSON.parse(s) : {}; } catch (e) { return {}; } },
    nameOf: (sid, fb) => fb || "未指定主播",
    esc: s => String(s == null ? "" : s),
    fmtErr: (e, fb) => (e && (e.detail || e.message)) || fb,
    renderDetailFor: (id, sid) => { env.rendered.push([id, sid]); },
    api: (p, o) => {
      env.calls.push(`${(o && o.method) || "GET"} ${p}`);
      if (p.endsWith("/streamers")) return Promise.resolve(env.streamers);
      return new Promise((resolve, reject) => env.pending.push({ resolve, reject }));
    },
  };
  vm.createContext(ctx);
  vm.runInContext(source.slice(begin, end) + "\n;globalThis.t = { _v2Jobs, v2StartJob, v2JobsRestore, v2JobsPoll, v2JobsTick, v2GenBoxInner, v2JobsAdopt };", ctx);
  return { ...ctx.t, env, ctx, store, flush: () => new Promise(r => setImmediate(r)) };
}
const withV2 = stamp => [{ streamer_id: 4, name: "小陈", summary: { data: JSON.stringify({ v2: { generated_at: stamp } }) } }];

test("点生成后记为生成中并落盘，重复点不再发请求；成功后自动刷新", async () => {
  const j = loadJobs();
  j.v2StartJob(20, 4); j.v2StartJob("20", "4");
  assert.deepEqual(j.env.calls, ["POST /api/sessions/20/reanalyze_v2?streamer_id=4"]);
  assert.equal(j._v2Jobs.get("20:4").status, "running");
  assert.match(j.store.get("fupan_v2_jobs"), /"20:4"/);
  assert.match(j.v2GenBoxInner(20, 4), /生成中 · 已等/);
  assert.match(j.v2GenBoxInner(20, 6), /data-v2gen/);          // 别的主播仍是自己的可点按钮
  j.env.streamers = withV2("2026-09-30 17:34"); j.env.pending[0].resolve({ ok: true });
  await j.flush();
  assert.equal(j._v2Jobs.size, 0);
  assert.deepEqual(j.env.rendered, [[20, "4"]]);
  assert.equal(j.store.get("fupan_v2_jobs"), "{}");
});

test("服务器回失败时显示原因并允许重试", async () => {
  const j = loadJobs();
  j.v2StartJob(20, 4); j.env.pending[0].resolve({ ok: false, error: "该场次没有整场总结" });
  await j.flush();
  assert.equal(j._v2Jobs.get("20:4").status, "failed");
  assert.match(j.v2GenBoxInner(20, 4), /重试生成[\s\S]*该场次没有整场总结/);
  j.v2StartJob(20, 4);
  assert.equal(j.env.calls.filter(c => c.startsWith("POST")).length, 2);
});

test("刚点就断网算失败；跑了一阵才断线则继续等，查到新结果后自动刷新", async () => {
  const quick = loadJobs();
  quick.v2StartJob(20, 4); quick.env.now += 3_000; quick.env.pending[0].reject(new TypeError("Failed to fetch"));
  await quick.flush();
  assert.equal(quick._v2Jobs.get("20:4").status, "failed");

  const j = loadJobs();
  j.v2StartJob(20, 4); j.env.now += 5 * 60_000; j.env.pending[0].reject(new TypeError("Failed to fetch"));
  await j.flush();
  assert.equal(j._v2Jobs.get("20:4").status, "running");
  assert.equal(j._v2Jobs.get("20:4").attached, false);
  await j.v2JobsPoll();
  assert.equal(j._v2Jobs.get("20:4").status, "running");       // 结果还没出来，接着等
  j.env.streamers = withV2("2026-09-30 17:34");
  await j.v2JobsPoll();
  assert.equal(j._v2Jobs.size, 0);
  assert.deepEqual(j.env.rendered, [[20, "4"]]);
});

test("刷新页面后接着显示生成中；超过 40 分钟没结果放开重试", async () => {
  const now = 1_000_000_000_000;
  const saved = { "20:4": { id: 20, sid: 4, startedAt: now - 10 * 60_000, prev: "" }, "21:6": { id: 21, sid: 6, startedAt: now - 50 * 60_000, prev: "" } };
  const j = loadJobs({ now, storage: { fupan_v2_jobs: JSON.stringify(saved) } });
  assert.deepEqual([...j._v2Jobs.keys()], ["20:4"]);            // 加载时已恢复；过期的那条丢掉
  assert.match(j.v2GenBoxInner(20, 4), /生成中 · 已等 <span[^>]*>10 分 0 秒</);
  j.v2StartJob(20, 4);
  assert.equal(j.env.calls.filter(c => c.startsWith("POST")).length, 0);   // 不会重复生成
  j.env.now += 31 * 60_000; j.v2JobsTick();
  assert.equal(j._v2Jobs.get("20:4").status, "failed");
  assert.match(j.v2GenBoxInner(20, 4), /重试生成/);
});

// ---- 2026-09-30：后端按主播报「正在生成」（/streamers 的 v2_job）----
const serverSays = (job, stamp) => [{ streamer_id: 4, name: "小陈", v2_job: job,
  summary: { data: stamp ? JSON.stringify({ v2: { generated_at: stamp } }) : "{}" } }];

test("别的设备点了生成：这里也显示生成中，再点不会重复发请求，出结果后自动刷新", async () => {
  const j = loadJobs({ streamers: serverSays({ status: "running", elapsed_sec: 95 }) });
  assert.match(j.v2GenBoxInner(20, 4), /data-v2gen/);            // 认领前：还是可点的按钮
  j.v2JobsAdopt(20, j.env.streamers);
  assert.equal(j._v2Jobs.get("20:4").status, "running");
  assert.equal(j._v2Jobs.get("20:4").attached, false);
  assert.match(j.v2GenBoxInner(20, 4), /生成中 · 已等 <span[^>]*>1 分 35 秒</);   // 按服务器说的已跑时间算
  assert.match(j.store.get("fupan_v2_jobs"), /"20:4"/);
  j.v2StartJob(20, 4);
  assert.equal(j.env.calls.filter(c => c.startsWith("POST")).length, 0);
  await j.v2JobsPoll();
  assert.equal(j._v2Jobs.get("20:4").status, "running");         // 服务器说还在跑，接着等
  j.env.streamers = serverSays(null, "2026-09-30 19:20");
  await j.v2JobsPoll();
  assert.equal(j._v2Jobs.size, 0);
  assert.deepEqual(j.env.rendered, [[20, "4"]]);
});

test("认领不会盖掉本机正在等的任务；本机记成失败但服务器还在跑的，改回生成中", async () => {
  const j = loadJobs({ streamers: serverSays({ status: "running", elapsed_sec: 5 }) });
  j.v2StartJob(20, 4);
  const mine = j._v2Jobs.get("20:4");
  j.v2JobsAdopt(20, j.env.streamers);
  assert.equal(j._v2Jobs.get("20:4"), mine);                      // 自己发出去的请求还挂着，原样不动
  j.env.pending[0].reject(Object.assign(new Error("网关超时"), { status: 504 }));
  await j.flush();
  assert.equal(j._v2Jobs.get("20:4").status, "failed");
  j.v2JobsAdopt(20, j.env.streamers);
  assert.equal(j._v2Jobs.get("20:4").status, "running");
  j.v2JobsAdopt(20, serverSays(null));                            // 服务器没在跑：不认领、不改动
  assert.equal(j._v2Jobs.get("20:4").status, "running");
});

test("服务器说没在跑、结果也没变：马上放开重试，不用等 40 分钟", async () => {
  const now = 1_000_000_000_000;
  const saved = { "20:4": { id: 20, sid: 4, startedAt: now - 4 * 60_000, prev: "2026-09-30 17:34" } };
  const j = loadJobs({ now, storage: { fupan_v2_jobs: JSON.stringify(saved) }, streamers: serverSays(null, "2026-09-30 17:34") });
  assert.equal(j._v2Jobs.get("20:4").status, "running");         // 刷新后先接着显示生成中
  await j.v2JobsPoll();
  assert.equal(j._v2Jobs.get("20:4").status, "failed");
  assert.match(j.v2GenBoxInner(20, 4), /重试生成[\s\S]*后台这次没有生成出结果/);
});

test("「未指定主播」的场次：服务器报的主播号是 0，对得上本机的空主播任务", async () => {
  const none = job => [{ streamer_id: 0, name: "未指定主播", v2_job: job, summary: { data: "{}" } }];
  const j = loadJobs({ streamers: none({ status: "running", elapsed_sec: 10 }) });
  j.v2JobsAdopt(20, j.env.streamers);
  assert.equal(j._v2Jobs.get("20:").status, "running");
  assert.match(j.v2GenBoxInner(20, null), /生成中/);
  j.env.streamers = none(null);
  await j.v2JobsPoll();
  assert.equal(j._v2Jobs.get("20:").status, "failed");
});

test("认领只发生在打开详情拿到最新状态时，重画页签不再认领", () => {
  const open = source.slice(source.indexOf("async function loadDetail("), source.indexOf("async function loadDetail(") + 1200);
  assert.match(open, /detailState = \{ id, streamers,[^\n]*\n\s*v2JobsAdopt\(id, streamers\);/);
  const render = source.slice(source.indexOf("function renderV2Panes("), source.indexOf("function renderV2Panes(") + 1500);
  assert.doesNotMatch(render, /v2JobsAdopt/);
});
