// ===== 工具 =====
const $ = (s) => document.querySelector(s);
const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
const BASE_PATH = location.pathname === "/fupan" || location.pathname.startsWith("/fupan/") ? "/fupan" : "";
const withBase = (p) => `${BASE_PATH}${p}`;
const Client = window.LiveReviewClient;
// 主播签名外链（/?view=…&sig=…）：免登录只读页，任何情况下都不跳平台登录
const VIEWER_MODE = (() => { const q = new URLSearchParams(location.search); return Boolean(q.get("view") && q.get("sig")); })();
const api = (p, opt = {}) => {
  const headers = { ...(opt.headers || {}) };
  if (typeof opt.body === "string" && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  return fetch(withBase(p), { ...opt, headers }).then(r => {
  if (r.status === 401 || r.status === 403 || r.status === 503) return r.json().catch(() => ({ detail: r.statusText })).then(j => {
    // 统一登录没通过：整页跳走（或显示应急口令框），这次请求就停在这里，不再弹出任何报错
    if (authBounce(r.status, j)) return new Promise(() => { });
    if (r.status === 401) return Promise.reject({ ...j, detail: j.detail || "登录会话已过期，请重新登录", status: 401 });
    return Promise.reject({ ...j, status: r.status });
  });
  if (r.ok) return r.json();
  return r.json().catch(() => ({ detail: r.statusText })).then(j => Promise.reject({ ...j, status: r.status }));
  });
};
// ===== 统一登录（workspace.example.test 平台）=====
// 没登录 → 整页跳平台登录页（next = 本系统站内路径 + 查询 + 井号）；没开通 → 跳平台「申请开通」页；
// 服务器本机直连（SSH 隧道应急）时服务端会标 emergency_login，改显示应急口令框。
// 20 秒内因同一原因刚跳过一次又被拦回来（多半是配置问题），不再来回跳，改显示提示；
// 「刚登录完发现没开通」是正常的先后两步，不算来回跳。
const AUTH_BOUNCE_KEY = "fupan_auth_bounce";
let _authMe = {};
function authBounce(status, j) {
  const code = (j && j.code) || "";
  if (VIEWER_MODE || !["need_login", "need_access", "auth_unavailable"].includes(code)) return false;
  if (j.emergency_login) { showLogin(); return true; }
  if (code === "auth_unavailable") { showAuthNotice(j.detail || "统一登录暂时无法核验，请稍后刷新重试"); return true; }
  let target = j.go_url || "";
  if (code === "need_login" && j.login_url) {
    const u = new URL(j.login_url, location.href);
    const home = (u.searchParams.get("next") || "/").replace(/\/+$/, "");
    u.searchParams.set("next", home + (location.pathname.slice(BASE_PATH.length) || "/") + location.search + location.hash);
    target = u.toString();
  }
  if (!target) { showAuthNotice("登录状态已失效，请刷新页面"); return true; }
  let last = {};
  try { last = JSON.parse(sessionStorage.getItem(AUTH_BOUNCE_KEY) || "{}") || {}; } catch (e) { }
  if (last.code === code && Date.now() - Number(last.at || 0) < 20000) {
    showAuthNotice("统一登录核验没有通过（刚刚已经跳转过一次）。请稍后刷新；一直这样请联系管理员。");
    return true;
  }
  try { sessionStorage.setItem(AUTH_BOUNCE_KEY, JSON.stringify({ code, at: Date.now() })); } catch (e) { }
  location.replace(target);
  return true;
}
function authReady(me) {
  _authMe = me || {};
  try { sessionStorage.removeItem(AUTH_BOUNCE_KEY); } catch (e) { }
  const u = _authMe.user || {};
  $("#whoAmI").textContent = u.display_name || u.username || "";
  if (["emergency", "password"].includes(_authMe.auth)) hideNavHome();
  document.documentElement.classList.remove("fupan-authing");
}
// 顶栏「系统导航」（<a href="/">，整页回平台导航页）：本机直连（SSH 隧道的应急口令会话，127.0.0.1/localhost）
// 没有平台导航页可回，不显示；主播签名外链页整页换掉、没有顶栏，主播端 anchor.html 本来就没有
function hideNavHome() { const a = $("#navHome"); if (a) a.remove(); }
if (/^(127\.0\.0\.1|localhost|\[::1\])$/.test(location.hostname)) hideNavHome();
const fmtErr = (e, fallback) => Client.safeError(e, fallback);
// HLS 播放挂载：hls.js 永远优先（vendor 内置），原生 src 仅作最后回退。返回销毁函数。
// 坑：Mac 版 Chrome 对 canPlayType("application/vnd.apple.mpegurl") 返回 "maybe" 但实际
// 不能原生播 HLS——曾据此提前走原生分支导致 Mac 用户全员播放失效（Windows 返回 "" 无感）。
function attachMedia(el, url, isHls) {
  el.muted = false; el.volume = 1;   // 防任何静音残留状态(用户反馈过"播放没声音")
  if (!isHls && !/\.m3u8(\?|$)/.test(url)) { el.src = url; return () => { }; }
  if (window.Hls && window.Hls.isSupported()) {
    const h = new Hls({ maxBufferLength: 30 });
    h.loadSource(url); h.attachMedia(el);
    return () => { try { h.destroy(); } catch (e) { } };
  }
  el.src = url; return () => { };
}
let _detailHlsDispose = null;
const jpost = (p, body) => api(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
const go = (p) => { location.href = withBase(p); };
const esc = (s) => (s == null ? "" : String(s).replace(/[&<>"']/g, c => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c])));
const asObj = (v) => (v && typeof v === "object" && !Array.isArray(v)) ? v : {};
const objList = (v) => Array.isArray(v) ? v.filter(x => x && typeof x === "object" && !Array.isArray(x)) : [];
const mmss = (ms) => { const s = Math.floor((ms || 0) / 1000); return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`; };
// 相对毫秒 → 北京时间墙钟 HH:MM:SS（逐字稿/点评行显示用；无开播时间回退 mm:ss）
// 逐字稿/点评跟随高亮:只滚动所在滚动框(.stream),绝不带动整页——
// scrollIntoView 会冒泡滚动 modal/页面,曾把用户从视频区强行拽到逐字稿区。
const scrollInBox = (el, smooth) => {
  const box = el && el.closest(".stream");
  if (!box) return;
  const er = el.getBoundingClientRect(), br = box.getBoundingClientRect();
  if (er.top < br.top || er.bottom > br.bottom) {
    const target = box.scrollTop + (er.top - br.top) - br.height / 2 + er.height / 2;
    if (smooth) box.scrollTo({ top: target, behavior: "smooth" });
    else box.scrollTop = target;
  }
};
const wallT = (startedAt, ms) => {
  if (!startedAt) return mmss(ms);
  const d = new Date(new Date(startedAt).getTime() + (ms || 0));
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}`;
};
const scrollDown = (b) => { b.scrollTop = b.scrollHeight; };

// 时钟
setInterval(() => { const d = new Date(); $("#clock").textContent = d.toLocaleTimeString("zh-CN", { hour12: false }); }, 1000);

// ===== 主播花名册 =====
let roster = [], rosterMap = {};
async function loadRoster() {
  roster = await api("/api/streamers");
  rosterMap = {}; roster.forEach(s => rosterMap[s.id] = s);
  if (!lineup.length) {
    try {
      const remembered = JSON.parse(localStorage.getItem("fupan_last_lineup") || "[]");
      lineup = Array.isArray(remembered) ? remembered.map(Number).filter(id => rosterMap[id]) : [];
    } catch (_) { lineup = []; }
  }
}
const colorOf = (sid) => (rosterMap[sid] && rosterMap[sid].color) || "#5a6376";
const nameOf = (sid, fb) => (rosterMap[sid] && rosterMap[sid].name) || fb || "未指定主播";

// ===== Tab 切换 =====
const PAGE_TITLES = { home: "工作台", live: "实时复盘", history: "历史复盘", streamers: "主播", kb: "知识库", scripts: "话术合规", observe: "观摩学习", stats: "数据统计" };
let listPollTimer = null, listPollView = "";
let homeSessionsCache = [];
const LIST_ACTIVE = new Set(["draft", "starting", "live", "stopping", "finalizing", "created", "parsing", "processing"]);
function stopListPolling() { if (listPollTimer) clearTimeout(listPollTimer); listPollTimer = null; listPollView = ""; }
function scheduleListPolling(view, sessions) {
  stopListPolling();
  if (!(sessions || []).some(s => LIST_ACTIVE.has(s.status))) return;
  listPollView = view;
  listPollTimer = setTimeout(async () => {
    listPollTimer = null;
    if (listPollView !== view || !$("#view-" + view).classList.contains("active")) return;
    if (view === "home") await loadHome(); else await loadHistory();
  }, document.hidden ? 30000 : 9000);
}
function switchView(v) {
  stopListPolling();
  stopObservationPolling();
  document.querySelectorAll(".navitem").forEach(x => x.classList.toggle("active", x.dataset.view === v));
  document.querySelectorAll(".view").forEach(x => x.classList.remove("active"));
  $("#view-" + v).classList.add("active");
  if ($("#pageTitle")) $("#pageTitle").textContent = PAGE_TITLES[v] || "";
  if (v === "home") { loadHome(); syncActiveSession(false); }
  if (v === "live") { loadRoster().then(renderLineupPicker); loadRooms(); checkLiveNow(); loadReady(); loadSessionScriptPicker(); syncActiveSession(); }
  if (v === "history") loadHistory();
  if (v === "streamers") loadStreamerTab();
  if (v === "kb") { loadKb(); loadSettings(); }
  if (v === "scripts") { loadScriptRules(); loadComplianceRules(); }
  if (v === "observe") loadObservations();
  if (v === "stats") loadStats();
}
document.querySelectorAll(".navitem").forEach(t => t.onclick = () => switchView(t.dataset.view));
$("#btnGoLive") && ($("#btnGoLive").onclick = () => switchView("live"));
document.addEventListener("visibilitychange", () => {
  const active = document.querySelector(".view.active");
  const view = active && active.id.replace("view-", "");
  if (view !== "home" && view !== "history") return;
  stopListPolling();
  if (!document.hidden) { if (view === "home") loadHome(); else loadHistory(); }
  else scheduleListPolling(view, view === "history" ? _histCache : homeSessionsCache);
});

// ===== 工作台首页 =====
async function loadHome() {
  const sessions = await api("/api/sessions").catch(() => []);
  homeSessionsCache = sessions;
  await loadRoster();
  const now = new Date();
  const today = sessions.filter(s => s.started_at && new Date(s.started_at).toDateString() === now.toDateString()).length;
  let totalSec = 0;
  sessions.forEach(s => { if (s.started_at && s.ended_at) totalSec += Math.max(0, (new Date(s.ended_at) - new Date(s.started_at)) / 1000); });
  const kpis = [
    { v: sessions.length, u: "场", k: "累计复盘场次" },
    { v: today, u: "场", k: "今日场次" },
    { v: (totalSec / 3600).toFixed(1), u: "小时", k: "累计复盘时长" },
    { v: roster.length, u: "人", k: "主播数" },
  ];
  $("#homeKpis").innerHTML = kpis.map(x => `<div class="kpi"><div class="v">${x.v}<span class="u">${x.u}</span></div><div class="k">${esc(x.k)}</div></div>`).join("");
  const recent = sessions.slice(0, 7);
  $("#homeRecentCount").textContent = sessions.length;
  $("#homeRecent").innerHTML = recent.length ? recent.map(s => {
    const t = s.started_at ? s.started_at.replace("T", " ").slice(5, 16) : "-";
    return `<div class="recent-item" data-id="${s.id}"><span class="badge ${s.status}">${sessionStatusLabel(s)}</span><div style="flex:1;min-width:0"><div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(s.title) || "（未命名）"}</div><div class="faint" style="font-size:11px">${t} · ${fmtDur(s.started_at, s.ended_at)}</div></div><span class="faint">›</span></div>`;
  }).join("") : '<div class="empty">还没有复盘记录，去「实时复盘」开一场</div>';
  $("#homeRecent").querySelectorAll(".recent-item").forEach(el => el.onclick = () => {
    switchView("history");
    setTimeout(() => { if (typeof loadDetail === "function") loadDetail(+el.dataset.id); }, 400);
  });
  scheduleListPolling("home", sessions);
}

// ===================== 开播复盘 =====================
let ws = null, curSession = null, sentCount = 0, ocrCount = 0, flagCount = 0;
const scriptSaveGate = new Client.RequestGate();
let prescribedRules = [];
let wsRetry = 0, wsWantOpen = false;   // WS 自动重连控制
let partialEl = null, player = null, curStreamerId = null, lastStreamerId = null;
let lineup = [], curFilter = "seg";   // 默认 实时分析(段级)
let draftId = null;   // 粘贴链接后自动建场的草稿 id（开播时复用）
const createGate = new Client.RequestGate();
const stopGate = new Client.RequestGate();
const compassGate = new Client.RequestGate();
let sessionPollTimer = null;
let systemReady = null;
function updateStartAvailability(showHint = true) {
  const button = $("#btnStart"); if (!button) return false;
  const ready = !!(systemReady && systemReady.ready);
  const explicitlyUnassigned = !!($("#allowUnassigned") && $("#allowUnassigned").checked);
  const hasOwner = !roster.length || lineup.length > 0 || explicitlyUnassigned;
  button.disabled = !ready || !hasOwner || createGate.has("start");
  if (showHint && !createGate.has("start")) {
    if (!ready) $("#setupHint").textContent = "系统未就绪：" + (systemReady && systemReady.message || "正在检查运行状态");
    else if (!hasOwner) $("#setupHint").textContent = "请选择本场主播";
    else if ($("#setupHint").textContent.includes("请选择本场主播") || $("#setupHint").textContent.startsWith("系统未就绪")) $("#setupHint").textContent = "";
  }
  return !button.disabled;
}
let videoQuality = (() => {
  try {
    const q = localStorage.getItem("fupan_video_quality");
    return ["smooth", "clear", "high", "source"].includes(q) ? q : "clear";
  } catch (e) {
    return "clear";
  }
})();

async function syncActiveSession(showLiveUi = true) {
  try {
    const sessions = await api("/api/sessions");
    const active = sessions.find(s => ["live", "processing", "stopping", "finalizing"].includes(s.status));
    if (!active) { if (!curSession) $("#btnStop").disabled = true; return null; }
    curSession = Number(active.id);
    if (!showLiveUi) { setStatus(active.status, active); $("#btnStop").disabled = active.status !== "live"; return active; }
    await enterLiveUI(active);
    setStatus(active.status, active);
    $("#btnStop").disabled = active.status !== "live";
    if (active.status === "live") {
      if (!ws || ws.readyState > 1) openWs(curSession);
      if (!player) startPlayer(curSession);
    } else startSessionPolling(curSession);
    return active;
  } catch (e) {
    $("#setupHint").textContent = "活动场次同步失败：" + fmtErr(e);
    return null;
  }
}

// ---- 排班选择 ----
function renderLineupPicker() {
  const box = $("#lineupPicker"); box.innerHTML = "";
  if (!roster.length) {
    box.appendChild(el("span", "hint", '还没有主播，可先到「主播」页添加；也可直接开播（按"未指定主播"复盘）。'));
    return;
  }
  roster.forEach(s => {
    const idx = lineup.indexOf(s.id);
    const c = el("span", "chip" + (idx >= 0 ? " on" : ""));
    c.innerHTML = `<span class="dot" style="background:${s.color}"></span>${esc(s.name)}`
      + (idx >= 0 ? `<span class="num">${idx + 1}</span>` : "");
    c.onclick = () => { const i = lineup.indexOf(s.id); i >= 0 ? lineup.splice(i, 1) : lineup.push(s.id); if (lineup.length && $("#allowUnassigned")) $("#allowUnassigned").checked = false; try { localStorage.setItem("fupan_last_lineup", JSON.stringify(lineup)); } catch (_) {} renderLineupPicker(); };
    box.appendChild(c);
  });
  updateStartAvailability();
}
$("#allowUnassigned").addEventListener("change", () => {
  if ($("#allowUnassigned").checked) lineup = [];
  renderLineupPicker(); updateStartAvailability();
});

// ---- 播放器 ----
function updateQualityButtons() {
  document.querySelectorAll("#playerQuality .qbtn").forEach(b => b.classList.toggle("on", b.dataset.q === videoQuality));
}
function startPlayer(id) {
  destroyPlayer();
  const v = $("#player"), ph = $("#playerPh");
  if (!window.mpegts || !mpegts.isSupported()) { ph.textContent = "当前浏览器不支持 MSE/FLV（建议 Chrome/Edge）"; return; }
  player = mpegts.createPlayer(
    { type: "flv", isLive: true, url: withBase(`/api/sessions/${id}/live.flv?quality=${encodeURIComponent(videoQuality)}`) },
    {
      enableStashBuffer: true,
      stashInitialSize: videoQuality === "source" ? 2 * 1024 * 1024 : 1024 * 1024,
      liveBufferLatencyChasing: false,
    },
  );
  player.attachMediaElement(v);
  player.on(mpegts.Events.ERROR, async () => {
    ph.style.display = "flex"; ph.textContent = "画面加载失败，检测原因中…";
    try {
      const r = await fetch(withBase(`/api/sessions/${id}/live.flv?quality=smooth`));
      if (r.body && r.body.cancel) { try { r.body.cancel(); } catch (e) { } }
      if (r.status === 404 || r.status === 409) ph.textContent = "直播已结束或拉流地址失效";
      else ph.textContent = "拉流失败：可能是网络问题，或该直播正被多路同时拉取。若已在自动录制，请用上方「观看实时」而不要重复开播。";
    } catch (e) { ph.textContent = "拉流失败：网络异常，请稍后重试"; }
  });
  player.load(); player.play().catch(() => {});
  ph.style.display = "none";
  updateQualityButtons();
}
function destroyPlayer() { if (player) { try { player.destroy(); } catch (e) {} player = null; } }

// ---- 直播 UI ----
async function enterLiveUI(sess) {
  $("#setupCard").style.display = "none";
  $("#liveBar").style.display = "block";
  $("#liveTitle").textContent = sess.title || ("场次 #" + sess.id);
  if (!roster.length) await loadRoster();
  setCurStreamer(sess.current_streamer_id, sess.current_streamer_name);
  loadSessionRuleSnapshot(sess.id);
}
async function loadSessionRuleSnapshot(sessionId) {
  const box = $("#liveRuleSnapshot"); if (!box) return;
  try {
    const payload = await api(`/api/sessions/${sessionId}/compliance-snapshot`);
    if (!payload.snapshot_version) { box.textContent = "本场未选择追加合规规则"; return; }
    box.textContent = `${payload.locked ? "规则已锁定" : "规则快照已保存，开播后锁定"} · 规定话术 ${Number(payload.required_script_count || 0)} · 追加违禁词 ${Number(payload.forbid_rule_count || 0)} · 合规豁免 ${Number(payload.allow_rule_count || 0)}`;
  } catch (e) { box.textContent = "规则快照读取失败：" + fmtErr(e); }
}
function setCurStreamer(sid, name) {
  curStreamerId = sid;
  const nm = name || nameOf(sid);
  const cs = $("#curStreamer");
  cs.innerHTML = `<span class="dot" style="background:${colorOf(sid)}"></span>${esc(nm)}`;
  $("#playerBadge").style.display = "flex"; $("#playerBadgeName").textContent = nm;
  renderSwitchTargets();
}

// 列出全部主播，点谁就切到谁（当前主播高亮、不可点）
function renderSwitchTargets() {
  const box = $("#switchTargets"); if (!box) return;
  box.innerHTML = "";
  if (!roster.length) { box.appendChild(el("span", "hint", "主播列表为空，请到「主播」页添加")); return; }
  roster.forEach(s => {
    const on = s.id === curStreamerId;
    const c = el("span", "chip" + (on ? " on" : ""));
    c.innerHTML = `<span class="dot" style="background:${s.color}"></span>${esc(s.name)}${on ? " · 当前" : ""}`;
    if (on) { c.style.opacity = ".75"; c.style.cursor = "default"; }
    else { c.onclick = () => switchTo(s.id); }
    box.appendChild(c);
  });
}
async function switchTo(id) {
  try {
    const s = await jpost(`/api/sessions/${curSession}/switch`, { streamer_id: id });
    setCurStreamer(s.current_streamer_id, s.current_streamer_name);
  } catch (e) { alert("切换失败：" + fmtErr(e)); }
}

// 点主播名字直接改名（无需去主播页维护）
async function renameStreamer(id) {
  if (!id) return;
  const cur = rosterMap[id] || {};
  const name = prompt("修改主播名字：", cur.name || nameOf(id));
  if (name === null) return;
  const n = name.trim();
  if (!n || n === cur.name) return;
  try {
    await api(`/api/streamers/${id}`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: n, note: cur.note || "", color: cur.color || "" }),
    });
    await loadRoster();
    if (curStreamerId === id) setCurStreamer(id, n); else renderSwitchTargets();
    if ($("#view-streamers").classList.contains("active")) loadStreamerTab();
  } catch (e) { alert("改名失败：" + fmtErr(e)); }
}
function showSetup() {
  stopSessionPolling();
  resetLive();
  curSession = null; draftId = null;
  $("#link").value = ""; $("#manual").value = ""; $("#setupHint").textContent = "";
  $("#setupCard").style.display = "block"; $("#liveBar").style.display = "none";
  loadRoster().then(renderLineupPicker);
  loadRooms();
  loadReady();
}

function renderReady(payload) {
  systemReady = Client.readinessState(payload);
  const box = $("#readyStatus"); if (!box) return systemReady.ready;
  const entries = Object.values((payload && payload.components) || {});
  const chips = entries.map(item => `<span class="tag" style="margin-right:5px;background:${item.ok ? "var(--ok)" : "var(--red)"};color:#fff">${esc(item.label)} ${item.ok ? "正常" : esc(item.message)}</span>`).join("");
  box.innerHTML = `${chips}<div style="margin-top:6px;color:${systemReady.ready ? "var(--ok)" : "var(--red)"}">${systemReady.ready ? "系统已就绪，可以新建场次" : "系统未就绪，暂不能新建场次：" + esc(systemReady.message)}</div>`;
  if (!curSession) updateStartAvailability();
  return systemReady.ready;
}
async function loadReady() {
  try { return renderReady(await api("/api/ready")); }
  catch (error) {
    systemReady = { ready: false, blockers: [fmtErr(error)], message: fmtErr(error) };
    const box = $("#readyStatus"); if (box) box.textContent = "系统状态读取失败，暂不能新建场次：" + systemReady.message;
    if (!curSession) updateStartAvailability();
    return false;
  }
}
async function ensureReady() {
  const ok = await loadReady();
  if (!ok) $("#setupHint").textContent = "无法新建场次：" + (systemReady && systemReady.message || "系统未就绪");
  return ok;
}

function duplicateHint(session) {
  const hint = $("#setupHint");
  hint.innerHTML = `同一直播链接已有未结束场次 #${Number(session.id)}，请进入原场次。 `
    + `<button class="btn ghost sm" id="enterExistingSession">进入原场次</button>`;
  const button = $("#enterExistingSession");
  if (button) button.onclick = () => watchLive(Number(session.id));
}

async function findExistingSession(link) {
  const sessions = await api("/api/sessions").catch(() => []);
  return Client.findDuplicateSession(sessions, link);
}

// 粘贴/输入抖音链接后自动建场存档（标题自动生成；同时收录常用直播间）
async function autoSaveDraft() {
  const link = Client.normalizeLiveLink($("#link").value);
  if (!link || draftId || $("#setupCard").style.display === "none") return;
  const allowUnassigned = !!($("#allowUnassigned") && $("#allowUnassigned").checked);
  if (roster.length && !lineup.length && !allowUnassigned) { $("#setupHint").textContent = "请选择本场主播"; return; }
  if (!(await ensureReady())) return;
  $("#link").value = link;
  $("#btnStart").disabled = true;
  $("#setupHint").textContent = "解析并自动存档中…";
  try {
    const existing = await findExistingSession(link);
    if (existing) { draftId = existing.id; duplicateHint(existing); return existing; }
    const sess = await createGate.run(`create:${link}`, () =>
      jpost("/api/sessions", { title: "", share_link: link, streamer_ids: lineup, allow_unassigned: allowUnassigned }));
    draftId = sess.id;
    $("#setupHint").textContent = `✓ 已识别：${esc(sess.title) || ("场次 #" + sess.id)}${sess.error_msg ? "（" + esc(sess.error_msg) + "）" : ""}，可点开播`;
    loadRooms();
    return sess;
  } catch (e) {
    $("#setupHint").textContent = e.status === 409
      ? "该直播链接已有未结束场次，请进入原场次。"
      : "自动存档失败：" + fmtErr(e);
    return null;
  } finally { updateStartAvailability(); }
}
$("#link").addEventListener("blur", autoSaveDraft);

// 正在录制中的场次（含无人值守自动录）→ 提供实时观看入口，避免用户再手动开同直播间抢流
async function checkLiveNow() {
  const bar = $("#liveNowBar"); if (!bar) return;
  try {
    const ss = await api("/api/sessions");
    const lives = (ss || []).filter(s => s.status === "live");
    if (!lives.length) { bar.innerHTML = ""; return; }
    bar.innerHTML = `<div class="card" style="border-color:var(--gold);margin-bottom:14px">
      <div class="hint" style="margin-bottom:8px">🔴 正在录制中（含无人值守自动录制），直接观看实时画面即可，无需重复开播（重复开会和录制抢流导致拉流失败）：</div>
      <div class="row">${lives.map(s => `<button class="btn gold sm" data-watch="${s.id}">▶ 观看 ${esc(s.title || ("场次#" + s.id))}</button>`).join("")}</div></div>`;
    bar.querySelectorAll("[data-watch]").forEach(b => b.onclick = () => watchLive(Number(b.dataset.watch)));
  } catch (e) { bar.innerHTML = ""; }
}
// 进入某个已在录制的场次的实时视图（只加一路预览，不重复开录）
async function watchLive(id) {
  resetLive();
  try {
    const sess = await api(`/api/sessions/${id}`);
    curSession = sess.id; draftId = null;
    enterLiveUI(sess);
    openWs(sess.id);
    startPlayer(sess.id);
  } catch (e) { alert("进入失败：" + fmtErr(e)); }
}

// 常用直播间列表：点一下填链接并建草稿，无需重复粘贴
async function loadRooms() {
  const box = $("#roomList"), wrap = $("#roomListWrap");
  if (!box || !wrap) return;
  const rooms = await api("/api/rooms").catch(() => []);
  if ($("#roomCount")) $("#roomCount").textContent = String(rooms.length);
  if (!rooms.length) { wrap.style.display = "none"; return; }
  wrap.style.display = "block";
  box.innerHTML = rooms.map(r =>
    `<div class="roomchip" data-link="${esc(r.share_link)}"><span class="rn">${esc(r.title) || "直播间"}</span><span class="ri">房间 ${esc(r.room_id) || "-"}</span>`
    + `<span class="auto-toggle" data-auto="${r.id}" title="开播自动录制，无需打开网页" style="margin-left:8px;padding:2px 8px;border-radius:10px;font-size:11px;cursor:pointer;background:${r.auto_record ? "rgba(232,179,57,.18)" : "var(--surface3)"};color:${r.auto_record ? "var(--gold)" : "var(--dim)"}">${r.auto_record ? "🔴 自动录制中" : "⚪ 自动录制"}</span><button class="btn danger sm" data-room-delete="${r.id}" ${r.in_use || r.auto_record ? 'disabled title="使用中的房间不能删除"' : ''}>删除</button></div>`).join("");
  box.querySelectorAll(".roomchip").forEach(el => el.onclick = (e) => {
    if (e.target.closest("[data-auto]")) return;   // 点开关不触发填链接开播
    $("#link").value = el.dataset.link || ""; draftId = null; autoSaveDraft();
  });
  box.querySelectorAll("[data-auto]").forEach(t => t.onclick = async (e) => {
    e.stopPropagation();
    const id = t.dataset.auto, on = t.textContent.includes("中");
    try {
      await fetch(withBase(`/api/rooms/${id}/auto`), {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ auto_record: !on, streamer_ids: lineup }),
      });
      loadRooms();
    } catch (err) { alert("设置失败：" + (err.detail || JSON.stringify(err))); }
  });
  box.querySelectorAll("[data-room-delete]").forEach(button => button.onclick = async event => {
    event.stopPropagation();
    if (button.disabled || button.dataset.pending === "1" || !confirm("确认删除这个常用直播间？历史场次不会被删除。")) return;
    button.dataset.pending = "1"; button.disabled = true; button.textContent = "删除中…";
    try { await api(`/api/rooms/${button.dataset.roomDelete}`, { method: "DELETE" }); await loadRooms(); }
    catch (error) { alert("删除失败：" + fmtErr(error)); button.disabled = false; button.textContent = "删除"; button.dataset.pending = ""; }
  });
}

// 系统设置（成本/采集）：读写运行时配置
async function loadSettings() {
  if (!$("#setRecord")) return;
  try {
    const s = await api("/api/settings");
    $("#setRecord").value = s.record_video ? "video" : "audio";
    $("#setEmotion").value = s.auto_emotion ? "1" : "0";
    $("#setRetention").value = s.media_retention_days ?? 0;
  } catch (e) { }
}
$("#btnSaveSettings") && ($("#btnSaveSettings").onclick = async () => {
  const body = {
    record_video: $("#setRecord").value === "video",
    auto_emotion: $("#setEmotion").value === "1",
    media_retention_days: parseInt($("#setRetention").value || "0", 10),
  };
  try {
    await fetch(withBase("/api/settings"), {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    $("#setHint").textContent = "✓ 已保存（对下一场采集/收播生效）";
  } catch (e) { $("#setHint").textContent = "保存失败"; }
});

// 侧边栏收缩（记住状态）
(() => {
  const sb = document.querySelector(".sidebar");
  if (!sb) return;
  try { if (localStorage.getItem("fupan_sb_collapsed") === "1") sb.classList.add("collapsed"); } catch (e) { }
  const btn = $("#sbToggle");
  if (btn) btn.onclick = () => {
    sb.classList.toggle("collapsed");
    try { localStorage.setItem("fupan_sb_collapsed", sb.classList.contains("collapsed") ? "1" : "0"); } catch (e) { }
  };
})();

$("#btnStart").onclick = async () => {
  if (createGate.has("start")) { $("#setupHint").textContent = "正在提交，请稍候…"; return; }
  if (!(await ensureReady())) { updateStartAvailability(); return; }
  const allowUnassigned = !!($("#allowUnassigned") && $("#allowUnassigned").checked);
  if (roster.length && !lineup.length && !allowUnassigned) { $("#setupHint").textContent = "请选择本场主播"; updateStartAvailability(); return; }
  if (!lineup.length && allowUnassigned && !confirm("确认本场暂不指定主播？主播端将无法按账号查看归属复盘。")) return;
  $("#btnStart").disabled = true; $("#setupHint").textContent = "";
  await createGate.run("start", async () => { try {
    let sess;
    const manualUrl = $("#manual").value.trim();
    const link = Client.normalizeLiveLink($("#link").value);
    $("#link").value = link;
    const createKey = `create:${link}`;
    if (!manualUrl && createGate.has(createKey)) {
      sess = await createGate.run(createKey, () => Promise.resolve(null));
      if (sess) draftId = sess.id;
    }
    const existing = await findExistingSession(link);
    if (existing && Number(existing.id) !== Number(draftId)) {
      duplicateHint(existing); return;
    }
    if (draftId) {
      sess = await api(`/api/sessions/${draftId}`);   // 无手动地址时复用已自动存档的草稿
    } else {
      // 填了手动兜底地址就必须新建带该地址的会话，不能复用不含手动地址的旧草稿
      sess = await jpost("/api/sessions", {
        title: "", share_link: link,
        manual_stream_url: $("#manual").value, streamer_ids: lineup, allow_unassigned: allowUnassigned,
      });
    }
    if (!sess) return;
    const selectedRuleIds = [...document.querySelectorAll('#sessionScriptPicker input[data-rule-id]:checked')]
      .map(x => Number(x.dataset.ruleId));
    const forbiddenRuleIds = [...document.querySelectorAll('#sessionForbiddenPicker input[data-compliance-rule-id]:checked')].map(x => Number(x.dataset.complianceRuleId));
    const exemptionRuleIds = [...document.querySelectorAll('#sessionExemptionPicker input[data-compliance-rule-id]:checked')].map(x => Number(x.dataset.complianceRuleId));
    if (selectedRuleIds.length && !lineup.length) throw new Error("选择规定话术前，请先选择本场主播");
    for (const streamerId of lineup) {
      if (selectedRuleIds.length) await jpost(`/api/sessions/${sess.id}/script-compliance/plan`, {
        streamer_id: Number(streamerId), rule_ids: selectedRuleIds,
      });
    }
    resetLive();
    curSession = sess.id; draftId = null;
    enterLiveUI(sess);
    openWs(sess.id);
    // 以开播一刻选中的排班为准（草稿建于粘链接时往往还没选主播），并立即代入当前主播
    const started = await jpost(`/api/sessions/${sess.id}/start`, {
      streamer_ids: lineup,
      compliance_rule_ids: [...forbiddenRuleIds, ...exemptionRuleIds],
    });
    if (started) { setCurStreamer(started.current_streamer_id, started.current_streamer_name); loadSessionRuleSnapshot(sess.id); }
    startPlayer(sess.id);
  } catch (e) {
    $("#setupHint").textContent = e.status === 409
      ? "该直播链接已有未结束场次，请进入原场次。"
      : "开播失败：" + fmtErr(e);
  } finally { updateStartAvailability(); } });
};
$("#btnStop").onclick = async () => {
  if (!curSession) await syncActiveSession();
  if (!curSession || stopGate.has(`stop:${curSession}`)) return;
  $("#btnStop").disabled = true;
  const b = $("#liveStatus"); b.className = "badge processing"; b.innerHTML = "正在停止"; b.title = "正在结束直播";
  destroyPlayer();                                  // 立刻停画面（视频是独立代理，不停就一直在动）
  $("#playerPh").style.display = "flex"; $("#playerPh").textContent = "正在结束采集…画面已停止";
  try {
    await stopGate.run(`stop:${curSession}`, () => jpost(`/api/sessions/${curSession}/stop`));
    startSessionPolling(curSession);
  } catch (e) {
    $("#setupHint").textContent = "收尾失败：" + fmtErr(e);
    $("#btnStop").disabled = false;
  }
};
$("#btnNewSession").onclick = () => { lineup = []; if ($("#allowUnassigned")) $("#allowUnassigned").checked = false; showSetup(); };
$("#btnExport").onclick = () => {
  if (!curSession) { alert("还没有场次"); return; }
  go(`/api/sessions/${curSession}/export.md`);
};
$("#btnExportWord").onclick = () => {
  if (!curSession) { alert("还没有场次"); return; }
  go(`/api/sessions/${curSession}/export.docx`);
};
// 主播切换：见 renderSwitchTargets / switchTo（点主播列表里的人即切换）

// 过滤器
document.querySelectorAll("#playerQuality .qbtn").forEach(b => b.onclick = () => {
  videoQuality = b.dataset.q;
  try { localStorage.setItem("fupan_video_quality", videoQuality); } catch (e) {}
  updateQualityButtons();
  if (curSession) {
    $("#playerPh").style.display = "flex";
    $("#playerPh").textContent = "正在切换画质……";
    startPlayer(curSession);
  }
});
updateQualityButtons();

$("#flagFilters").querySelectorAll(".fbtn").forEach(b => b.onclick = () => {
  $("#flagFilters").querySelectorAll(".fbtn").forEach(x => x.classList.remove("on"));
  b.classList.add("on"); curFilter = b.dataset.f;
  $("#streamFlag").querySelectorAll(".line").forEach(applyFilter);
});
function applyFilter(d) { d.style.display = (curFilter === "all" || d.dataset.f === curFilter) ? "" : "none"; }

function setStatus(s, session = null) {
  const map = { starting: "正在启动", parsing: "正在启动", live: "正在采集", stopping: "正在停止", processing: "正在整理音频", finalizing: "正在生成复盘", done: "复盘完成", error: "采集异常", created: "准备就绪" };
  const b = $("#liveStatus"); b.className = "badge " + s;
  b.innerHTML = (s === "live" ? '<span class="livedot"></span>' : "") + (session ? sessionStatusLabel(session) : (map[s] || s));
  if (Client.isTerminal(s)) { $("#btnStop").disabled = true; stopSessionPolling(); }
}

function stopSessionPolling() {
  if (sessionPollTimer) clearTimeout(sessionPollTimer);
  sessionPollTimer = null;
}
function startSessionPolling(id) {
  stopSessionPolling();
  const poll = async () => {
    if (curSession !== id) return stopSessionPolling();
    try {
      const session = await api(`/api/sessions/${id}`);
      setStatus(session.status, session);
      if (Client.isTerminal(session.status)) {
        $("#playerPh").style.display = "flex";
        $("#playerPh").textContent = session.status === "done" ? "✅ 已结束" : ("⚠️ " + fmtErr(session.error_msg, "收尾失败"));
        return stopSessionPolling();
      }
    } catch (e) {
      $("#setupHint").textContent = "状态查询失败：" + fmtErr(e);
    }
    sessionPollTimer = setTimeout(poll, 1500);
  };
  sessionPollTimer = setTimeout(poll, 500);
}

function resetLive() {
  sentCount = ocrCount = flagCount = 0; partialEl = null; lastStreamerId = null; curFilter = "seg";
  ["#streamSent", "#streamOcr", "#streamFlag"].forEach(s => $(s).innerHTML = "");
  $("#cntSent").textContent = "0 句"; $("#cntOcr").textContent = "0";
  $("#summaryWrap").style.display = "none"; $("#summaryGrid").innerHTML = "";
  $("#screenToast").classList.remove("show");
  $("#playerBadge").style.display = "none";
  $("#playerPh").style.display = "flex"; $("#playerPh").textContent = "正在连接直播画面……";
  $("#flagFilters").querySelectorAll(".fbtn").forEach(x => x.classList.toggle("on", x.dataset.f === curFilter));
  destroyPlayer();
  wsWantOpen = false;
  if (ws) { ws.close(); ws = null; }
}

function openWs(id) {
  wsWantOpen = true;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}${BASE_PATH}/api/sessions/${id}/ws`);
  ws.onmessage = (m) => { try { handleEvent(JSON.parse(m.data)); } catch (e) { console.warn("[ws] 坏消息已忽略", e); } };
  ws.onopen = () => { wsRetry = 0; };
  ws.onclose = () => {
    if (!wsWantOpen || curSession !== id) return;   // 主动关/已切场次：不重连
    wsRetry = Math.min(wsRetry + 1, 6);
    // 连续断开时顺手核一次登录态：平台已退出/撤权时 api() 会整页跳走，不再空转重连
    if (wsRetry >= 2) api("/api/me").catch(() => { });
    setTimeout(() => { if (wsWantOpen && curSession === id) openWs(id); }, 500 * wsRetry);
  };
  ws.onerror = () => { try { ws.close(); } catch (e) { } };
}

let toastTimer = null;
function showToast(ev) {
  const o = $("#screenToast");
  o.innerHTML = `⚠️ 画面点评 · ${esc(ev.category)}：${esc(ev.comment)}`;
  o.classList.add("show"); clearTimeout(toastTimer);
  toastTimer = setTimeout(() => o.classList.remove("show"), 6000);
}

function whoMeta(ev) {
  return `<span class="dot" style="background:${colorOf(ev.streamer_id)}"></span>`
    + `<span class="who" style="color:${colorOf(ev.streamer_id)}">${esc(ev.streamer || "")}</span>`;
}

function handleEvent(ev) {
  const sb = $("#streamSent");
  if (ev.type === "error") {                 // 系统错误：不再弹层打扰，仅记录（结束态在画面占位处说明）
    window._lastErrMsg = ev.message || "";
    console.warn("[capture:error]", ev.message || "");
    return;
  }
  if (ev.type === "status") {
    if (ev.status === "asr_warn" || ev.status === "asr_reconnect") {
      // ASR 抖动/重连属系统内部信息：静默重试，不再弹提示
      console.warn("[asr]", ev.error || ev.status);
      return;
    }
    setStatus(ev.status);
    if (ev.status === "done" || ev.status === "error") {
      stopSessionPolling();
      // 采集真正结束：停画面、断 WS（不再来新字幕/弹幕）、清残留字幕，给明确提示
      wsWantOpen = false;
      destroyPlayer();
      if (ws) { try { ws.close(); } catch (e) { } ws = null; }
      if (partialEl) { partialEl.remove(); partialEl = null; }
      $("#playerPh").style.display = "flex";
      $("#playerPh").textContent = ev.status === "done"
        ? "✅ 已结束"
        : ("⚠️ " + fmtErr(window._lastErrMsg, "采集出错已停止"));
    }
    return;
  }
  if (ev.type === "streamer") {
    setCurStreamer(ev.streamer_id, ev.name);
    if (lastStreamerId !== null && lastStreamerId !== ev.streamer_id) {
      const d = el("div"); d.style.textAlign = "center"; d.style.padding = "4px";
      d.innerHTML = `<span class="hint">—— 切换主播：${esc(ev.name)} ——</span>`;
      sb.appendChild(d); scrollDown(sb);
    }
    lastStreamerId = ev.streamer_id; return;
  }
  if (ev.type === "partial") {
    if (!partialEl) { partialEl = el("div", "line partial"); sb.appendChild(partialEl); }
    partialEl.textContent = "… " + ev.text; scrollDown(sb); return;
  }
  if (ev.type === "sentence") {
    if (partialEl) { partialEl.remove(); partialEl = null; }
    const d = el("div", "line");
    d.innerHTML = `<div class="meta">${whoMeta(ev)}<span class="t">${mmss(ev.start_ms)}</span></div>${esc(ev.text)}`;
    sb.appendChild(d); scrollDown(sb); $("#cntSent").textContent = `${++sentCount} 句`; return;
  }
  if (ev.type === "ocr") {
    const b = $("#streamOcr");
    const d = el("div", "line");
    d.innerHTML = `<div class="meta"><span class="t">${mmss(ev.t_ms)}</span>`
      + `<button class="btn sm" data-reply style="padding:2px 9px;font-size:11px">回应</button></div>`
      + `${esc(ev.text)}<div class="ans" style="display:none;margin-top:5px;color:var(--green);font-size:12.5px;background:var(--surface3);padding:6px 9px;border-radius:7px"></div>`;
    d.querySelector("[data-reply]").onclick = async (e) => {
      const btn = e.target; btn.disabled = true; btn.textContent = "生成中…";
      const q = ev.text.includes("：") ? ev.text.split("：").slice(1).join("：") : ev.text;
      try {
        const r = await jpost(`/api/sessions/${curSession}/danmaku/reply`, { question: q });
        const ans = d.querySelector(".ans");
        ans.style.display = "block"; ans.innerHTML = `💡 建议回应：${esc(r.answer)}`;
        btn.remove();
      } catch (err) { btn.disabled = false; btn.textContent = "回应"; alert("生成失败：" + fmtErr(err)); }
    };
    b.appendChild(d); scrollDown(b); $("#cntOcr").textContent = ++ocrCount; return;
  }
  if (ev.type === "flag") {
    const b = $("#streamFlag"); const isScreen = ev.source === "screen";
    const d = el("div", "line fl " + (ev.severity === "error" ? "error " : "") + (isScreen ? "screen" : ""));
    d.dataset.f = isScreen ? "screen" : "speech";
    d.innerHTML = `<div class="meta"><span class="tag cat">${esc(ev.category)}</span>`
      + `${isScreen ? '<span class="tag src">画面</span>' : ""}${whoMeta(ev)}<span class="t">${mmss(ev.start_ms)}</span></div>`
      + `${esc(ev.comment)}<div class="quote">原话：${esc(ev.quote)}</div>`;
    applyFilter(d); b.appendChild(d); scrollDown(b); ++flagCount;
    if (isScreen) showToast(ev); return;
  }
  if (ev.type === "segment") {
    const b = $("#streamFlag");
    const d = el("div", "line fl seg"); d.dataset.f = "seg";
    const issues = (ev.issues || []).map(i => `<span class="pill">${esc(i)}</span>`).join("");
    const stype = ev.seg_type ? ` <span class="tag" style="background:var(--surface3);color:var(--dim)">${esc(ev.seg_type)}</span>` : "";
    const hl = (ev.highlights || []).length ? `<div class="hint" style="margin-top:5px">✅ 讲到：${ev.highlights.map(esc).join("、")}</div>` : "";
    const gaps = (ev.gaps || []).length ? `<div class="hint" style="margin-top:3px;color:var(--warn)">⚠️ 漏/不到位：${ev.gaps.map(esc).join("、")}</div>` : "";
    const pace = ev.pace_review ? `<div class="hint" style="margin-top:3px">⏩ 语速：${esc(ev.pace_review)}</div>` : "";
    const aud = ev.audience ? `<div class="hint" style="margin-top:3px">💬 公屏反应：${esc(ev.audience)}</div>` : "";
    d.innerHTML = `<div class="meta"><b>段级点评 ${ev.score != null ? ev.score + "分" : ""}</b>${stype}${sceneHTML(ev.scene_label)}${ev.asr_emotion_label ? emotionHTML(ev.asr_emotion_label, true) : emotionHTML(ev.emotion)}${llmBadge(ev.llm, ev.is_fallback, ev.llm_requested)}${whoMeta(ev)}</div>`
      + `${esc(ev.comment)}${emotionMatchHTML(ev.emotion_match)}${hl}${gaps}${pace}${aud}${loopHTML(ev.loop)}${rhythmHTML(ev.metrics)}<div style="margin-top:5px">${issues}</div>`;
    applyFilter(d); b.appendChild(d); scrollDown(b); return;
  }
  if (ev.type === "predict") {
    const b = $("#streamFlag"); if (!b) return;
    const d = el("div", "line fl");
    d.style.borderLeftColor = "var(--gold)";
    d.style.background = "rgba(232,179,57,.10)";
    d.innerHTML = `<div class="meta"><b style="color:var(--gold)">⚡ 实时预警 · 概率 ${ev.prob}%</b>${whoMeta(ev)}</div>${esc(ev.tip)}`;
    b.appendChild(d); scrollDown(b); return;
  }
  if (ev.type === "summary") {
    addSummaryCard(ev);
    const b = $("#streamFlag");
    // 整体点评块
    const d = el("div", "line fl seg"); d.dataset.f = "summary";
    d.innerHTML = `<div class="meta"><b>整体点评 · ${esc(ev.streamer || "")} ${ev.overall_score ?? ""}分</b>${llmBadge(ev.llm, ev.is_fallback, ev.llm_requested)}</div>`
      + `${esc(ev.summary || "")}`;
    applyFilter(d); b.appendChild(d);
    // 话术框架块（整场结束才有）
    if (ev.structure && ev.structure.arc && ev.structure.arc.length) {
      const f = el("div", "line fl"); f.dataset.f = "framework";
      f.innerHTML = `<div class="meta"><b>话术框架 · ${esc(ev.streamer || "")}</b>${llmBadge(ev.llm, ev.is_fallback, ev.llm_requested)}</div>`
        + structureHTML(ev.structure, ev.structure_review);
      applyFilter(f); b.appendChild(f);
    }
    // 情绪与语境块
    if (ev.emotion || ev.emotion_context || ev.emotion_overview) {
      const em = el("div", "line fl"); em.dataset.f = "emotion";
      em.innerHTML = `<div class="meta"><b>情绪与语境 · ${esc(ev.streamer || "")}</b></div>`
        + (ev.emotion ? `<div class="hint" style="margin-top:4px">🎭 整体语气：${esc(ev.emotion)}</div>` : "")
        + emotionOverviewHTML(ev.emotion_overview)
        + (ev.emotion_context ? `<p style="margin-top:6px">${esc(ev.emotion_context)}</p>` : "");
      applyFilter(em); b.appendChild(em);
    }
    scrollDown(b);
  }
}

function parseJSON(s) { try { return s ? JSON.parse(s) : {}; } catch (e) { return {}; } }

// 节奏指标 chips（段级；问题项标黄）。m 来自 segment 事件或 critique.data.metrics
function rhythmHTML(m) {
  if (!m || !m.sentences) return "";
  const chip = (k, v, bad) => `<span class="rchip${bad ? " bad" : ""}"><i>${k}</i>${esc(String(v))}</span>`;
  const wpm = m.wpm || 0, rep = Math.round((m.repeat_ratio || 0) * 100);
  const out = [
    chip("语速", wpm + " 字/分", false),
    chip("冷场", (m.cold_opens || 0) + " 次", (m.cold_opens || 0) > 0),
    chip("停顿", (m.avg_pause_ms || 0) + "ms"),
    chip("重复", rep + "%", rep > 30),
    chip("互动", (m.ask_count || 0), (m.ask_count || 0) < 2),
    chip("弹幕", m.danmaku || 0),
  ];
  if (m.energy_cv != null) out.push(chip("起伏", m.energy_cv, m.energy_cv < 0.25));
  if (m.products && m.products.length)
    out.push(chip("主讲", m.products.join("/") + " " + Math.round((m.dominant_share || 0) * 100) + "%"));
  return `<div class="rhythm">${out.join("")}</div>`;
}

// 语气情绪标签：LLM 判定=紫色🎭；Qwen 语音识别=金色🎯（更权威）
function emotionHTML(e, isAsr) {
  if (!e) return "";
  return isAsr
    ? ` <span class="tag" style="background:var(--gold);color:#1a1407" title="Qwen 语音识别">🎯 ${esc(e)}</span>`
    : ` <span class="tag" style="background:var(--purple);color:#fff">🎭 ${esc(e)}</span>`;
}

// R8：同一时间段的真实情绪与话术/弹幕语境是否一致。
function emotionMatchHTML(m) {
  if (!m) return "";
  const ok = !!m.matched;
  const color = ok ? "var(--ok)" : "var(--warn)";
  const title = ok ? "情绪与语境匹配" : "情绪与语境不匹配";
  const reason = m.reason ? `<div style="margin-top:3px">${esc(m.reason)}</div>` : "";
  const suggestion = !ok && m.suggestion
    ? `<div style="margin-top:3px"><b>建议：</b>${esc(m.suggestion)}</div>` : "";
  return `<div class="hint" style="margin-top:6px;border-left:3px solid ${color};padding-left:8px">`
    + `<b style="color:${color}">${ok ? "✓" : "⚠"} ${title}</b>`
    + ` · 实际 ${esc(m.actual_emotion || "-")} / 应有 ${esc(m.expected_emotion || "-")}`
    + `${reason}${suggestion}</div>`;
}

// 话术场景标签（青色）：本段处在开场/讲款/逼单…哪个环节
function sceneHTML(s) {
  return s ? ` <span class="tag" style="background:var(--cyan);color:#04222a">🎬 ${esc(s)}</span>` : "";
}

// 语调（真实音高 F0）：均值Hz + 起伏标签
function toneHTML(pt) {
  if (!pt || !pt.pitch_hz) return "";
  return ` <span class="tag" style="background:var(--surface3);color:var(--dim)" title="真实基频 F0（Praat）">🎵 ${esc(pt.label || "")} ${pt.pitch_hz}Hz</span>`;
}

// 段级讲款闭环链：2026-10-04 起不再显示——它按关键词判（得说出「卖点」「下单」这些词才算），
// 杨雪 43 段只认出 3 段讲了卖点，和按意思逐句核对的结果（96% 讲全）打架。讲款缺什么看深度复盘。
function loopHTML(lp) {
  return "";
  if (!lp || !lp.steps) return "";
  const chips = lp.steps.map(s =>
    `<span class="lchip ${s.ok ? "ok" : "miss"}">${s.ok ? "✓" : "✗"} ${esc(s.step)}</span>`).join("");
  return `<div class="loopbar"><span class="faint" style="font-size:11px">🔁 讲款闭环 ${lp.pct}%</span>${chips}</div>`;
}

// 整场语音情绪分布（Qwen 识别，报告卡）
function emotionOverviewHTML(ov) {
  if (!ov || !ov.dist || !ov.dist.length) return "";
  const items = ov.dist.map(d => `${esc(d.label)} ${d.pct}%`).join(" · ");
  return `<div class="hint" style="margin-top:8px">🎯 语音情绪分布：${items}${ov.dominant ? `（主导：${esc(ov.dominant)}）` : ""}</div>`;
}

// 讲款常缺环节：取深度复盘逐段判出来的「缺失结构」（2026-10-04 起替代按关键词算的「平均完整度 %」）
function v2MissingHTML(v2) {
  const items = (((v2 || {}).chapters || {}).items) || [];
  const cnt = {};
  items.forEach(c => (c.missing || []).forEach(m => {
    const k = String(m || "").trim();
    if (k) cnt[k] = (cnt[k] || 0) + 1;
  }));
  const top = Object.entries(cnt).sort((a, b) => b[1] - a[1]).slice(0, 4);
  if (!top.length) return "";
  return `<div class="hint" style="margin-top:8px">🔁 讲款常缺（深度复盘逐段判）：${top.map(([k, n]) => `${esc(k)}（${n} 段）`).join("、")}</div>`;
}

// 整场讲款闭环诊断（报告卡）：关键词算的，2026-10-04 起不再用（见 loopHTML 的说明）
function loopSummaryHTML(lp) {
  if (!lp || !lp.product_segments) return "";
  const miss = (lp.often_missing || []).length
    ? `常缺环节：${lp.often_missing.map(esc).join("、")}` : "六环基本齐全";
  return `<div class="hint" style="margin-top:8px">🔁 讲款闭环：${lp.product_segments} 个讲款段 · 平均完整度 ${lp.avg_completeness}% · ${miss}</div>`;
}

// LLM 来源标记：严格按单次调用的 is_fallback 标记，不能按系统当前配置反推历史结果
function llmBadge(llm, isFallback, requested) {
  if (llm === "demo") return ' <span class="tag" style="background:var(--surface3);color:var(--dim)">虚构演示</span>';
  const meta = Client.fallbackMeta(isFallback === undefined && llm === "mock" ? true : isFallback);
  if (meta.key === "fallback") {
    const detail = requested ? `${requested}${llm ? ` → ${llm}` : ""}` : "本次模型调用使用了降级结果";
    return ` <span class="tag" style="background:var(--warn);color:#000" title="${esc(detail)}">降级结果，仅供参考</span>`;
  }
  if (meta.key === "real") return ` <span class="tag" style="background:var(--surface3);color:var(--dim)">${llm ? esc(llm) + " · " : ""}模型点评</span>`;
  return "";
}

// 整场环节结构（话术逻辑）：弧线 + 占比 + 结构诊断 + LLM 逻辑点评
function structureHTML(st, review) {
  if (!st || !st.arc || !st.arc.length) return "";
  const arcNodes = a => a.map(s => `<span class="arc-node">${esc(s)}</span>`).join('<span class="arc-sep">→</span>');
  const dist = (st.distribution || []).map(d => `<span class="rchip"><i>${esc(d.scene)}</i>${d.pct}%</span>`).join("");
  const diags = (st.diagnostics || []).map(x => `<li>${esc(x)}</li>`).join("");
  const f = st.frame;
  let frameHTML = "";
  if (f && f.main && f.main.length) {
    // 去噪后的主框架：出现最多的循环模式 + 变体（完整原始序列折叠，按需展开）
    const variants = (f.variants || []).map(v =>
      `<div class="rhythm" style="margin-top:4px"><span class="faint" style="font-size:11px;margin-right:6px">变体×${v.count}</span>${arcNodes(v.seq)}</div>`).join("");
    frameHTML = `<div style="margin-bottom:8px">
      ${f.prelude && f.prelude.length ? `<div class="rhythm" style="margin-bottom:4px"><span class="faint" style="font-size:11px;margin-right:6px">开场</span>${arcNodes(f.prelude)}</div>` : ""}
      <div class="arc">${arcNodes(f.main)}<span class="tag" style="margin-left:8px">主框架 ×${f.main_count}/${f.loops}循环</span></div>
      ${variants}
      ${f.denoised ? `<div class="faint" style="font-size:11px;margin-top:4px">已过滤 ${f.denoised} 个碎片环节噪声</div>` : ""}
      <details style="margin-top:6px"><summary class="faint" style="font-size:11px;cursor:pointer">完整环节序列</summary><div class="arc" style="margin-top:4px">${arcNodes(st.arc)}</div></details>
    </div>`;
  }
  return `<div class="struct">
    <div class="hint" style="margin-bottom:6px">🧭 整场环节结构${f && f.main && f.main.length ? "（去噪框架提炼）" : ""}</div>
    ${frameHTML || `<div class="arc">${arcNodes(st.arc)}</div>`}
    ${dist ? `<div class="rhythm" style="margin-top:8px">${dist}</div>` : ""}
    ${review ? `<p style="margin:8px 0 4px">${esc(review)}</p>` : ""}
    ${diags ? `<ul class="tight" style="margin-top:4px">${diags}</ul>` : ""}
  </div>`;
}

// 错失清单（观众高价值弹幕没接住的），带时间可跳转
function missedHTML(missed) {
  if (!missed || !missed.length) return "";
  const items = missed.map(m =>
    `<li data-seek="${m.start_ms || 0}" style="cursor:pointer"><span class="t" style="margin-right:6px">${wallT(detailState.startedAt, m.start_ms || 0)}</span>「${esc(m.q || "")}」<span class="tag cat" style="margin:0 4px">${esc(m.intent || "")}</span>${esc(m.why || "")}</li>`).join("");
  return `<div class="struct" style="margin-top:8px"><div class="hint" style="margin-bottom:4px">🎯 错失清单（观众问了但没接住）</div><ul class="tight">${items}</ul></div>`;
}

// 结论证据链：总结的判断指到具体段（点击跳播放）
function examplesHTML(ex) {
  if (!ex || (!ex.best && !ex.worst)) return "";
  const row = (label, arr, color) => (arr || []).map(e =>
    `<li data-seek="${e.start_ms}" style="cursor:pointer"><span class="tag" style="background:${color};color:#000;margin-right:6px">${label} ${e.score}分</span><span class="t" style="margin-right:6px">${wallT(detailState.startedAt, e.start_ms)}</span>${e.scene_label ? esc(e.scene_label) + "：" : ""}${esc(e.comment || "")}</li>`).join("");
  const items = row("高光", ex.best, "var(--ok, #7ee787)") + row("短板", ex.worst, "var(--warn, #f0b429)");
  if (!items) return "";
  return `<div class="struct" style="margin-top:8px"><div class="hint" style="margin-bottom:4px">🔍 结论对应案例（点击回放）</div><ul class="tight">${items}</ul></div>`;
}

function experienceSourcesHTML(items) {
  const refs = Array.isArray(items) ? items.filter(i => i && typeof i === "object") : [];
  if (!refs.length) return "";
  const usedCount = refs.filter(i => i.used).length;
  const rows = refs.map(i => {
    const status = i.used
      ? '<span style="color:var(--green);font-weight:700">已引用</span>'
      : '<span class="faint">已召回</span>';
    const scene = i.scene ? ` · 场景 ${esc(i.scene)}` : "";
    const excerpt = i.excerpt ? `<div class="faint" style="margin-top:3px">${esc(i.excerpt)}</div>` : "";
    return `<li>${status} <code>${esc(i.citation || i.ref || "")}</code> · ${esc(i.source || "未标明来源")}${scene}${excerpt}</li>`;
  }).join("");
  const result = usedCount
    ? `实际引用 ${usedCount} 条 / 共召回 ${refs.length} 条`
    : `共召回 ${refs.length} 条，本次结论未显式引用`;
  return `<details class="struct" style="margin-top:8px">
    <summary style="cursor:pointer">📚 个性化经验来源 · ${esc(result)}</summary>
    <ul class="tight" style="margin-top:8px">${rows}</ul>
  </details>`;
}

// 复盘报告卡片（直播与历史共用）。d: {overall_score|score, dimensions, summary, top_issues, action_items, experience_sources, structure, structure_review}
function r3StatusMeta(status) {
  return ({
    effective: ["本场验证有效", "good"], ineffective: ["本场验证无效", "bad"], mixed: ["数据有好有坏", "warn"],
    insufficient_data: ["罗盘证据不足", "warn"], not_followed: ["执行一致性不足", "warn"], unverified: ["未声明执行", ""],
    planned: ["已登记执行计划", "info"], analyzing: ["分析中", "info"], ready: ["分析完成", "good"], failed: ["分析失败", "bad"],
    matched: ["符合", "good"], missing: ["缺失", "bad"], too_long: ["讲得过长", "warn"], too_short: ["讲得过短", "warn"],
    wrong_content: ["内容偏离", "bad"], out_of_order: ["顺序不对", "bad"],
  }[status] || [status || "待判断", ""]);
}

function r3CompactHTML(r3) {
  if (!r3 || !r3.available) return "";
  const cmp = asObj(r3.comparison), val = asObj(r3.validation), tpl = asObj(r3.template);
  const problems = objList(cmp.differences).filter(x => x.status !== "matched").length + objList(cmp.extra_issues).length;
  const [vLabel, vClass] = r3StatusMeta(val.status || r3.status);
  const match = cmp.match_score != null ? `${cmp.match_score} 分` : "待对比";
  return `<div class="r3-compact">
    <div class="r3-compact-head"><b>⏱ R3 话术节奏</b><span class="r3-badge info">匹配度 ${esc(match)}</span>
      <span class="r3-badge ${vClass}">${esc(val.verdict || vLabel)}</span></div>
    <div>${tpl.title ? `对比《${esc(tpl.title)}》 · ` : ""}${problems} 个节奏点需调整${cmp.summary ? `：${esc(cmp.summary)}` : ""}</div>
    ${r3.error_msg ? `<div class="faint" style="color:var(--warn);margin-top:4px">${esc(r3.error_msg)}</div>` : ""}
  </div>`;
}

// 复盘报告卡片（直播与历史共用）。d: {overall_score|score, dimensions, summary, top_issues, action_items, structure, structure_review}
function reportCardHTML(name, color, d) {
  const score = d.overall_score ?? d.score ?? "-";
  const dims = Object.entries(d.dimensions || {}).map(([k, v]) =>
    `<div class="dim"><div class="v">${v}</div><div class="k">${esc(k)}</div></div>`).join("");
  const itemText = i => (i && typeof i === "object") ? (i.action || i.text || i.issue || i.title || i.content || "") : (i || "");
  const tops = (d.top_issues || []).map(i => `<li>${esc(itemText(i))}</li>`).join("");
  const acts = (d.action_items || []).map(i => {
    const src = (i && typeof i === "object" && i.source) ? ` <span class="faint" style="font-size:11px">${esc(i.source)}</span>` : "";
    return `<li>${esc(itemText(i))}${src}</li>`;
  }).join("");
  return `<div class="sum-head"><span class="dot" style="width:12px;height:12px;background:${color || "#5a6376"}"></span>
    <b>${esc(name || "未指定主播")}</b>${llmBadge(d.llm, d.is_fallback, d.llm_requested)}<span class="sp"></span><span class="score-big">${score}</span><span class="faint" style="margin-left:6px">综合</span></div>
    ${dims ? `<div class="dims">${dims}</div>` : ""}${d.emotion ? `<div class="hint" style="margin:6px 0">🎭 整体语气情绪：${esc(d.emotion)}</div>` : ""}<p>${esc(d.summary || "")}</p>${experienceSourcesHTML(d.experience_sources)}
    ${emotionOverviewHTML(d.emotion_overview)}${d.emotion_context ? `<div class="struct" style="margin-top:8px"><div class="hint" style="margin-bottom:4px">💠 情绪与话术语境</div><p style="margin:0">${esc(d.emotion_context)}</p></div>` : ""}${structureHTML(d.structure, d.structure_review)}${v2MissingHTML(d.v2)}${missedHTML(d.missed_summary)}${examplesHTML(d.examples)}${r3CompactHTML(d.rhythm_analysis)}
    <div class="row" style="align-items:flex-start">
      <div style="flex:1"><div class="hint">Top 问题</div><ul class="tight">${tops || '<li class="faint">无</li>'}</ul></div>
      <div style="flex:1"><div class="hint">改进行动项</div><ul class="tight">${acts || '<li class="faint">无</li>'}</ul></div>
    </div>`;
}
function addSummaryCard(ev) {
  $("#summaryWrap").style.display = "block";
  const c = el("div", "sum-card");
  c.innerHTML = reportCardHTML(ev.streamer, colorOf(ev.streamer_id), ev);
  $("#summaryGrid").appendChild(c);
}

// ===================== 历史复盘 =====================
const STATUS_LABEL = { draft: "待启动", starting: "正在启动采集", live: "正在采集中", stopping: "正在停止采集", finalizing: "正在生成整场复盘报告", processing: "采集已停止，正在整理音频", done: "整场复盘已完成", error: "采集异常", created: "准备就绪", parsing: "正在启动采集" };
function sessionStatusLabel(session) {
  if (session && session.error_code === "SUMMARY_GENERATION_FAILED") return "采集完成，但整场总结生成失败";
  if (session && session.error_code === "FINALIZING_AUDIO") return "正在整理音频";
  if (session && session.error_code === "GENERATING_SUMMARY") return "正在生成复盘";
  return STATUS_LABEL[session && session.status] || esc(session && session.status || "未知状态");
}
function fmtDur(a, b) {
  if (!a) return "-";
  if (!b) return "进行中";
  const sec = Math.max(0, Math.round((new Date(b) - new Date(a)) / 1000));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}
let _histCache = [];
let _calY = null, _calM = null;   // 当前日历年 / 月(0基)
let _histMode = "cal";            // cal | list
let _selDay = null;               // 选中的日期 key(YYYY-MM-DD)

$("#btnRefreshHist").onclick = loadHistory;
if ($("#histSearch")) $("#histSearch").oninput = () => { if (modalOpen()) closeModal(); renderHistoryView(); };
if ($("#histToggle")) $("#histToggle").querySelectorAll("button").forEach(b => b.onclick = () => {
  closeModal();
  _histMode = b.dataset.hv;
  $("#histToggle").querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b));
  $("#histCalCard").style.display = _histMode === "cal" ? "" : "none";
  $("#histListCard").style.display = _histMode === "list" ? "" : "none";
  renderHistoryView();
});
if ($("#calPrev")) $("#calPrev").onclick = () => shiftMonth(-1);
if ($("#calNext")) $("#calNext").onclick = () => shiftMonth(1);
if ($("#calToday")) $("#calToday").onclick = () => { closeModal(); const d = new Date(); _calY = d.getFullYear(); _calM = d.getMonth(); renderCalendar(); };

function shiftMonth(delta) {
  closeModal();
  let m = _calM + delta, y = _calY;
  if (m < 0) { m = 11; y--; } else if (m > 11) { m = 0; y++; }
  _calY = y; _calM = m; renderCalendar();
}

// ---------- 详情/清单弹窗（modal） ----------
function modalOpen() { const m = $("#modal"); return !!(m && m.style.display !== "none"); }
function openModal(html) {
  const b = $("#modalBody"); if (!b) return;
  if (html != null) b.innerHTML = html;
  const m = $("#modal"); m.style.display = "flex"; m.scrollTop = 0;
  document.body.classList.add("modal-open");
}
function stopDetailPlayer() {   // 停掉详情里的音/视频，避免关窗后后台继续出声/下流量
  const pl = $("#detailPlayer");
  if (pl) { try { pl.pause(); pl.removeAttribute("src"); pl.load(); } catch (e) { } }
  const dk = document.getElementById("v2dockPlayer");   // v2 播放坞同样停掉
  if (dk) { try { dk.pause(); dk.removeAttribute("src"); dk.load(); } catch (e) { } }
  if (typeof _v2HlsDispose === "function") { try { _v2HlsDispose(); } catch (e) { } _v2HlsDispose = null; }
}
function closeModal() {
  const _mb = $("#modalBody"); if (_mb && _mb.parentElement) _mb.parentElement.classList.remove("v2skin");
  _selDay = null;
  const g = $("#calGrid"); if (g) g.querySelectorAll(".cal-cell.sel").forEach(c => c.classList.remove("sel"));
  const m = $("#modal"); if (!m || m.style.display === "none") return;
  stopDetailPlayer();
  m.style.display = "none";
  const b = $("#modalBody"); if (b) b.innerHTML = "";
  _detailOpenSeq += 1;
  detailState = { id: null, streamers: [] };
  document.body.classList.remove("modal-open");
}
if ($("#modalClose")) $("#modalClose").onclick = closeModal;
if ($("#modal")) $("#modal").addEventListener("click", e => { if (e.target === $("#modal")) closeModal(); });
document.addEventListener("keydown", e => { if (e.key === "Escape" && modalOpen()) closeModal(); });

async function loadHistory() {
  await loadRoster();
  _histCache = await api("/api/sessions");
  if (_calY === null) {   // 首次：定位到最近有场次的月份，否则今天
    const latest = _histCache.map(sessDayKey).filter(Boolean).sort().pop();
    const base = latest ? new Date(latest + "T00:00:00") : new Date();
    _calY = base.getFullYear(); _calM = base.getMonth();
  }
  renderHistoryView();
  scheduleListPolling("history", _histCache);
}

function renderHistoryView() {
  if ($("#histCount")) $("#histCount").textContent = _filteredSessions().length;
  if (_histMode === "list") renderHistoryList();
  else renderCalendar();
}

// 归属日期 = 开播日，缺则建场日；直接截字符串，避免时区把日期挪一天
function sessDayKey(s) {
  const t = s.started_at || s.created_at || "";
  return t ? t.slice(0, 10) : "";
}
function _matchQ(s, q) {
  return !q || (s.title || "").toLowerCase().includes(q)
    || (s.lineup || []).some(x => (x.name || "").toLowerCase().includes(q));
}
function _filteredSessions() {
  const q = (($("#histSearch") && $("#histSearch").value) || "").trim().toLowerCase();
  // 轮询试探失败等 error 场次是系统噪音，前端兜底再滤一层（后端默认已不返回）
  return _histCache.filter(s => s.status !== "error").filter(s => _matchQ(s, q));
}

function renderCalendar() {
  const grid = $("#calGrid"); if (!grid) return;
  $("#calLabel").textContent = `${_calY}年${_calM + 1}月`;
  const byDay = {};
  _filteredSessions().forEach(s => { const k = sessDayKey(s); if (k) (byDay[k] = byDay[k] || []).push(s); });
  Object.values(byDay).forEach(arr =>
    arr.sort((a, b) => (a.started_at || a.created_at || "").localeCompare(b.started_at || b.created_at || "")));

  const pad = n => String(n).padStart(2, "0");
  const first = new Date(_calY, _calM, 1);
  const offset = (first.getDay() + 6) % 7;   // 周一为第一列
  const now = new Date();
  const todayKey = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;

  const WD = ["一", "二", "三", "四", "五", "六", "日"];
  let html = WD.map((w, i) => `<div class="cal-wd${i >= 5 ? " we" : ""}">${w}</div>`).join("");

  for (let i = 0; i < 42; i++) {
    const cd = new Date(_calY, _calM, 1 - offset + i);
    const y = cd.getFullYear(), m = cd.getMonth(), d = cd.getDate();
    const key = `${y}-${pad(m + 1)}-${pad(d)}`;
    const list = byDay[key] || [];
    const cls = ["cal-cell"];
    if (m !== _calM) cls.push("out");
    if (list.length) cls.push("has");
    if (key === todayKey) cls.push("today");
    if (key === _selDay) cls.push("sel");
    let pills = list.slice(0, 3).map(s =>
      `<div class="cal-pill s-${s.status}" data-pill="${s.id}" data-day="${key}" title="${esc(s.title) || "（未命名）"}">${esc(s.title) || "（未命名）"}</div>`).join("");
    if (list.length > 3) pills += `<div class="cal-more" data-day="${key}">+${list.length - 3} 更多</div>`;
    const cnt = list.length ? `<span class="cnt">${list.length}场</span>` : "";
    html += `<div class="${cls.join(" ")}" data-day="${key}"><div class="cal-dnum"><span class="d">${d}</span>${cnt}</div>${pills}</div>`;
  }
  grid.innerHTML = html;

  grid.querySelectorAll("[data-pill]").forEach(p => p.onclick = (e) => {
    e.stopPropagation();
    selectDayHighlight(p.dataset.day);
    loadDetail(+p.dataset.pill, p.dataset.day);
  });
  grid.querySelectorAll(".cal-cell.has").forEach(c => c.onclick = () => openDayModal(c.dataset.day));
  grid.querySelectorAll(".cal-more").forEach(mo => mo.onclick = (e) => { e.stopPropagation(); openDayModal(mo.dataset.day); });
}

function selectDayHighlight(key) {
  _selDay = key;
  const g = $("#calGrid");
  if (g) g.querySelectorAll(".cal-cell").forEach(c => c.classList.toggle("sel", c.dataset.day === key));
}

// 点某天 → 弹出当天场次清单（modal）
function openDayModal(key) {
  selectDayHighlight(key);
  const list = _filteredSessions().filter(s => sessDayKey(s) === key)
    .sort((a, b) => (b.started_at || b.created_at || "").localeCompare(a.started_at || a.created_at || ""));
  if (!list.length) { closeModal(); return; }
  const rows = list.map(s => {
    const streamers = (s.lineup || []).map(x =>
      `<span style="white-space:nowrap"><span class="dot" style="background:${x.color}"></span> ${esc(x.name)}</span>`).join("　")
      || '<span class="faint">未指定</span>';
    const tm = (s.started_at || s.created_at || "").slice(11, 16) || "-";
    return `<tr>
      <td class="faint" style="width:52px;font-variant-numeric:tabular-nums">${tm}</td>
      <td><div>${esc(s.title) || "（未命名）"}</div><div class="faint" style="font-size:11px">#${s.id}${s.room_id ? " · 房间 " + esc(s.room_id) : ""}</div></td>
      <td style="font-size:12.5px">${streamers}</td>
      <td class="faint" style="width:74px;font-variant-numeric:tabular-nums">${fmtDur(s.started_at, s.ended_at)}</td>
      <td style="width:78px"><span class="badge ${s.status}">${sessionStatusLabel(s)}</span></td>
      <td style="width:140px"><div class="row" style="gap:6px">
        <button class="btn sm" data-act="detail" data-id="${s.id}">详情</button>
        <button class="btn danger sm" data-act="del" data-id="${s.id}">删除</button>
      </div></td></tr>`;
  }).join("");
  const nice = key.replace(/-/g, "/");
  openModal(`<div class="card-head" style="padding-right:44px"><h3 style="font-size:15px;color:var(--text)">📅 ${nice} · ${list.length} 场复盘</h3></div>
    <div class="card pad0" style="overflow-x:auto"><table class="daytbl"><tbody>${rows}</tbody></table></div>
    <div class="faint" style="font-size:12px;margin-top:10px">点「详情」进入单场复盘，可在其中选择整场 / 分主播导出</div>`);
  $("#modalBody").querySelectorAll("[data-act]").forEach(b => b.onclick = () => histAction(b.dataset.act, b.dataset.id, key));
}

// 组装导出 URL（整场 / 分主播全部 / 指定主播）× (Word/MD/音频)
function doExport(id, fmt) {
  const scope = ($("#expScope") || {}).value || "full";
  let qs = "";
  if (scope === "by_role") qs = "?by_role=1";
  else if (scope.startsWith("sid:")) qs = `?streamer_id=${scope.slice(4)}`;
  const hint = $("#expHint");
  if (fmt === "audio") {
    if (scope === "by_role") { if (hint) hint.textContent = "音频不支持「分主播全部」，请选整场或某位主播"; return; }
    if (hint) hint.textContent = scope.startsWith("sid:") ? "正在裁剪该主播音频，稍候会开始下载…" : "";
    go(`/api/sessions/${id}/audio/download${qs}`); return;
  }
  if (hint) hint.textContent = "";
  go(`/api/sessions/${id}/export.${fmt}${qs}`);
}

function renderHistoryList() {
  const rows = _filteredSessions();
  const tb = $("#histRows"); if (!tb) return; tb.innerHTML = "";
  if (!rows.length) {
    tb.innerHTML = `<tr><td colspan="6"><div class="empty">${_histCache.length ? "无匹配场次" : "还没有复盘场次"}</div></td></tr>`;
    return;
  }
  rows.forEach(s => {
    const tr = el("tr");
    const streamers = (s.lineup || []).map(x =>
      `<span style="white-space:nowrap"><span class="dot" style="background:${x.color}"></span> ${esc(x.name)}</span>`).join("　")
      || '<span class="faint">未指定</span>';
    const audioState = Client.audioPresentation(s);
    const t = s.started_at ? s.started_at.replace("T", " ").slice(5, 16) : "-";
    tr.innerHTML = `
      <td class="faint" style="font-variant-numeric:tabular-nums">${t}</td>
      <td><div>${esc(s.title) || "（未命名）"}</div><div class="faint" style="font-size:11px">#${s.id}${s.room_id ? " · 房间 " + esc(s.room_id) : ""}</div></td>
      <td style="font-size:12.5px">${streamers}</td>
      <td class="faint" style="font-variant-numeric:tabular-nums">${fmtDur(s.started_at, s.ended_at)}</td>
      <td><span class="badge ${s.status}">${sessionStatusLabel(s)}</span></td>
      <td><div class="row" style="gap:6px">
        <button class="btn ghost sm" data-act="detail" data-id="${s.id}">详情</button>
        <button class="btn ghost sm" data-act="exportWord" data-id="${s.id}">Word</button>
        <button class="btn ghost sm" data-act="export" data-id="${s.id}">MD</button>
        ${audioState.playable ? `<button class="btn ghost sm" data-act="dl" data-id="${s.id}">播放录音</button>` : `<span class="faint" title="${esc(audioState.reason)}">录音不可用${audioState.reason ? "：" + esc(audioState.reason) : ""}</span>`}
        <button class="btn danger sm" data-act="del" data-id="${s.id}">删除</button>
      </div></td>`;
    tb.appendChild(tr);
  });
  tb.querySelectorAll("[data-act]").forEach(b => b.onclick = () => histAction(b.dataset.act, b.dataset.id));
}
async function histAction(act, id, dayKey) {
  if (act === "export") { go(`/api/sessions/${id}/export.md`); return; }
  if (act === "exportWord") { go(`/api/sessions/${id}/export.docx`); return; }
  if (act === "dl") { go(`/api/sessions/${id}/audio/download`); return; }
  if (act === "del") {
    if (!confirm(`确认彻底删除场次 #${id}？\n转写、点评、录音将一并删除，不可恢复。`)) return;
    await api(`/api/sessions/${id}`, { method: "DELETE" });
    await loadHistory();
    if (dayKey) {                       // 从某天弹窗删除：刷新该天弹窗，删空则关闭
      if (_filteredSessions().some(s => sessDayKey(s) === dayKey)) openDayModal(dayKey);
      else closeModal();
    }
    return;
  }
  if (act === "detail") loadDetail(id, dayKey || null);
}

let detailState = { id: null, streamers: [] };
let _detailOpenSeq = 0;
// 总览 KPI:罗盘整场汇总(支付金额/成交件数/在线峰值/累计观看/新增粉丝)。
// 整场级数据与主播切换无关,算一次缓存,切主播重渲染直接回填。
async function fillDetailKpis(id) {
  const put = (k, v) => { const el = document.querySelector(`#dtKpis .kpi[data-k="${k}"] b`); if (el) el.textContent = v; };
  const apply = (k) => { put("gmv", k.gmv); put("orders", k.orders); put("peak", k.peak); put("views", k.views); put("fans", k.fans); };
  if (detailState.kpis && detailState.kpisFor === id) { apply(detailState.kpis); return; }
  const num = (v) => { const n = parseFloat(String(v ?? "").replace(/[¥,，\s]/g, "")); return Number.isFinite(n) ? n : null; };
  try {
    const pts = await api(`/api/sessions/${id}/compass`);
    if (!Array.isArray(pts) || !pts.length) { apply({ gmv: "—", orders: "—", peak: "—", views: "—", fans: "—" }); return; }
    let peak = 0;
    for (const p of pts) { const v = num(p.data && p.data["实时在线人数"]); if (v != null && v > peak) peak = v; }
    const last = pts[pts.length - 1].data || {};
    const k = {
      gmv: (() => { const v = num(last["直播间用户支付金额"]); return v != null ? `¥${v.toLocaleString("zh-CN")}` : "—"; })(),
      orders: last["成交件数"] ?? "—",
      peak: peak || "—",
      views: last["累计观看人数"] ?? "—",
      fans: last["新增粉丝数"] ?? "—",
    };
    detailState.kpis = k; detailState.kpisFor = id;
    apply(k);
  } catch { apply({ gmv: "—", orders: "—", peak: "—", views: "—", fans: "—" }); }
}

async function loadDetail(id, backKey) {
  const openSeq = ++_detailOpenSeq;
  const [streamers, _all] = await Promise.all([
    api(`/api/sessions/${id}/streamers`),
    _histCache && _histCache.length ? Promise.resolve(_histCache) : api("/api/sessions").catch(() => []),
  ]);
  if (openSeq !== _detailOpenSeq) return;
  const _meta = (_all || []).find(x => String(x.id) === String(id)) || {};
  detailState = { id, streamers, startedAt: _meta.started_at || null, endedAt: _meta.ended_at || null, status: _meta.status || "", audio: _meta.audio || null, errorMsg: _meta.error_msg || "" };
  v2JobsAdopt(id, streamers);   // 别的设备正在给这一场生成 v2：这里也显示「生成中」（只在刚拿到最新状态时认领）
  const detailAudio = Client.audioPresentation({ status: detailState.status, audio: detailState.audio, error_msg: detailState.errorMsg });
  const subs = [`<span class="chip on" data-sid="">全部主播</span>`]
    .concat(streamers.map(s => `<span class="chip" data-sid="${s.streamer_id}"><span class="dot" style="background:${s.color || "#5a6376"}"></span>${esc(s.name)} · ${s.flag_count}问题</span>`));
  const expOpts = [`<option value="full">整场</option>`, `<option value="by_role">分主播（全部·分章）</option>`]
    .concat(streamers.filter(s => s.streamer_id)
      .map(s => `<option value="sid:${s.streamer_id}">仅 ${esc(s.name)}</option>`)).join("");
  const back = backKey ? `<button class="btn ghost sm" id="mBack">← 当天列表</button>` : "";
  // 页面重设计（2026-07-10）：先结论后细节——三个视图页签（总览/回放对照/归因分析），
  // 低频操作（导出/情绪精算/分工/罗盘接入）折叠收纳，告别 4000px 长流水。
  const _mb0 = $("#modalBody"); if (_mb0 && _mb0.parentElement) _mb0.parentElement.classList.add("v2skin");  // .modal-box 挂浅色皮肤(拍板原型样式)
  openModal(`<div class="card-head" style="padding-right:44px;gap:10px">
      <h3 style="font-size:15px;color:var(--text)">场次 #${id} 复盘详情</h3>${back}</div>
    <div class="seg-sub-tabs" id="subTabs" style="margin-bottom:8px">${subs.join("")}</div>
    <div id="bindingBox"></div>
    <div class="dt-tabs" id="dtViewTabs">
      <span class="dt-tab on" data-pane="overview">📊 总览</span>
      <span class="dt-tab" data-pane="replay">🎬 整场回放</span>
      <span class="dt-tab" data-pane="v2tone">🎭 语气语调</span>
      <span class="dt-tab" data-pane="v2struct">🧭 话术结构</span>
      <span class="dt-tab" data-pane="r3rhythm">⏱ 话术节奏</span>
      <span class="dt-tab" data-pane="prescribed">📋 规定话术执行</span>
      <span class="dt-tab" data-pane="v2qc">🛡️ 质检</span>
      <span class="dt-tab" data-pane="v2dm">💬 弹幕回复</span>
      <span class="dt-tab" data-pane="attr">📈 罗盘分析</span>
      <details class="dt-tools"><summary>⚙️ 导出与工具</summary>
        <div class="exportbar" style="margin-top:8px">
          <span class="lb">导出范围</span>
          <select id="expScope">${expOpts}</select>
          <button class="btn ghost sm" data-exp="docx">Word</button>
          <button class="btn ghost sm" data-exp="md">Markdown</button>
          <button class="btn ghost sm" data-exp="audio"${detailAudio.playable ? "" : ' disabled title="录音不可用"'}>音频</button>
          <button class="btn ghost sm" id="btnEmo">🎯 情绪精算</button>
          <button class="btn ghost sm" id="btnSummaryRegen">重新生成整场总结</button>
          <span class="lb" id="expHint" style="color:var(--faint);font-weight:400"></span>
          <span class="lb faint" id="emoHint" style="font-size:12px"></span>
        </div>
      </details>
    </div>
    <div id="detailBody"></div>
    <div class="dt-pane" data-pane="attr" hidden>
      <div id="danmakuBox"></div>
      <div id="attrBox"></div>
      <div id="compassReviewBox"></div>
      <div id="experienceBox"></div>
      <details class="rolebox"><summary>👥 主播 / 场控分工</summary>
        <div id="roleBody" class="faint" style="font-size:12.5px;padding:8px 2px">展开加载…</div></details>
      <details class="rolebox" id="compassBox"><summary>📊 抖音罗盘接入（实时数据 / 成交归因）</summary>
        <div id="compassBody" class="faint" style="font-size:12.5px;padding:8px 2px">展开加载…</div></details>
    </div>`);
  const box = $("#modalBody");
  if (backKey && $("#mBack")) $("#mBack").onclick = () => openDayModal(backKey);
  box.querySelectorAll("[data-exp]").forEach(b => b.onclick = () => doExport(id, b.dataset.exp));
  if ($("#btnEmo")) $("#btnEmo").onclick = () => recomputeEmotion(id);
  if ($("#btnSummaryRegen")) $("#btnSummaryRegen").onclick = async () => {
    const btn = $("#btnSummaryRegen");
    if (detailState.summaryGenerating || !confirm("确认重新生成整场总结？生成失败时会保留当前总结。")) return;
    detailState.summaryGenerating = true; btn.disabled = true; btn.textContent = "整场总结生成中…";
    const selected = ($("#subTabs .chip.on") || { dataset: {} }).dataset.sid;
    try {
      await api(`/api/sessions/${id}/summary/regenerate`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ streamer_id: selected ? Number(selected) : null }),
      });
      detailState.streamers = await api(`/api/sessions/${id}/streamers`);
      await renderDetailFor(id, selected || null);
    } catch (e) {
      alert("整场总结生成失败：" + fmtErr(e));
    } finally {
      if (detailState.id === id) detailState.summaryGenerating = false;
      if (document.body.contains(btn)) { btn.disabled = false; btn.textContent = "重新生成整场总结"; }
    }
  };
  box.querySelectorAll("#subTabs .chip").forEach(c => c.onclick = () => {
    box.querySelectorAll("#subTabs .chip").forEach(x => x.classList.remove("on"));
    c.classList.add("on");
    renderDetailFor(id, c.dataset.sid || null);
  });
  // 视图页签切换：pane 分布在 detailBody 内(总览/回放)与外部(归因),统一按 data-pane 切
  detailState.viewPane = detailState.viewPane || "overview";
  const applyPane = () => {
    box.querySelectorAll(".dt-pane").forEach(pn => { pn.hidden = pn.dataset.pane !== detailState.viewPane; });
    box.querySelectorAll("#dtViewTabs .dt-tab").forEach(t => t.classList.toggle("on", t.dataset.pane === detailState.viewPane));
  };
  detailState.applyPane = applyPane;
  box.querySelectorAll("#dtViewTabs .dt-tab").forEach(t => t.onclick = () => {
    detailState.viewPane = t.dataset.pane;
    (detailState.applyPane || applyPane)();   // 用包装版:切页签同时把播放坞搬进当前页
  });
  applyPane();
  const rb = box.querySelector("details.rolebox:not(#compassBox)");
  if (rb) rb.addEventListener("toggle", () => { if (rb.open) loadRoles(id); }, { once: true });
  const cb = $("#compassBox");
  if (cb) cb.addEventListener("toggle", () => { if (cb.open) loadCompass(id); });
  loadDanmakuSignals(id);
  loadAttribution(id);
  loadCompassReview(id);
  loadExperience(id);
  loadBindingStatus(id);
  loadCompass(id);
  renderDetailFor(id, null);
}
async function loadBindingStatus(id) {
  const box = $("#bindingBox"); if (!box) return;
  const st = await api(`/api/sessions/${id}/binding-status`).catch(() => null); if (!st) return;
  if (st.complete) { box.innerHTML = '<div class="hint" style="color:#62c98d;margin-bottom:8px">✓ R1 主播归属完整</div>'; return; }
  const people = await api("/api/streamers").catch(() => []);
  box.innerHTML = `<div class="card" style="border-color:#b88438;margin-bottom:10px"><b>R1 归属待修复</b> · 未归属 ${st.total_unassigned} 条
    <select id="bindSid" style="margin-left:8px">${people.map(x => `<option value="${x.id}">${esc(x.name)}</option>`).join("")}</select>
    <button class="btn sm" id="bindFix">补齐空归属</button></div>`;
  $("#bindFix").onclick = async () => {
    const streamer_id = Number($("#bindSid").value); if (!streamer_id) return;
    await api(`/api/sessions/${id}/bind-streamer`, { method:"PUT",
      headers:{"Content-Type":"application/json"}, body:JSON.stringify({streamer_id, force:false}) });
    await loadBindingStatus(id); renderDetailFor(id, null);
  };
}
async function loadAttribution(id) {
  const box = $("#attrBox"); if (!box) return;
  const r = await api(`/api/sessions/${id}/attribution`).catch(() => null);
  if (!r || !r.total) { box.innerHTML = ""; return; }   // 无成交信号则不占位
  const pct = r.total ? Math.round(r.driven * 100 / r.total) : 0;
  const stat = `
    <div class="attr-stats">
      <div class="attr-stat"><b>${r.total}</b><span>成交信号</span></div>
      <div class="attr-stat ok"><b>${r.driven}</b><span>话术驱动 ${pct}%</span></div>
      <div class="attr-stat dim"><b>${r.natural}</b><span>自然 / 引导缺失</span></div>
      <div class="attr-stat"><b>${esc(r.dominant || "—")}</b><span>主导类型</span></div>
    </div>`;
  const rows = (r.events || []).slice(0, 30).map(e => `
    <div class="attr-ev">
      <span class="t">${wallT(detailState.startedAt, e.at)}</span>
      <span class="attr-badge ${e.verdict === "话术驱动" ? "ok" : "dim"}">${esc(e.verdict)}</span>
      <span class="attr-src">${esc(e.source)}</span>
      <span class="attr-scene">${esc(e.scene || "—")}</span>
      <span class="attr-note">${esc(e.note)}</span>
    </div>`).join("");
  const tips = (r.tips || []).map(t => `<li>${esc(t)}</li>`).join("");
  box.innerHTML = `<div class="card" style="margin-top:12px">
    <div class="card-head"><h3>💰 成交归因</h3><span class="faint" style="font-size:12px">每个成交信号回溯当时话术场景</span></div>
    ${stat}
    ${tips ? `<ul class="tight" style="margin:8px 0 4px">${tips}</ul>` : ""}
    ${rows ? `<div class="attr-list">${rows}</div>` : ""}</div>`;
}
async function loadCompass(id) {
  const el = $("#compassBody"); if (!el) return;
  if (compassGate.has(`compass-${id}`)) return;
  el.innerHTML = '<span class="tag" style="background:var(--gold);color:#1a1407">导入中</span> <span class="faint">正在查询真实导入状态…</span>';
  await compassGate.run(`compass-${id}`, async () => {
    try {
      const [setup, pts] = await Promise.all([
        api("/api/compass/setup"),
        api(`/api/sessions/${id}/compass`).catch(() => []),
      ]);
      if (!document.body.contains(el)) return;
      const status = Client.compassStatus(setup, false, false);
      const rows = (pts || []).slice(-15).map(p =>
        `<div style="padding:2px 0"><span class="t" style="margin-right:6px">${wallT(detailState.startedAt, p.start_ms)}</span>${Object.entries(p.data || {}).map(([k, v]) => esc(k) + ":" + esc(String(v))).join(" · ") || "（空）"}</div>`).join("");
      const color = status.key === "success" ? "var(--ok)" : "var(--surface3)";
      el.innerHTML = `<div class="row" style="margin-bottom:6px"><span class="tag" style="background:${color};color:var(--text)">${status.label}</span><b>记录数量 ${(pts || []).length}</b><button class="btn ghost sm" id="compassRefreshBtn">刷新状态</button></div>
        <div>① 把下面整段拷贝，在浏览器新建一个书签、网址粘贴为它；② 打开抖音罗盘/大屏页面点该书签，即每 30 秒上报一次：</div>
        <textarea readonly style="width:100%;height:64px;margin:6px 0;font-size:11px;background:var(--surface3);color:var(--text);border:1px solid var(--border);border-radius:8px" onclick="this.select()">${esc(setup.bookmarklet || "")}</textarea>
        <div class="faint">${rows ? "最近导入记录：" : "尚未导入"}</div>${rows}
        <div class="faint" style="margin-top:6px">最近一次导入：${esc(setup.last_point_at || "尚未导入")}</div>`;
      const btn = el.querySelector("#compassRefreshBtn");
      if (btn) btn.onclick = () => loadCompass(id);
    } catch (error) {
      if (!document.body.contains(el)) return;
      const status = Client.compassStatus(null, false, true);
      el.innerHTML = `<span class="tag" style="background:var(--red);color:#fff">${status.label}</span> <span class="faint">${esc(fmtErr(error))}</span> <button class="btn ghost sm" id="compassRetryBtn">重试</button>`;
      const btn = el.querySelector("#compassRetryBtn"); if (btn) btn.onclick = () => loadCompass(id);
    }
  });
}
// 罗盘数据变化归因：数据涨跌 ← 当时动作（收尾时算好，读缓存）
async function loadCompassReview(id) {
  const box = $("#compassReviewBox"); if (!box) return;
  const r = await api(`/api/sessions/${id}/compass-review`).catch(() => null);
  if (!r || !r.available || !(r.changes || []).length) { box.innerHTML = ""; return; }
  const rows = (r.changes || []).map(c => {
    const up = c.direction === "up";
    const cls = up ? (c.verdict === "动作驱动" ? "ok" : "info") : "warn";
    return `<div class="attr-row">
      <span class="rchip ${cls}"><i>${up ? "📈" : "📉"}</i>${esc(c.effect || "")}</span>
      <span class="t" style="margin:0 6px">${mmss(c.at_ms)}</span>
      <span class="faint">${esc(c.scene_label || "")}</span>
      ${c.verdict ? ` · <b>${esc(c.verdict)}</b>` : ""}
      ${c.action ? `<div class="attr-note" style="margin-top:2px">${esc(c.action)}</div>` : ""}
    </div>`;
  }).join("");
  box.innerHTML = `<div class="card" style="margin-top:12px">
    <div class="card-head"><h3>📊 罗盘数据变化归因</h3><span class="faint" style="font-size:12px">数据涨跌 ← 当时做了什么</span></div>
    ${r.summary ? `<div class="faint" style="margin:6px 0">${esc(r.summary)}</div>` : ""}
    <div class="attr-list">${rows}</div></div>`;
}
// 成功技巧沉淀：AI 从正向变化提炼，人工采纳入库供复用
async function loadExperience(id) {
  const box = $("#experienceBox"); if (!box) return;
  const r = await api(`/api/experience/session/${id}`).catch(() => null);
  const all = (r && r.tips) || [];
  const observing = all.filter(t => t.status === "observing");
  const pend = all.filter(t => t.status === "pending");
  const adopted = all.filter(t => t.status === "adopted");
  if (!observing.length && !pend.length && !adopted.length) { box.innerHTML = ""; return; }
  const percent = value => `${Math.round((Number(value) || 0) * 100)}%`;
  const card = (t, status) => {
    const threshold = t.threshold || {};
    const statusChip = status === "observing"
      ? `<span class="rchip info"><i>验证中</i></span>`
      : status === "pending"
        ? `<span class="rchip warn"><i>统计已达标 · 待人工采纳</i></span>`
        : `<span class="rchip ok"><i>已采纳</i></span>`;
    return `<div class="tip-card" style="border:1px solid var(--border);border-radius:10px;padding:8px 10px;margin:6px 0;background:var(--surface2)">
    <div class="row" style="gap:8px;align-items:center;flex-wrap:wrap">
      <span class="rchip"><i>${esc(t.scene_label || "通用")}</i></span>
      <b style="color:var(--ok,#3fbf7f)">${esc(t.effect || "")}</b>
      ${statusChip}</div>
    <div class="faint" style="margin-top:5px;line-height:1.7">
      指标：${esc(t.metric || "未标注")} · 命中 ${Number(t.matched_count) || 0}/${Number(t.change_sample_count) || 0} 次同类变化 ·
      命中率 ${percent(t.matched_rate)} · 覆盖 ${Number(t.session_count) || 0} 场 ·
      达标门槛：至少 ${Number(threshold.min_matches) || 0} 次且占比不低于 ${percent(threshold.min_rate)}
    </div>
    ${t.action ? `<div style="margin-top:4px">🎯 ${esc(t.action)}</div>` : ""}
    ${t.quote ? `<div class="quote" style="margin-top:2px">原话：${esc(t.quote)}</div>` : ""}
    ${t.template ? `<div class="faint" style="margin-top:2px">可套用：${esc(t.template)}</div>` : ""}
    ${status === "observing" ? `<div class="faint" style="margin-top:5px">验证中：继续累积同类变化样本，当前不可采纳。</div>` : ""}
    ${status === "pending" ? `<div class="row" style="gap:6px;margin-top:6px">
      <button class="btn sm" data-adopt="${t.id}">✓ 采纳入库</button>
      <button class="btn ghost sm" data-reject="${t.id}">驳回</button></div>` : ""}
  </div>`;
  };
  box.innerHTML = `<div class="card" style="margin-top:12px">
    <div class="card-head"><h3>💡 成功技巧沉淀</h3><span class="faint" style="font-size:12px">持续采样并归类相似动作；统计达标后仍需人工采纳，才会写入知识库</span></div>
    ${observing.length ? `<div class="faint" style="margin:4px 0 2px">验证中 ${observing.length}（只读）</div>${observing.map(t => card(t, "observing")).join("")}` : ""}
    ${pend.length ? `<div class="faint" style="margin:8px 0 2px">统计已达标，待人工采纳 ${pend.length}</div>${pend.map(t => card(t, "pending")).join("")}` : ""}
    ${adopted.length ? `<div class="faint" style="margin:8px 0 2px">已采纳 ${adopted.length}</div>${adopted.map(t => card(t, "adopted")).join("")}` : ""}</div>`;
  box.querySelectorAll("[data-adopt]").forEach(b => b.onclick = async () => {
    b.disabled = true; b.textContent = "采纳中…";
    try {
      const result = await api(`/api/experience/${b.dataset.adopt}/adopt`, { method: "POST" });
      if (result && result.ok === false) throw new Error(result.error || result.detail || "操作失败");
      await loadExperience(id);
    } catch (e) {
      alert("采纳失败：" + fmtErr(e));
      b.disabled = false; b.textContent = "✓ 采纳入库";
    }
  });
  box.querySelectorAll("[data-reject]").forEach(b => b.onclick = async () => {
    b.disabled = true; b.textContent = "驳回中…";
    try {
      const result = await api(`/api/experience/${b.dataset.reject}/reject`, { method: "POST" });
      if (result && result.ok === false) throw new Error(result.error || result.detail || "操作失败");
      await loadExperience(id);
    } catch (e) {
      alert("驳回失败：" + fmtErr(e));
      b.disabled = false; b.textContent = "驳回";
    }
  });
}
const _VERDICT_CLS = { "真实意向": "ok", "疑似玩笑或跟风": "warn", "咨询了解": "info" };
async function loadDanmakuSignals(id) {
  const box = $("#danmakuBox"); if (!box) return;
  const r = await api(`/api/sessions/${id}/danmaku_signals`).catch(() => null);
  if (!r || !r.total) { box.innerHTML = ""; return; }
  const chips = (r.dist || []).map(d =>
    `<span class="rchip"><i>${esc(d.label)}</i>${d.count}</span>`).join("");
  const tips = (r.attribution || []).map(t => `<li>${esc(t)}</li>`).join("");

  // AI 精析结果按弹幕时间+文本索引，购买意向条目展示判定标签与理由
  const refine = r.refine || null;
  const rfMap = new Map();
  if (refine) for (const it of (refine.items || [])) rfMap.set(it.at + "|" + it.text, it);

  // 每类信号的原文样本（购买意向排最前、默认展开）
  const groups = (r.dist || []).map(d => {
    const rows = ((r.samples || {})[d.key] || []).map(s => {
      const rf = d.key === "intent" ? rfMap.get(s.at + "|" + s.text) : null;
      const badge = rf ? `<span class="attr-badge ${_VERDICT_CLS[rf.verdict] || "dim"}">${esc(rf.verdict)}</span>` : "";
      const reason = rf && rf.reason ? `<div class="dm-reason">${esc(rf.reason)}</div>` : "";
      const ctx = rf && rf.context ? `
        <details class="dm-ctx"><summary>当时对话</summary>
          <div><b>主播：</b>${esc(rf.context.talk || "—")}</div>
          <div><b>相邻公屏：</b>${esc(rf.context.around || "—")}</div>
        </details>` : "";
      return `<div class="dm-row"><span class="t">${mmss(s.at)}</span><span class="dm-text">${esc(s.text)}</span>${badge}${reason}${ctx}</div>`;
    }).join("");
    return `<details class="dm-group"${d.key === "intent" ? " open" : ""}>
      <summary><b>${esc(d.label)}</b><span class="faint">${d.count} 条</span></summary>
      <div class="dm-list">${rows || '<div class="faint">无</div>'}</div>
    </details>`;
  }).join("");

  const rfBar = refine
    ? `<div class="dm-refine-bar">🧠 AI 意向精析：真实意向 <b class="ok">${refine.genuine ?? 0}</b> · 疑似玩笑/跟风 <b class="warn">${refine.joking ?? 0}</b> · 咨询了解 <b class="info">${refine.asking ?? 0}</b>
        <button class="btn ghost sm" id="btnDmRefine">重新精析</button></div>`
    : `<div class="dm-refine-bar">购买意向可能只是观众玩笑/跟风，点击用 AI 结合前后对话逐条判定
        <button class="btn ghost sm" id="btnDmRefine">🧠 意向精析</button><span id="dmRefineHint" class="faint"></span></div>`;

  box.innerHTML = `<div class="card" style="margin-top:12px"><div class="card-head"><h3>📣 公屏弱信号归因</h3>
      <span class="faint" style="font-size:12px">${r.danmaku_total} 条公屏 · ${r.total} 条含信号</span></div>
    <div class="rhythm">${chips || '<span class="faint">无明显信号</span>'}</div>
    ${rfBar}
    <div style="margin-top:8px">${groups}</div>
    ${tips ? `<ul class="tight" style="margin-top:8px">${tips}</ul>` : ""}</div>`;

  const btn = $("#btnDmRefine");
  if (btn) btn.onclick = async () => {
    btn.disabled = true; btn.textContent = "精析中…（结合上下文逐条判定，约需十几秒）";
    try {
      await jpost(`/api/sessions/${id}/danmaku_signals/refine`, {});
      await loadDanmakuSignals(id);
    } catch (e) {
      btn.disabled = false; btn.textContent = "🧠 意向精析";
      const h = $("#dmRefineHint"); if (h) h.textContent = "精析失败：" + fmtErr(e);
    }
  };
}

// ===================== 数据统计（跨场） =====================
if ($("#btnRefreshStats")) $("#btnRefreshStats").onclick = loadStats;
async function loadStats() {
  const box = $("#statsBody"); if (!box) return;
  box.innerHTML = '<div class="faint">加载中…</div>';
  const rows = await api("/api/stats/streamers").catch(() => []);
  if (!rows.length) { box.innerHTML = '<div class="empty">还没有可统计的整场复盘数据（先跑几场收播）</div>'; return; }
  box.innerHTML = rows.map(s => {
    const dims = Object.entries(s.dims || {}).map(([k, v]) =>
      `<div class="dim"><div class="v">${v ?? "-"}</div><div class="k">${esc(k)}</div></div>`).join("");
    const emo = (s.emotion_dist || []).map(d => `<span class="rchip"><i>${esc(d.label)}</i>${d.pct}%</span>`).join("");
    const issues = (s.top_issues || []).map(t => `<li>${esc(t)}</li>`).join("");
    return `<div class="card" style="margin-bottom:14px">
      <div class="sum-head"><span class="dot" style="width:12px;height:12px;background:${colorOf(s.streamer_id)}"></span>
        <b>${esc(s.name)}</b><span class="sp"></span><span class="score-big">${s.avg_score ?? "-"}</span>
        <span class="faint" style="margin-left:6px">场均综合 · ${s.sessions} 场</span></div>
      ${dims ? `<div class="dims">${dims}</div>` : ""}
      ${emo ? `<div class="hint" style="margin-top:8px">🎯 情绪分布</div><div class="rhythm">${emo}</div>` : ""}
      ${issues ? `<div class="hint" style="margin-top:8px">高频问题（跨场）</div><ul class="tight">${issues}</ul>` : ""}
    </div>`;
  }).join("");
}
async function recomputeEmotion(id) {
  const btn = $("#btnEmo"), hint = $("#emoHint");
  if (!confirm("用 Qwen 对每个点评段的录音重新识别情绪（7 类）？\n段数多时需 1–2 分钟，可关掉弹窗稍后再看。")) return;
  btn.disabled = true; const old = btn.textContent; btn.textContent = "识别中…";
  try {
    let dist = {}, last = null;
    for (let k = 0; k < 10; k++) {          // 每次接口跑一批，直到无新增
      last = await jpost(`/api/sessions/${id}/emotion/recompute`, {});
      dist = last.distribution || dist;
      if (hint) hint.textContent = `已识别 ${(last.skipped || 0) + (last.updated || 0)}/${last.total || 0} 段`;
      if ((last.updated || 0) === 0) break;
    }
    const summary = Object.entries(dist).map(([k, v]) => `${k} ${v}段`).join(" · ") || "无可识别段";
    if (hint) hint.textContent = `完成：${summary}`;
    renderDetailFor(id, ($("#subTabs .chip.on") || {}).dataset ? $("#subTabs .chip.on").dataset.sid || null : null);
  } catch (e) {
    if (hint) hint.textContent = "识别失败：" + fmtErr(e);
  } finally {
    btn.disabled = false; btn.textContent = old;
  }
}

async function loadRoles(id) {
  const el = $("#roleBody"); if (!el) return;
  const multi = (detailState.streamers || []).filter(s => s.streamer_id).length >= 2;
  const r = await api(`/api/sessions/${id}/roles`).catch(() => null);
  if (!r) { el.textContent = "加载失败"; return; }
  const note = multi ? "" :
    '<div class="faint" style="margin-top:6px">注：本场为单主播，场控未上麦；以下"疑似搭话"多为主播自己的短应答/互动/上链接话术。等场控上麦后此处即自动区分双线。</div>';
  if (!r.assist_count) {
    el.innerHTML = `全部计为主讲 ${r.host_count} 句，未识别到搭话话术。${note}`;
    return;
  }
  const samples = (r.samples || []).map(s =>
    `<div style="padding:3px 0"><span class="t" style="margin-right:6px">${mmss(s.start_ms)}</span>${esc(s.text)}</div>`).join("");
  el.innerHTML = `<div style="margin-bottom:6px">主讲 <b style="color:var(--text)">${r.host_count}</b> 句 · ${multi ? "场控搭话" : "疑似搭话"} <b style="color:var(--cyan)">${r.assist_count}</b> 句（${r.assist_ratio}%）</div>
    <div class="faint">${multi ? "场控搭话样例：" : "疑似搭话话术样例："}</div>${samples}${note}`;
}

function r3Duration(sec) {
  const n = Math.max(0, Math.round(Number(sec) || 0));
  return n >= 60 ? `${Math.floor(n / 60)}分${n % 60 ? n % 60 + "秒" : ""}` : `${n}秒`;
}

function r3AnalysisHTML(r3, hasMedia, canVerify = false) {
  if (!r3 || !r3.available) {
    return `<div class="r3-empty">本场还没有话术节奏分析。<br>选择一条已采纳的优秀节奏后，点击“AI 提取并对比”。</div>`;
  }
  if (r3.status === "planned") {
    const tpl = asObj(r3.template);
    return `<div class="r3-empty"><span class="r3-badge info">已登记执行计划</span><br><br>
      本场将按《${esc(tpl.title || "所选节奏模板")}》执行。收播生成复盘时会自动提取实际节奏、逐段对比并结合罗盘验证效果。</div>`;
  }
  const actual = asObj(r3.actual), cmp = asObj(r3.comparison), tpl = asObj(r3.template), val = asObj(r3.validation);
  const hasPriorResult = objList(actual.blocks).length || cmp.summary;
  if (r3.status === "failed" && !hasPriorResult) {
    const label = r3.declared_execution ? "执行计划已停止归因" : "分析失败";
    return `<div class="r3-empty" style="color:var(--red)">${label}：${esc(r3.error_msg || "请重新选择优秀节奏后再试")}</div>`;
  }
  const failureNotice = r3.error_msg
    ? `<div class="v2hint" style="border-color:var(--red);color:var(--red)">${r3.declared_execution ? "当前计划已停止归因" : "本轮分析未更新"}：${esc(r3.error_msg)}。${hasPriorResult ? "下方继续保留上次可追溯结果。" : ""}</div>`
    : "";
  const blocks = objList(actual.blocks);
  const steps = objList(tpl.steps);
  const stepMap = new Map(steps.map((s, i) => [Number(s.order || i + 1), s]));
  const blockMap = new Map(blocks.map((b, i) => [Number(b.order || i + 1), b]));
  const blockRows = blocks.map(block => `<div class="r3-block">
      <div class="r3-time">${esc(block.time_range || `${mmss(block.start_ms)}-${mmss(block.end_ms)}`)}<br>${r3Duration(block.duration_sec)}</div>
      <div><div class="r3-block-title">${esc(block.scene_label || block.scene || "未分类")} ${block.share_pct != null ? `· ${block.share_pct}%` : ""}</div>
        <div class="r3-block-copy">${esc(block.content || block.quote || "-")}</div></div>
      ${hasMedia ? `<button class="clip-btn" data-dock="${Number(block.start_ms) || 0}" data-lab="${esc(block.scene_label || "实际节奏片段")}">▶ 回放</button>` : ""}
    </div>`).join("") || '<div class="faint">本主播没有可提取的逐字稿。</div>';

  const diffRows = objList(cmp.differences).map(item => {
    const [label, cls] = r3StatusMeta(item.status);
    const step = stepMap.get(Number(item.step_order)) || {};
    const refs = (Array.isArray(item.actual_block_orders) ? item.actual_block_orders : []).map(x => blockMap.get(Number(x))).filter(Boolean);
    const actualText = refs.length
      ? refs.map(x => `${x.time_range || mmss(x.start_ms)} ${x.content || x.quote || ""}`).join("；")
      : "本场未找到对应内容";
    const replay = hasMedia && refs.length
      ? `<button class="clip-btn" data-dock="${Number(refs[0].start_ms) || 0}" data-lab="${esc(item.template_phase || step.phase || "节奏差异")}">▶ 对照回放</button>` : "";
    return `<div class="r3-diff ${cls}">
      <div class="r3-diff-head"><span class="r3-badge ${cls}">${esc(label)}</span>
        <span class="r3-diff-title">第 ${esc(item.step_order || "-")} 段 · ${esc(item.template_phase || step.phase || "模板步骤")}</span>
        <span class="sp"></span>${replay}</div>
      <div class="r3-compare">
        <div class="r3-side"><i>优秀节奏 · ${r3Duration(item.expected_duration_sec || step.duration_sec)}</i>${esc(step.content || step.action || "-")}</div>
        <div class="r3-side"><i>本场实际 · ${r3Duration(item.actual_duration_sec)}</i>${esc(actualText)}</div>
      </div>
      <div class="r3-block-copy">差异：${esc(item.difference || "-")}</div>
      ${item.action ? `<div class="r3-action">下一场这样做：${esc(item.action)}</div>` : ""}
    </div>`;
  }).join("") || '<div class="faint">暂无逐段对比结果。</div>';

  const extraRows = objList(cmp.extra_issues).map(item => `<div class="r3-diff warn">
    <div class="r3-diff-head"><span class="r3-badge warn">额外节奏问题</span><b>${esc(item.diagnosis || "-")}</b></div>
    ${item.action ? `<div class="r3-action">下一场这样做：${esc(item.action)}</div>` : ""}</div>`).join("");
  const actions = (Array.isArray(cmp.action_items) ? cmp.action_items : []).map(x => `<li>${esc(x)}</li>`).join("");

  const stepRows = steps.map((step, i) => `<div class="r3-block" style="grid-template-columns:34px minmax(0,1fr)">
    <div class="r3-time">${esc(step.order || i + 1)}</div><div><div class="r3-block-title">${esc(step.phase || `第${i + 1}段`)} · ${r3Duration(step.duration_sec)}</div>
    <div class="r3-block-copy">${esc(step.content || step.action || "-")}</div></div></div>`).join("") || '<div class="faint">未选择可对比模板。</div>';
  const [vLabel, vClass] = r3StatusMeta(val.status || "unverified");
  const evidence = objList(val.evidence).map(item => `<div class="r3-evidence">
    ${hasMedia ? `<button class="clip-btn" data-dock="${Number(item.at_ms) || 0}" data-lab="罗盘证据 ${esc(item.metric || "")}">${esc(item.time || mmss(item.at_ms))}</button>` : `<span class="r3-time">${esc(item.time || mmss(item.at_ms))}</span>`}
    <span>${esc(item.effect || item.metric || "-")}</span></div>`).join("");
  const stats = asObj(val.template_stats);
  const statsText = stats.trial_count
    ? `累计登记 ${stats.trial_count} 场 · 已判定 ${stats.verified_count || 0} 场 · 有效率 ${stats.effective_rate == null ? "待积累" : stats.effective_rate + "%"} · 覆盖 ${stats.streamer_count || 0} 位主播`
    : "该模板尚无足够的历史执行样本";
  const validationHtml = `<div class="r3-panel r3-validation">
    <div class="r3-panel-head"><b>罗盘效果验证</b><span class="sp"></span><span class="r3-badge ${vClass}">${esc(val.verdict || vLabel)}</span></div>
    <div class="r3-block-copy">${esc(val.reason || "仅明确登记执行的场次才会参与有效性统计。")}</div>
    ${evidence}<div class="faint" style="font-size:11px;margin-top:8px">${esc(statsText)}</div>
    ${r3.declared_execution && r3.status === "ready" && canVerify ? '<button class="btn ghost sm" id="r3VerifyBtn" style="margin-top:9px">↻ 用最新罗盘数据重验</button>' : ""}
  </div>`;
  const match = cmp.match_score != null ? cmp.match_score : "-";
  return `${failureNotice}<div class="r3-layout">
    <div class="r3-stack">
      <div class="r3-panel"><div class="r3-panel-head"><b>本场实际节奏结构</b><span class="r3-badge info">${blocks.length} 段</span>
        <span class="sp"></span><span class="faint">口播 ${r3Duration(actual.spoken_duration_sec)} · 通用内容 ${actual.general_share_pct || 0}%</span></div>${blockRows}</div>
      <div class="r3-panel"><div class="r3-panel-head"><b>逐段差异与改法</b><span class="r3-badge info">匹配度 ${esc(match)} 分</span></div>
        ${cmp.summary ? `<div class="v2hint">${esc(cmp.summary)}</div>` : ""}${diffRows}${extraRows}</div>
      ${actions ? `<div class="r3-panel"><div class="r3-panel-head"><b>主播下一场执行清单</b></div><ol class="tight">${actions}</ol></div>` : ""}
    </div>
    <div class="r3-stack">
      <div class="r3-panel"><div class="r3-panel-head"><b>优秀节奏基准</b></div>
        <div class="r3-block-title">${esc(tpl.title || "暂无已采纳模板")}</div>
        ${tpl.source ? `<div class="faint" style="font-size:11px;margin-top:3px">来源：${esc(tpl.source)}</div>` : ""}
        <div style="margin-top:8px">${stepRows}</div></div>
      ${validationHtml}${hasMedia ? '<div class="v2dockslot" data-slot="r3rhythm"></div>' : ""}
    </div>
  </div>`;
}

async function loadR3Pane(id, sid, media) {
  const state = detailState;
  const modal = $("#modalBody");
  const pane = modal && modal.querySelector('.dt-pane[data-pane="r3rhythm"]');
  if (!pane) return;
  const owners = (state.streamers || []).filter(x => Number(x.streamer_id) > 0);
  const targetSid = Number(sid || (owners.length === 1 ? owners[0].streamer_id : 0));
  if (!targetSid) {
    pane.innerHTML = '<div class="r3-empty">话术节奏按主播独立分析。请先在上方选择一个具体主播。</div>';
    return;
  }
  const requestToken = (state.r3RequestToken || 0) + 1;
  state.r3RequestToken = requestToken;
  pane.innerHTML = '<div class="r3-empty">正在读取本场节奏与优秀节奏库…</div>';
  const query = `streamer_id=${encodeURIComponent(targetSid)}`;
  try {
    const [analysis, recommendationPack, templatePack] = await Promise.all([
      api(`/api/sessions/${id}/rhythm-analysis?${query}`),
      api(`/api/sessions/${id}/rhythm-analysis/recommendations?${query}&limit=8`).catch(() => ({ recommendations: [] })),
      api("/api/rhythm-library/templates?status=adopted").catch(() => ({ templates: [], load_failed: true })),
    ]);
    if (detailState !== state || state.id !== id || state.r3RequestToken !== requestToken || state.currentSid !== (sid ? String(sid) : "")) return;
    const recs = objList(recommendationPack && recommendationPack.recommendations);
    const templates = [...objList(templatePack && templatePack.templates)];
    recs.forEach(rec => {
      const recTemplate = asObj(rec.template);
      if (recTemplate.id && !templates.some(t => Number(t.id) === Number(recTemplate.id))) templates.push(recTemplate);
    });
    const eligibleIds = new Set(templates.map(t => Number(t.id)));
    const declaredLocked = !!analysis.declared_execution;
    const eligibilityKnown = !templatePack.load_failed || recs.length > 0;
    const declaredEligible = declaredLocked && Number(analysis.template_id) > 0
      && (eligibleIds.has(Number(analysis.template_id)) || (!eligibilityKnown && analysis.status !== "failed"));
    const currentEligibleId = eligibleIds.has(Number(analysis.template_id)) ? Number(analysis.template_id) : 0;
    const selectedId = Number(
      declaredLocked ? (analysis.template_id || 0)
        : currentEligibleId || (recs[0] && asObj(recs[0].template).id) || (templates[0] && templates[0].id) || 0,
    );
    const firstRecommendedId = Number(recs[0] && asObj(recs[0].template).id || 0);
    const snapshot = asObj(analysis.template);
    const options = declaredLocked
      ? `<option value="${selectedId || ""}" selected>${esc(snapshot.title || "原执行模板")}${declaredEligible ? "" : " · 已归档/撤出"}</option>`
      : (templates.map(t => `<option value="${Number(t.id)}"${Number(t.id) === selectedId ? " selected" : ""}>${Number(t.id) === firstRecommendedId ? "AI 首选 · " : ""}${esc(t.title || "未命名节奏")} · ${esc(t.scene_label || "通用")}</option>`).join("")
        || '<option value="">暂无已采纳模板，仅提取实际结构</option>');
    const canPlan = !state.endedAt && ["created", "parsing", "live"].includes(state.status);
    const planControl = analysis.declared_execution
      ? (declaredEligible
        ? '<span class="r3-badge info">✓ 已在直播执行前登记，模板已锁定</span>'
        : '<span class="r3-badge bad">原计划模板已撤出，停止效果归因</span>')
      : canPlan
        ? `<button class="btn ghost sm" id="r3PlanBtn"${selectedId ? "" : " disabled"}>登记本场执行计划</button>`
        : '<span class="faint" style="font-size:11.5px">已收播：可做节奏对比，但不能事后追认模板效果</span>';
    const progress = analysis.status === "analyzing"
      ? '<div class="v2hint">AI 正在逐段对比；若有上次结果，下方会继续保留显示。页面会自动刷新。</div>' : "";
    pane.innerHTML = `<div class="r3-toolbar">
      <label class="r3-field"><span>对比的优秀节奏（仅显示已人工采纳模板）</span>
        <select id="r3Template"${declaredLocked ? " disabled" : ""}>${options}</select></label>
      ${planControl}
      <button class="btn sm" id="r3RunBtn"${declaredLocked && !declaredEligible ? " disabled" : ""}>✨ AI 提取并对比</button>
      <div class="r3-rec" id="r3Rec"></div><div class="r3-rec" id="r3Hint"></div>
    </div>${progress}${!declaredLocked && analysis.available && analysis.template_id && !currentEligibleId ? `<div class="v2hint">下方保留的是已归档模板《${esc(snapshot.title || "历史模板")}》的历史结果；重新分析将使用上方新模板。</div>` : ""}${r3AnalysisHTML(analysis, !!(media && media.available), declaredEligible)}`;
    const select = pane.querySelector("#r3Template"), recBox = pane.querySelector("#r3Rec"), hint = pane.querySelector("#r3Hint");
    const updateRec = () => {
      const value = Number(select && select.value || 0);
      const rec = recs.find(x => Number(x.template && x.template.id) === value);
      recBox.textContent = declaredLocked
        ? (declaredEligible ? "执行计划已锁定；本场验证始终归属于登记时的同一模板。" : "原模板仅以快照保留，不能换模板追认效果。")
        : rec ? `AI 推荐 ${rec.score} 分：${(Array.isArray(rec.reasons) ? rec.reasons : []).join("；")}` : (templates.length ? "可手动选择其他已采纳节奏。" : "请先到知识库采纳至少一条优秀节奏模板，才能逐段对比。");
    };
    if (select) select.onchange = updateRec;
    updateRec();
    const refresh = async () => {
      if (detailState !== state || !modalOpen()) return;
      const freshStreamers = await api(`/api/sessions/${id}/streamers`).catch(() => null);
      if (detailState !== state || !modalOpen()) return;
      if (freshStreamers) state.streamers = freshStreamers;
      await renderDetailFor(id, state.currentSid || null);
    };
    const planBtn = pane.querySelector("#r3PlanBtn");
    if (planBtn) planBtn.onclick = async () => {
      const templateId = Number(select && select.value || 0);
      if (!templateId) return;
      planBtn.disabled = true; planBtn.textContent = "登记中…"; hint.textContent = "";
      try {
        await jpost(`/api/sessions/${id}/rhythm-analysis/plan`, { streamer_id: targetSid, template_id: templateId });
        await refresh();
      } catch (e) { planBtn.disabled = false; planBtn.textContent = "登记本场执行计划"; hint.textContent = "登记失败：" + fmtErr(e); }
    };
    const runBtn = pane.querySelector("#r3RunBtn");
    if (runBtn) runBtn.onclick = async () => {
      const templateId = Number(select && select.value || 0);
      runBtn.disabled = true; runBtn.textContent = "AI 对比中…（约几十秒）"; hint.textContent = "正在提取时间段、内容和时长，并逐段定位差异。";
      try {
        await jpost(`/api/sessions/${id}/rhythm-analysis/analyze`, {
          streamer_id: targetSid, template_id: templateId || null,
        });
        await refresh();
      } catch (e) {
        if (detailState === state && modalOpen()) {
          await loadR3Pane(id, sid, media);
          const freshHint = pane.querySelector("#r3Hint");
          if (freshHint) freshHint.textContent = "分析失败：" + fmtErr(e);
        }
      }
    };
    const verifyBtn = pane.querySelector("#r3VerifyBtn");
    if (verifyBtn) verifyBtn.onclick = async () => {
      verifyBtn.disabled = true; verifyBtn.textContent = "验证中…";
      try { await api(`/api/sessions/${id}/rhythm-analysis/verify?${query}`, { method: "POST" }); await refresh(); }
      catch (e) {
        if (detailState === state && modalOpen()) {
          await loadR3Pane(id, sid, media);
          const freshHint = pane.querySelector("#r3Hint");
          if (freshHint) freshHint.textContent = "验证失败：" + fmtErr(e);
        }
      }
    };
    if (state.viewPane === "r3rhythm") { v2EnsureDock(); if (state.applyPane) state.applyPane(); }
    const shouldPoll = analysis.status === "analyzing"
      || (["not_generated", "planned"].includes(analysis.status) && state.status === "done");
    if (shouldPoll && (state.r3PollCount || 0) < 48) {
      const poll = async () => {
        if (detailState !== state || !modalOpen() || state.r3RequestToken !== requestToken) return;
        state.r3PollCount = (state.r3PollCount || 0) + 1;
        const latest = await api(`/api/sessions/${id}/rhythm-analysis?${query}`).catch(() => null);
        if (detailState !== state || !modalOpen() || state.r3RequestToken !== requestToken) return;
        const pending = latest && (latest.status === "analyzing"
          || (["not_generated", "planned"].includes(latest.status) && state.status === "done"));
        if (!pending) { state.r3PollCount = 0; void loadR3Pane(id, sid, media); return; }
        if (state.r3PollCount < 48) setTimeout(poll, 2500);
      };
      setTimeout(poll, 2500);
    } else if (!shouldPoll) state.r3PollCount = 0;
  } catch (e) {
    if (detailState === state && state.r3RequestToken === requestToken) pane.innerHTML = `<div class="r3-empty" style="color:var(--red)">话术节奏加载失败：${esc(fmtErr(e))}</div>`;
  }
}

async function renderDetailFor(id, sid) {
  const state = detailState;
  const renderToken = (detailState.renderToken || 0) + 1;
  detailState.renderToken = renderToken;
  detailState.currentSid = sid ? String(sid) : "";
  const q = sid ? `?streamer_id=${sid}` : "";
  const [tr, cr, media] = await Promise.all([
    api(`/api/sessions/${id}/transcripts${q}`),
    api(`/api/sessions/${id}/critiques${q}`),
    api(`/api/sessions/${id}/media_url`).catch(() => null),
  ]);
  if (detailState !== state || detailState.id !== id || detailState.renderToken !== renderToken) return;
  const sm = detailState.streamers.find(s => String(s.streamer_id) === String(sid));
  const flags = cr.filter(c => c.level === "sentence" || c.level === "screen");
  const segs = cr.filter(c => c.level === "segment");
  const summaries = sid
    ? (sm && sm.summary ? [sm.summary] : [])
    : (detailState.streamers || []).map(s => s.summary).filter(Boolean);
  const hasPlayer = !!(media && media.available);

  // v2 深度复盘数据（2026-07-15 改版）：取当前主播（或第一个有 v2 的）summary.data.v2
  const v2Pack = summaries.map(c => ({ sid: c.streamer_id, v2: (parseJSON(c.data) || {}).v2 }))
    .filter(x => x.v2);
  const v2 = v2Pack.length ? v2Pack[0].v2 : null;
  const v2Sid = v2Pack.length ? v2Pack[0].sid : (sid || (summaries[0] && summaries[0].streamer_id) || null);
  const sumHtml = summaries.map(c => {
    const d = parseJSON(c.data); d.summary = d.summary || c.comment;
    if (d.overall_score == null) d.overall_score = c.score;
    d.llm = d.llm || c.llm_provider;
    d.llm_requested = d.llm_requested || c.llm_requested_provider;
    d.is_fallback = Boolean(d.is_fallback || c.is_fallback);
    return `<div class="sum-card" style="margin-bottom:12px">${reportCardHTML(nameOf(c.streamer_id, sm && sm.name), colorOf(c.streamer_id), d)}</div>`;
  }).join("");
  const lines = tr.map(t =>
    `<div class="line" data-transcript-id="${Number(t.id)}" data-seek="${t.start_ms}"${hasPlayer ? ' style="cursor:pointer"' : ""}><div class="meta">${t.streamer_id ? `<span class="dot" style="background:${colorOf(t.streamer_id)}"></span><span class="who" style="color:${colorOf(t.streamer_id)}">${esc(nameOf(t.streamer_id))}</span>` : ""}<span class="t">${wallT(detailState.startedAt, t.start_ms)}</span></div>${esc(t.corrected_text)}</div>`).join("") || '<div class="faint">无</div>';
  const flagHtml = flags.map(c => {
    const d = parseJSON(c.data) || {};
    return `<div class="line fl ${c.severity === "error" ? "error" : ""} ${c.level === "screen" ? "screen" : ""}" data-seek="${c.start_ms}"${hasPlayer ? ' style="cursor:pointer"' : ""}><div class="meta"><span class="tag cat">${esc(c.category)}</span>${c.level === "screen" ? '<span class="tag src">画面</span>' : ""}${llmBadge(d.llm || c.llm_provider, c.is_fallback ?? d.is_fallback, d.llm_requested || c.llm_requested_provider)}<span class="t">${wallT(detailState.startedAt, c.start_ms)}</span></div>${esc(c.comment)}<div class="quote">原话：${esc(c.quote)}</div></div>`;
  }).join("") || '<div class="faint">无</div>';
  const segHtml = segs.map(c => {
    const d = parseJSON(c.data) || {};
    const seek = hasPlayer ? `<button class="btn ghost sm" data-seek="${c.start_ms}" style="margin-top:6px">▶ 播放本段（${wallT(detailState.startedAt, c.start_ms)}）</button>` : "";
    return `<div class="line fl seg" data-start="${c.start_ms}">${c.score != null ? "<b>" + c.score + "分</b> " : ""}${sceneHTML(d.scene_label)}${d.asr_emotion_label ? emotionHTML(d.asr_emotion_label, true) : emotionHTML(d.emotion)}${toneHTML(d.pitch)}${llmBadge(d.llm || c.llm_provider, d.is_fallback || c.is_fallback, d.llm_requested || c.llm_requested_provider)}${esc(c.comment)}${emotionMatchHTML(d.emotion_match)}${loopHTML(d.loop)}${rhythmHTML(d.metrics)}${seek}</div>`;
  }).join("");

  detailState.avOffset = +(localStorage.getItem("fupan_v2off_" + id) || 0);
  let playerHtml = "";
  if (hasPlayer) {
    const elt = media.kind === "video"
      ? `<video id="detailPlayer" controls preload="metadata" style="height:56vh;aspect-ratio:9/16;width:auto;max-width:100%;background:#000;border-radius:10px;object-fit:contain"></video>`
      : `<audio id="detailPlayer" controls preload="metadata" style="width:100%"></audio>`;
    const calBtn = `<span style="display:flex;gap:6px;align-items:center"><button class="btn ghost sm" id="avCalib" title="视频和逐字稿对不上时:先播放到一句能听清的话,点此按钮,再点逐字稿里对应那句">⚙ 对齐校准</button><span class="faint" id="avCalHint" style="font-size:11.5px">${detailState.avOffset ? "已校准 " + (detailState.avOffset > 0 ? "+" : "") + (detailState.avOffset / 1000).toFixed(1) + "s" : ""}</span></span>`;
    playerHtml = `<div class="card"><div class="card-head"><h3>${media.kind === "video" ? "🎬 直播回看" : "🔊 录音回放"} · 点右侧话术句跳转</h3>${calBtn}</div>${elt}</div>`;
  }
  const unavailableAudio = Client.audioPresentation({ status: detailState.status, audio: detailState.audio, error_msg: detailState.errorMsg });
  const audioNotice = hasPlayer ? "" : `<div class="card" style="margin-bottom:10px;border-left:3px solid var(--warn)"><b>录音不可用</b>${unavailableAudio.reason ? `<div class="faint" style="margin-top:4px">${esc(Client.safeError(unavailableAudio.reason, "音频合并失败"))}</div>` : ""}</div>`;

  // 整段复盘章节卡(2026-07-15 用户规则:同产品/同动作聚合,四要素整体复盘,闲置/偷懒点名)
  const chs = (v2 && v2.chapters && v2.chapters.items) || [];
  const idleTag = c => c.idle === "prep" ? '<span class="vchip bad">⚠ 准备不足/闲置</span>'
    : (c.idle === "lazy" ? '<span class="vchip bad">⚠ 偷懒行为</span>' : "");
  const chCard = c => `<div class="v2q ch" data-chs="${c.start_ms}" data-che="${c.end_ms}">
      <div class="v2q-head" data-tg>
        <span class="v2sev ${c.idle && c.idle !== "none" ? "r" : (c.score != null && c.score < 6 ? "o" : "g")}"></span>
        <span class="v2no">${esc(c.wall || "")}</span>
        <span class="v2title">${esc(c.type || "")}${c.product ? " · " + esc(c.product) : ""}${c.score != null ? " · " + c.score + "分" : ""} ${idleTag(c)}</span>
        <span class="v2clips">${hasPlayer ? `<button class="clip-btn" data-seek="${c.start_ms}">▶ 播放本段</button>` : ""}</span>
        <span class="v2arrow">▶</span></div>
      <div class="v2body">
        ${(c.structures || []).length ? `<div class="v2lbl">包含的话术结构</div><div>${c.structures.map(x => `<span class="vchip">${esc(x)}</span>`).join(" ")}</div>` : ""}
        ${(c.missing || []).length ? `<div class="v2lbl">缺少</div><div>${c.missing.map(x => `<span class="vchip warn">${esc(x)}</span>`).join(" ")}</div>` : ""}
        ${(c.problems || []).length ? `<div class="v2lbl">哪里做得不好</div>${c.problems.map(p => `<div>· ${esc(p)}</div>`).join("")}` : ""}
        ${c.how_to ? `<div class="v2lbl">应该怎么讲</div><div class="v2fix">${esc(c.how_to)}</div>` : ""}
      </div></div>`;
  // 按产品分组:同一产品的所有整段集中一块(2026-07-15 用户要求"集中复盘一个产品,不要零散")
  const groups = [];
  for (const c of chs) {
    const key = c.product || "通用环节（开场/互动/过渡）";
    let g = groups.find(x => x.key === key);
    if (!g) { g = { key, items: [] }; groups.push(g); }
    g.items.push(c);
  }
  const prodReviews = (v2 && v2.products && v2.products.items) || [];
  const chHtml = groups.map(g => {
    const mins = Math.round(g.items.reduce((s, c) => s + (c.end_ms - c.start_ms), 0) / 60000);
    const scores = g.items.map(c => c.score).filter(x => x != null);
    const avg = scores.length ? (scores.reduce((a, b) => a + b, 0) / scores.length).toFixed(1) : "—";
    const pr = prodReviews.find(x => x.product === g.key);
    // 产品级综合判词(2026-07-15):同一产品全场时段合并成一份整体复盘,时段明细默认收起
    const summary = pr ? `<div class="v2q open" style="border-left:3px solid var(--green)">
        <div class="v2body" style="display:block;border-top:none;padding-top:10px">
          <div style="font-weight:700;margin-bottom:4px">${pr.score != null ? pr.score + " 分 · " : ""}${esc(pr.verdict || "")}</div>
          ${(pr.structures || []).length ? `<div class="v2lbl">全场覆盖的结构</div><div>${pr.structures.map(x => `<span class="vchip">${esc(x)}</span>`).join(" ")}</div>` : ""}
          ${(pr.missing || []).length ? `<div class="v2lbl">全场始终缺失</div><div>${pr.missing.map(x => `<span class="vchip warn">${esc(x)}</span>`).join(" ")}</div>` : ""}
          ${(pr.problems || []).length ? `<div class="v2lbl">整体最要命的问题</div>${pr.problems.map(p => `<div>· ${esc(p)}</div>`).join("")}` : ""}
          ${pr.how_to ? `<div class="v2lbl">这个产品今后应该怎么讲</div><div class="v2fix">${esc(pr.how_to)}</div>` : ""}
        </div></div>` : "";
    const detail = `<details${pr ? "" : " open"}><summary class="faint" style="cursor:pointer;font-size:12px;padding:2px 0">▸ ${g.items.length} 个时段明细（点击展开,含逐段回放）</summary>${g.items.map(chCard).join("")}</details>`;
    return `<div class="v2pgroup"><div class="v2pgroup-head">📦 ${esc(g.key)}
        <span class="faint" style="font-weight:400;font-size:11.5px">共 ${g.items.length} 段 · 约 ${mins} 分钟 · 均分 ${avg}</span></div>
      ${summary}${detail}</div>`;
  }).join("");
  const rightCol = chHtml
    ? `<div class="card"><div class="card-head"><h3>整段复盘（${chs.length} 段 · 播放到哪段自动高亮）</h3></div>
        <div class="stream" id="dtChapters" style="height:56vh">${chHtml}
          <details style="margin-top:10px"><summary class="faint" style="cursor:pointer;font-size:12px">🗂 句级问题（${flags.length}）+ 段级明细（旧视图）</summary>${flagHtml}${segHtml}</details>
        </div></div>`
    : `<div class="card"><div class="card-head"><h3>问题（${flags.length}）+ 段级点评</h3></div><div class="stream" style="height:56vh">${flagHtml}${segHtml}</div></div>`;
  stopDetailPlayer();   // 切主播子tab时先停掉旧播放器，再覆盖内容
  // 有播放器:视频左 | 逐字稿右(同屏对照);点评移到下方全宽。无播放器:沿用两栏。
  const mainHtml = hasPlayer
    ? `<div class="av-grid">
        ${playerHtml}
        <div class="card"><div class="card-head"><h3>逐字稿（${tr.length} 句）</h3></div><div class="stream" id="dtScript" style="height:56vh">${lines}</div></div>
        ${rightCol}
      </div>`
    : `${audioNotice}<div class="live-grid" style="margin-top:0">
        <div class="card"><div class="card-head"><h3>逐字稿（${tr.length} 句）</h3></div><div class="stream" id="dtScript" style="height:46vh">${lines}</div></div>
        ${rightCol}
      </div>`;
  // 总览 KPI 条:时长/句数同步可得,罗盘(GMV/成交/在线峰值/观看/涨粉)异步补
  const durMs = detailState.startedAt && detailState.endedAt
    ? (new Date(detailState.endedAt) - new Date(detailState.startedAt)) : 0;
  const durTxt = durMs > 0 ? `${Math.floor(durMs / 3600000)}时${Math.round(durMs % 3600000 / 60000)}分` : "—";
  const kpiHtml = `<div class="dt-kpis" id="dtKpis">
      <span class="kpi"><i>直播时长</i><b>${durTxt}</b></span>
      <span class="kpi"><i>话术句数</i><b>${tr.length}</b></span>
      <span class="kpi" data-k="gmv"><i>支付金额</i><b>…</b></span>
      <span class="kpi" data-k="orders"><i>成交件数</i><b>…</b></span>
      <span class="kpi" data-k="peak"><i>在线峰值</i><b>…</b></span>
      <span class="kpi" data-k="views"><i>累计观看</i><b>…</b></span>
      <span class="kpi" data-k="fans"><i>新增粉丝</i><b>…</b></span>
    </div>`;
  $("#detailBody").innerHTML = `
    <div class="dt-pane" data-pane="overview" hidden>${kpiHtml}${v2 ? v2OverviewIdx(v2) : ""}${sumHtml || '<div class="faint" style="padding:24px;text-align:center">整场总结还未生成（下播后自动生成，或点「情绪精算」手动触发）</div>'}</div>
    <div class="dt-pane" data-pane="replay" hidden>${mainHtml}</div>
    <div class="dt-pane" data-pane="v2tone" hidden></div>
    <div class="dt-pane" data-pane="v2struct" hidden></div>
    <div class="dt-pane" data-pane="r3rhythm" hidden></div>
    <div class="dt-pane" data-pane="prescribed" hidden><div id="prescribedAnalysis"><div class="r3-empty">正在读取规定话术执行结果…</div></div></div>
    <div class="dt-pane" data-pane="v2qc" hidden></div>
    <div class="dt-pane" data-pane="v2dm" hidden></div>`;
  renderV2Panes(id, v2Sid, v2, tr, media);
  void loadR3Pane(id, sid, media);
  void loadPrescribedAnalysis(id);
  // 无总结时默认落到回放对照,避免打开是空页
  if (!sumHtml && detailState.viewPane === "overview") detailState.viewPane = "replay";
  if (detailState.applyPane) detailState.applyPane();
  void fillDetailKpis(id);

  const pl = $("#detailPlayer");
  if (pl && hasPlayer) {
    if (_detailHlsDispose) { _detailHlsDispose(); _detailHlsDispose = null; }
    const mSrc = String(media.url).startsWith("http") ? media.url : withBase(media.url);
    _detailHlsDispose = attachMedia(pl, mSrc, Boolean(media.hls));
    pl.addEventListener("loadedmetadata", () => avCalcScale(pl), { once: true });
    avCalcScale(pl);
    // 主播页签联动(2026-07-15):选中某主播时,进度条直接跳到 ta 的时段开头(首句话术处)
    if (sid && tr.length) {
      const t0 = tr[0].start_ms || 0;
      pl.addEventListener("loadedmetadata", () => { try { pl.currentTime = avToVideoSec(t0); } catch (e) { } }, { once: true });
      try { pl.currentTime = avToVideoSec(t0); } catch (e) { }
    }
  }
  {
    const wrap = $("#detailBody");
    const script = $("#dtScript");
    const tLines = script ? [...script.querySelectorAll(".line[data-seek]")] : [];
    // 右侧点评（句级/画面 data-seek + 段级 data-start）统一按时间索引
    const rItems = [...wrap.querySelectorAll(".line.fl")].map(e => ({ e, ts: +(e.dataset.seek || e.dataset.start || 0) }));
    const tsOf = (e) => +(e.dataset.seek || e.dataset.start || 0);
    // 找时间最接近的元素（优先其前最近，其次整体最近）
    const nearest = (list, ms) => {
      if (!list.length) return null;
      let best = null, bd = Infinity;
      for (const x of list) { const d = Math.abs((x.ts != null ? x.ts : tsOf(x)) - ms); if (d < bd) { bd = d; best = x; } }
      return best && (best.e || best);
    };
    const flash = (el) => {
      if (!el) return;
      scrollInBox(el, true);
      el.classList.add("assoc-hl");
      setTimeout(() => el.classList.remove("assoc-hl"), 2200);
    };
    const calib = $("#avCalib");
    if (calib) calib.onclick = () => {
      detailState._calibArm = true;
      $("#avCalHint").textContent = "现在点逐字稿里与当前画面对应的那句话…";
    };
    wrap.querySelectorAll("[data-seek],[data-start]").forEach(elm => elm.addEventListener("click", (ev) => {
      if (elm.classList.contains("seg") && ev.target.tagName === "BUTTON") return;  // 段内播放按钮走自己的 data-seek
      const ms = tsOf(elm);
      // 校准模式:点句=声明"当前画面正在讲这句" → 偏移=视频当前时间-句子时间,全场生效
      if (detailState._calibArm && pl && script && script.contains(elm)) {
        detailState._calibArm = false;
        detailState.avOffset = Math.round(pl.currentTime * 1000 - ms * avScale());
        localStorage.setItem("fupan_v2off_" + id, String(detailState.avOffset));
        $("#avCalHint").textContent = "已校准 " + (detailState.avOffset > 0 ? "+" : "") + (detailState.avOffset / 1000).toFixed(1) + "s(全场生效,再点⚙可重校)";
        return;
      }
      if (pl) { try { pl.currentTime = avToVideoSec(ms); pl.play().catch(() => { }); } catch (e) { } }
      // 精准关联：点左侧话术句 → 右侧滚到对应点评；点右侧点评 → 左侧滚到对应话术句
      if (script && script.contains(elm)) flash(nearest(rItems, ms));
      else if (elm.closest(".fl")) flash(nearest(tLines, ms));
    }));
    // 播放进度 → 双列自动高亮跟随
    if (pl) {
      const sItems = [...wrap.querySelectorAll(".line.fl.seg[data-start]")];
      const pick = (arr, tms, key) => { let c = null; for (const e of arr) { if ((+e.dataset[key] || 0) <= tms) c = e; else break; } return c; };
      let lastT = null, lastS = null, lastC = null;
      const chCards = [...wrap.querySelectorAll(".v2q.ch")];
      pl.addEventListener("timeupdate", () => {
        const tms = avFromVideoMs(pl.currentTime);
        const ct = pick(tLines, tms, "seek");
        if (ct !== lastT) { if (lastT) lastT.classList.remove("playing"); if (ct) { ct.classList.add("playing"); scrollInBox(ct); } lastT = ct; }
        const cs = pick(sItems, tms, "start");
        if (cs !== lastS) { if (lastS) lastS.classList.remove("playing"); if (cs) { cs.classList.add("playing"); scrollInBox(cs); } lastS = cs; }
        // 整段复盘卡跟随:播放进入哪个章节,右列该卡高亮并滚到可见(修"右边没有突出显示")
        const cc = chCards.find(c => +c.dataset.chs <= tms && tms <= +c.dataset.che);
        if (cc !== lastC) { if (lastC) lastC.classList.remove("playing"); if (cc) { cc.classList.add("playing"); scrollInBox(cc); } lastC = cc; }
      });
    }
  }
}

// ===================== 主播管理 =====================
async function loadStreamerTab() {
  await loadRoster();
  const tb = $("#stmRows"); tb.innerHTML = "";
  if (!roster.length) { tb.innerHTML = '<tr><td colspan="3"><div class="empty">还没有主播，先在上方添加</div></td></tr>'; return; }
  roster.forEach(s => {
    const tr = el("tr");
    tr.innerHTML = `<td><span class="badge" data-edit="${s.id}" style="cursor:pointer" title="点击改名"><span class="dot" style="background:${s.color}"></span>${esc(s.name)} ✎</span></td>
      <td class="faint">${esc(s.note)}</td>
      <td><button class="btn sm" data-account="${s.id}">设置主播账号</button> <button class="btn danger sm" data-delete="${s.id}">删除</button></td>`;
    tb.appendChild(tr);
  });
  tb.querySelectorAll("[data-edit]").forEach(el => el.onclick = () => renameStreamer(Number(el.dataset.edit)));
  tb.querySelectorAll("[data-account]").forEach(b => b.onclick = async () => {
    const username = prompt("主播登录账号"); if (!username) return;
    const password = prompt("主播登录密码（至少 8 位）"); if (!password) return;
    await api(`/api/anchor/admin/account/${b.dataset.account}`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password })
    });
    alert("主播账号已设置。注意：主播账号登录只在服务器本机应急时可用，主播平时请在业绩系统里点「查看完整复盘」。");
  });
  tb.querySelectorAll("[data-delete]").forEach(b => b.onclick = async () => {
    if (!confirm("删除该主播？历史归属记录会保留 streamer_id。")) return;
    await fetch(withBase(`/api/streamers/${b.dataset.delete}`), { method: "DELETE" }); loadStreamerTab();
  });
}
$("#btnStmAdd").onclick = async () => {
  const name = $("#stmName").value.trim();
  if (!name) { alert("请填写主播名"); return; }
  await jpost("/api/streamers", { name, note: $("#stmNote").value, color: $("#stmColor").value });
  $("#stmName").value = $("#stmNote").value = "";
  loadStreamerTab();
};

// ===================== 知识库 =====================
async function loadKb() {
  loadKbChunks();
  loadRhythmLibrary();
  const rows = await api("/api/knowledge");
  const tb = $("#kbRows"); tb.innerHTML = "";
  if (!rows.length) { tb.innerHTML = '<tr><td colspan="5"><div class="empty">暂无自定义条目</div></td></tr>'; return; }
  rows.forEach(it => {
    const tr = el("tr");
    tr.innerHTML = `<td><span class="badge">${esc(it.kind)}</span></td><td>${esc(it.name)}</td>
      <td class="faint">${esc(it.aliases)}</td><td class="faint">${esc(it.attrs || it.note)}</td>
      <td><button class="btn danger sm" data-id="${it.id}">删除</button></td>`;
    tb.appendChild(tr);
  });
  tb.querySelectorAll("button").forEach(b => b.onclick = async () => {
    await fetch(withBase(`/api/knowledge/${b.dataset.id}`), { method: "DELETE" }); loadKb();
  });
}
$("#btnKbAdd").onclick = async () => {
  try {
    await jpost("/api/knowledge", { kind: $("#kbKind").value, name: $("#kbName").value, aliases: $("#kbAliases").value, attrs: $("#kbAttrs").value });
    $("#kbName").value = $("#kbAliases").value = $("#kbAttrs").value = ""; loadKb();
  } catch (e) { alert("添加失败：" + fmtErr(e)); }
};
$("#btnKbImport").onclick = async () => {
  const f = $("#kbFile").files[0]; if (!f) { alert("请先选择 .xlsx"); return; }
  const fd = new FormData(); fd.append("file", f);
  const r = await fetch(withBase("/api/knowledge/import"), { method: "POST", body: fd }).then(x => x.json());
  alert(`已导入 ${r.imported} 条`); loadKb();
};

// ---- 知识库文档（RAG：文本/文件 → 切片 → 向量检索）----
let _kbScene = "";   // 当前分库场景筛选（""=全部）
async function loadKbChunks() {
  const [rows, st, scenes] = await Promise.all([
    api(`/api/knowledge/chunks${_kbScene ? "?scene=" + encodeURIComponent(_kbScene) : ""}`),
    api("/api/knowledge/embed_status").catch(() => null),
    api("/api/knowledge/scenes").catch(() => []),
  ]);
  // 分库场景栏（全部 + 各话术场景计数）
  const bar = $("#kbSceneBar");
  if (bar) {
    const total = (scenes || []).reduce((a, s) => a + (s.count || 0), 0);
    const chips = [`<span class="schip${_kbScene === "" ? " on" : ""}" data-s="">全部<span class="n">${total}</span></span>`]
      .concat((scenes || []).filter(s => s.count > 0)
        .map(s => `<span class="schip${_kbScene === s.key ? " on" : ""}" data-s="${esc(s.key)}">${esc(s.label)}<span class="n">${s.count}</span></span>`));
    bar.innerHTML = chips.join("");
    bar.querySelectorAll(".schip").forEach(c => c.onclick = () => { _kbScene = c.dataset.s; loadKbChunks(); });
  }
  const tb = $("#kbChunkRows"); tb.innerHTML = "";
  if (st) {   // 向量索引健康提示（不再无声降级）
    let warn = "";
    if (!st.configured) warn = "未配置百炼 key：知识库仅走关键词检索（语义检索关闭）";
    else if (st.last_ok === false) warn = "向量化最近失败，新切片可能只走关键词：" + (st.last_error || "");
    else if (st.unindexed > 0) warn = `${st.unindexed}/${st.total} 切片未建向量索引`;
    if (warn) tb.innerHTML = `<tr><td colspan="5"><div class="empty" style="color:var(--warn)">⚠️ ${esc(warn)}</div></td></tr>`;
  }
  if (!rows.length) { tb.innerHTML += `<tr><td colspan="5"><div class="empty">${_kbScene ? "该场景下暂无切片" : "还没有文档，拖文件进来或粘贴文本"}</div></td></tr>`; return; }
  rows.forEach(r => {
    const tr = el("tr");
    const kindBadge = r.kind === "distilled"
      ? `<span class="tag" style="background:var(--green);color:#000">提炼${r.category ? "·" + esc(r.category) : ""}</span> ` : "";
    const sceneChip = r.scene_label
      ? `<span class="tag" style="background:var(--cyan);color:#04222a">${esc(r.scene_label)}</span>` : '<span class="faint">—</span>';
    tr.innerHTML = `<td class="faint">${esc(r.source)}</td><td>${sceneChip}</td><td>${kindBadge}${esc(r.text)}</td>
      <td>${r.indexed ? "✓ 向量" : "关键词"}</td>
      <td><button class="btn danger sm" data-id="${r.id}">删除</button></td>`;
    tb.appendChild(tr);
  });
  tb.querySelectorAll("button").forEach(b => b.onclick = async () => {
    await fetch(withBase(`/api/knowledge/chunks/${b.dataset.id}`), { method: "DELETE" }); loadKbChunks();
  });
}
$("#btnKbText").onclick = async () => {
  const text = $("#kbText").value.trim();
  if (!text) { alert("请输入文本"); return; }
  $("#kbDocHint").textContent = "保存中…";
  try {
    const r = await jpost("/api/knowledge/text", { source: $("#kbTextSource").value || "手动录入", text });
    $("#kbText").value = "";
    $("#kbDocHint").textContent = `已保存：提炼 ${r.distilled || 0} 条 / 原文 ${r.raw || 0} 片`; loadKbChunks();
  } catch (e) { $("#kbDocHint").textContent = "保存失败：" + fmtErr(e); }
};
async function kbUpload(file) {
  const fd = new FormData(); fd.append("file", file);
  $("#kbDocHint").textContent = `上传 ${file.name}…`;
  try {
    const r = await fetch(withBase("/api/knowledge/upload"), { method: "POST", body: fd })
      .then(x => x.ok ? x.json() : x.json().then(j => Promise.reject(j)));
    if (r.processing) {
      $("#kbDocHint").textContent = r.message || `${file.name} 正在转写+提炼…`;
      setTimeout(loadKbChunks, 5000);
    } else {
      $("#kbDocHint").textContent = `已导入 ${file.name}：提炼 ${r.distilled || 0} 条 / 原文 ${r.raw || 0} 片`;
      loadKbChunks();
    }
  } catch (e) { $("#kbDocHint").textContent = "上传失败：" + fmtErr(e); }
}
(() => {
  const drop = $("#kbDrop"), input = $("#kbDocFile");
  if (!drop) return;
  drop.onclick = () => input.click();
  input.onchange = () => { if (input.files[0]) kbUpload(input.files[0]); input.value = ""; };
  drop.ondragover = (e) => { e.preventDefault(); drop.style.borderColor = "var(--blue)"; };
  drop.ondragleave = () => { drop.style.borderColor = "var(--border)"; };
  drop.ondrop = (e) => { e.preventDefault(); drop.style.borderColor = "var(--border)"; if (e.dataTransfer.files[0]) kbUpload(e.dataTransfer.files[0]); };
})();

// ---- 知识库扩展：直播技巧 / 运营经验 → 优秀节奏库 ----
let _rhythmCorpora = [];
let _rhythmTemplates = [];
let _rhythmLibraryLoading = false;
let _rhythmReloadQueued = false;
let _rhythmUploading = false;
const _RHYTHM_DOC_EXTS = new Set(["txt", "md", "docx", "pdf", "xlsx", "xlsm", "csv", "json"]);
const _RHYTHM_TYPE_LABELS = { live_skill: "直播技巧", operations: "运营经验" };
const _RHYTHM_STATUS_LABELS = {
  pending: "待处理", processing: "处理中", ready: "已提炼", completed: "已提炼", done: "已提炼",
  success: "已提炼", error: "处理失败", failed: "处理失败", adopted: "已采纳", rejected: "已驳回",
};
const _RHYTHM_STATUS_CLASSES = new Set([
  "pending", "processing", "ready", "completed", "done", "success", "error", "failed", "adopted", "rejected",
]);

function rhythmEl(tag, cls, text) {
  const node = el(tag, cls || "");
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function rhythmArray(payload, key) {
  if (Array.isArray(payload)) return payload;
  if (payload && Array.isArray(payload[key])) return payload[key];
  if (payload && Array.isArray(payload.items)) return payload.items;
  return [];
}

function rhythmTypeLabel(item) {
  return (item && item.corpus_type_label) || _RHYTHM_TYPE_LABELS[(item && item.corpus_type) || ""] || "未分类";
}

function rhythmStatus(status, template) {
  const key = String(status || "pending").toLowerCase();
  const label = template && key === "pending" ? "待审核" : (_RHYTHM_STATUS_LABELS[key] || status || "未知状态");
  const cls = _RHYTHM_STATUS_CLASSES.has(key) ? key : "unknown";
  return rhythmEl("span", `rhl-status ${cls}`, label);
}

function rhythmDate(value) {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value).slice(0, 16).replace("T", " ");
  return d.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}

function rhythmDuration(value) {
  const seconds = Math.max(0, Number(value) || 0);
  if (!seconds) return "";
  if (seconds < 60) return `${Math.round(seconds)} 秒`;
  const mins = Math.floor(seconds / 60), rest = Math.round(seconds % 60);
  return rest ? `${mins} 分 ${rest} 秒` : `${mins} 分钟`;
}

function rhythmTimepoint(value) {
  const seconds = Math.max(0, Math.round(Number(value) || 0));
  if (seconds < 60) return `${seconds}秒`;
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

function rhythmHint(id, message, state) {
  const node = $(id);
  if (!node) return;
  node.className = `rhl-hint${state ? " " + state : ""}`;
  node.textContent = message || "";
}

function rhythmResultOk(result) {
  if (result && result.ok === false) throw new Error(result.error || result.detail || "操作失败");
  return result;
}

function rhythmButton(label, className, handler) {
  const button = rhythmEl("button", className, label);
  button.type = "button";
  button.onclick = handler;
  return button;
}

function renderRhythmCorpora(error) {
  const box = $("#rhythmCorpusList"), count = $("#rhythmCorpusCount");
  if (!box) return;
  box.replaceChildren();
  if (count) count.textContent = String(_rhythmCorpora.length);
  if (error) {
    box.appendChild(rhythmEl("div", "empty", `语料加载失败：${fmtErr(error)}`));
    return;
  }
  if (!_rhythmCorpora.length) {
    box.appendChild(rhythmEl("div", "empty", "还没有直播技巧或运营经验语料。"));
    return;
  }
  _rhythmCorpora.forEach(corpus => {
    const item = rhythmEl("div", "rhl-corpus");
    const head = rhythmEl("div", "rhl-corpus-head");
    head.appendChild(rhythmEl("span", "rhl-corpus-source", corpus.source || "未标注来源"));
    head.appendChild(rhythmStatus(corpus.status, false));
    item.appendChild(head);

    const meta = rhythmEl("div", "rhl-corpus-meta");
    meta.appendChild(rhythmEl("span", "rhl-type", rhythmTypeLabel(corpus)));
    meta.appendChild(rhythmEl("span", "", `模板 ${Number(corpus.template_count) || 0} 条`));
    if (corpus.llm_provider) meta.appendChild(rhythmEl("span", "", `模型：${corpus.llm_provider}`));
    if (corpus.created_at) meta.appendChild(rhythmEl("span", "", rhythmDate(corpus.created_at)));
    item.appendChild(meta);

    if (corpus.error_msg) item.appendChild(rhythmEl("div", "rhl-corpus-error", corpus.error_msg));
    const actions = rhythmEl("div", "rhl-corpus-actions");
    const corpusStatusKey = String(corpus.status || "").toLowerCase();
    if ((corpusStatusKey === "error" || corpusStatusKey === "failed") && corpus.id != null) {
      actions.appendChild(rhythmButton("重新提炼", "btn ghost sm", button => retryRhythmCorpus(corpus, button.currentTarget)));
    }
    if (corpus.id != null) {
      actions.appendChild(rhythmButton("删除", "btn danger sm", button => deleteRhythmCorpus(corpus, button.currentTarget)));
    }
    if (actions.childNodes.length) item.appendChild(actions);
    box.appendChild(item);
  });
}

function refreshRhythmSceneFilter() {
  const select = $("#rhythmSceneFilter");
  if (!select) return;
  const previous = select.value;
  const scenes = new Map();
  _rhythmTemplates.forEach(t => {
    const key = String(t.scene || "").trim();
    if (key) scenes.set(key, t.scene_label || key);
  });
  select.replaceChildren();
  const all = document.createElement("option");
  all.value = ""; all.textContent = "全部场景"; select.appendChild(all);
  Array.from(scenes.entries()).sort((a, b) => String(a[1]).localeCompare(String(b[1]), "zh-CN")).forEach(([key, label]) => {
    const option = document.createElement("option");
    option.value = key; option.textContent = label; select.appendChild(option);
  });
  if (scenes.has(previous)) select.value = previous;
}

function rhythmTags(value) {
  if (Array.isArray(value)) return value.filter(x => x != null && String(x).trim()).map(String);
  if (!value) return [];
  if (typeof value === "string") {
    try {
      const parsed = JSON.parse(value);
      if (Array.isArray(parsed)) return parsed.filter(x => x != null && String(x).trim()).map(String);
    } catch (e) { /* 普通逗号分隔文本 */ }
    return value.split(/[,，、|]/).map(x => x.trim()).filter(Boolean);
  }
  return [];
}

function rhythmSteps(value) {
  if (Array.isArray(value)) return value.filter(x => x && typeof x === "object").slice();
  if (typeof value === "string" && value.trim()) {
    try {
      const parsed = JSON.parse(value);
      return Array.isArray(parsed) ? parsed.filter(x => x && typeof x === "object") : [];
    }
    catch (e) { return []; }
  }
  return [];
}

function addRhythmInfo(box, label, value) {
  if (value === undefined || value === null || String(value).trim() === "") return;
  const cell = rhythmEl("div", "rhl-info");
  cell.appendChild(rhythmEl("i", "", label));
  cell.appendChild(rhythmEl("span", "", value));
  box.appendChild(cell);
}

function renderRhythmTemplate(template) {
  const statusKey = String(template.status || "pending").toLowerCase();
  const cardStatus = ["pending", "adopted", "rejected"].includes(statusKey) ? ` ${statusKey}` : "";
  const card = rhythmEl("article", `rhl-template${cardStatus}`);
  const head = rhythmEl("div", "rhl-template-head");
  const kicker = rhythmEl("div", "rhl-template-kicker");
  kicker.appendChild(rhythmEl("span", "rhl-type", rhythmTypeLabel(template)));
  kicker.appendChild(rhythmEl("span", "rhl-scene", template.scene_label || template.scene || "通用"));
  kicker.appendChild(rhythmStatus(statusKey, true));
  const validation = template.validation || {};
  const validationLabel = ({ effective: "多场实测有效", ineffective: "多场实测无效", mixed: "效果不稳定", observing: "初步信号·观察中", unverified: "待实测" })[validation.status] || "待实测";
  kicker.appendChild(rhythmEl("span", `rhl-status ${validation.status || "unverified"}`,
    `${validationLabel}${validation.verified_count ? ` ${validation.effective_count || 0}/${validation.verified_count}` : ""}`));
  head.appendChild(kicker);
  head.appendChild(rhythmEl("div", "rhl-template-title", template.title || "未命名节奏模板"));
  const meta = rhythmEl("div", "rhl-template-meta");
  const totalDuration = rhythmDuration(template.total_duration_sec);
  if (totalDuration) meta.appendChild(rhythmEl("span", "", `总时长 ${totalDuration}`));
  if (template.source) meta.appendChild(rhythmEl("span", "", `来源：${template.source}`));
  if (typeof template.indexed === "boolean") {
    const retrieval = template.indexed ? "✓ 已建向量索引"
      : (template.retrievable ? "✓ 关键词可检索" : "待采纳后进入知识召回");
    meta.appendChild(rhythmEl("span", "", retrieval));
  }
  if (template.created_at) meta.appendChild(rhythmEl("span", "", rhythmDate(template.created_at)));
  if (validation.trial_count) meta.appendChild(rhythmEl("span", "",
    `已登记 ${validation.trial_count} 场 · ${validation.streamer_count || 0} 位主播 · 有效率 ${validation.effective_rate == null ? "待积累" : validation.effective_rate + "%"}`));
  if (meta.childNodes.length) head.appendChild(meta);
  card.appendChild(head);

  const body = rhythmEl("div", "rhl-template-body");
  if (template.summary) body.appendChild(rhythmEl("div", "rhl-summary", template.summary));
  const info = rhythmEl("div", "rhl-info-grid");
  addRhythmInfo(info, "适用时机", template.applicable_when);
  addRhythmInfo(info, "目标人群", template.audience);
  addRhythmInfo(info, "节奏目标", template.goal);
  if (info.childNodes.length) body.appendChild(info);

  const steps = rhythmSteps(template.steps).sort((a, b) => (Number(a.order) || 0) - (Number(b.order) || 0));
  if (steps.length) {
    body.appendChild(rhythmEl("div", "rhl-steps-title", `分步节奏 · ${steps.length} 步`));
    const timeline = rhythmEl("div", "rhl-timeline");
    steps.forEach((step, index) => {
      const row = rhythmEl("div", "rhl-step");
      row.appendChild(rhythmEl("div", "rhl-step-no", step.order || index + 1));
      const stepCard = rhythmEl("div", "rhl-step-card");
      const stepHead = rhythmEl("div", "rhl-step-head");
      stepHead.appendChild(rhythmEl("span", "rhl-step-phase", step.phase || `第 ${index + 1} 步`));
      const startSec = Number(step.start_sec), endSec = Number(step.end_sec);
      const duration = Number.isFinite(startSec) && Number.isFinite(endSec) && endSec > startSec
        ? `${rhythmTimepoint(startSec)}–${rhythmTimepoint(endSec)}` : rhythmDuration(step.duration_sec);
      if (duration) stepHead.appendChild(rhythmEl("span", "rhl-duration", duration));
      stepCard.appendChild(stepHead);
      if (step.content) stepCard.appendChild(rhythmEl("div", "rhl-step-content", step.content));
      const details = [];
      if (step.trigger) details.push(`触发条件：${step.trigger}`);
      if (step.action) details.push(`主播动作：${step.action}`);
      if (step.duration_basis) {
        const basis = { source: "原文给定", estimated: "模型估算" }[step.duration_basis] || step.duration_basis;
        details.push(`时长依据：${basis}`);
      }
      if (details.length) stepCard.appendChild(rhythmEl("div", "rhl-step-detail", details.join("\n")));
      if (step.example_script) stepCard.appendChild(rhythmEl("div", "rhl-example", `示范话术：${step.example_script}`));
      row.appendChild(stepCard); timeline.appendChild(row);
    });
    body.appendChild(timeline);
  }

  const tags = rhythmTags(template.tags);
  if (tags.length) {
    const tagsBox = rhythmEl("div", "rhl-tags");
    tags.forEach(tag => tagsBox.appendChild(rhythmEl("span", "rhl-tag", tag)));
    body.appendChild(tagsBox);
  }
  if (template.source_excerpt) body.appendChild(rhythmEl("div", "rhl-excerpt", `来源摘录：${template.source_excerpt}`));
  if (statusKey === "pending" && template.id != null) {
    const actions = rhythmEl("div", "rhl-template-actions");
    actions.appendChild(rhythmButton("✓ 采纳入库", "btn sm", event => actRhythmTemplate(template, "adopt", event.currentTarget)));
    actions.appendChild(rhythmButton("驳回", "btn ghost sm", event => actRhythmTemplate(template, "reject", event.currentTarget)));
    body.appendChild(actions);
  }
  card.appendChild(body);
  return card;
}

function renderRhythmTemplates(error) {
  const box = $("#rhythmTemplateList"), count = $("#rhythmTemplateCount"), hint = $("#rhythmLibraryHint");
  if (!box) return;
  box.replaceChildren();
  if (error) {
    if (count) count.textContent = "0";
    if (hint) hint.textContent = `节奏模板加载失败：${fmtErr(error)}`;
    box.appendChild(rhythmEl("div", "empty", "暂时无法读取优秀节奏库，请稍后刷新。"));
    return;
  }
  refreshRhythmSceneFilter();
  const status = $("#rhythmStatusFilter").value;
  const corpusType = $("#rhythmTypeFilter").value;
  const scene = $("#rhythmSceneFilter").value;
  const rows = _rhythmTemplates.filter(t => (!status || t.status === status)
    && (!corpusType || t.corpus_type === corpusType) && (!scene || t.scene === scene));
  if (count) count.textContent = rows.length === _rhythmTemplates.length ? String(rows.length) : `${rows.length} / ${_rhythmTemplates.length}`;
  if (hint) {
    const pending = _rhythmTemplates.filter(t => t.status === "pending").length;
    hint.textContent = `当前显示 ${rows.length} 条，共 ${_rhythmTemplates.length} 条；待审核 ${pending} 条。`;
  }
  if (!rows.length) {
    box.appendChild(rhythmEl("div", "empty", _rhythmTemplates.length ? "当前筛选条件下没有节奏模板。" : "还没有节奏模板，请先录入直播技巧或运营经验。"));
    return;
  }
  rows.forEach(template => box.appendChild(renderRhythmTemplate(template)));
}

async function loadRhythmLibrary() {
  if (!$("#rhythmCorpusList")) return;
  if (_rhythmLibraryLoading) { _rhythmReloadQueued = true; return; }
  _rhythmLibraryLoading = true;
  const refresh = $("#btnRhythmRefresh");
  if (refresh) { refresh.disabled = true; refresh.textContent = "刷新中…"; }
  if (!_rhythmCorpora.length) $("#rhythmCorpusList").replaceChildren(rhythmEl("div", "empty", "正在加载语料…"));
  if (!_rhythmTemplates.length) $("#rhythmTemplateList").replaceChildren(rhythmEl("div", "empty", "正在加载节奏模板…"));
  const settled = promise => promise.then(value => ({ value })).catch(error => ({ error }));
  try {
    const [corporaResult, templatesResult] = await Promise.all([
      settled(api("/api/rhythm-library/corpora")),
      settled(api("/api/rhythm-library/templates")),
    ]);
    if (!corporaResult.error) _rhythmCorpora = rhythmArray(corporaResult.value, "corpora");
    if (!templatesResult.error) _rhythmTemplates = rhythmArray(templatesResult.value, "templates");
    renderRhythmCorpora(corporaResult.error);
    renderRhythmTemplates(templatesResult.error);
  } finally {
    _rhythmLibraryLoading = false;
    if (refresh) { refresh.disabled = false; refresh.textContent = "刷新"; }
    if (_rhythmReloadQueued) {
      _rhythmReloadQueued = false;
      setTimeout(loadRhythmLibrary, 0);
    }
  }
}

function rhythmCorpusForm() {
  const corpusType = $("#rhythmCorpusType").value;
  const source = $("#rhythmCorpusSource").value.trim();
  return { corpusType, source };
}

async function submitRhythmCorpusText() {
  const textBox = $("#rhythmCorpusText"), button = $("#btnRhythmCorpusText");
  const text = textBox.value.trim();
  if (!text) { rhythmHint("#rhythmIngestHint", "请先粘贴直播技巧或运营经验文本。", "error"); textBox.focus(); return; }
  const form = rhythmCorpusForm();
  const source = form.source || (form.corpusType === "operations" ? "手动录入·运营经验" : "手动录入·直播技巧");
  button.disabled = true; button.textContent = "AI 拆解中…";
  rhythmHint("#rhythmIngestHint", "大模型正在拆解文本并生成话术节奏模板，请稍候…");
  try {
    const result = rhythmResultOk(await jpost("/api/rhythm-library/corpora/text", {
      source, corpus_type: form.corpusType, text,
    }));
    textBox.value = "";
    const templates = rhythmArray(result, "templates");
    const corpus = (result && result.corpus) || {};
    if (["error", "failed"].includes(String(corpus.status || "").toLowerCase())) {
      rhythmHint("#rhythmIngestHint", `语料已保存，但提炼失败：${corpus.error_msg || "请在语料列表中重试"}`, "error");
    } else {
      rhythmHint("#rhythmIngestHint", `入库完成，已生成 ${templates.length || Number(corpus.template_count) || 0} 条待审核节奏模板。`, "ok");
    }
    await loadRhythmLibrary();
  } catch (e) {
    rhythmHint("#rhythmIngestHint", `入库失败：${fmtErr(e)}`, "error");
  } finally {
    button.disabled = false; button.textContent = "AI 拆解并入库";
  }
}

function rhythmFileAllowed(file) {
  const name = String((file && file.name) || "");
  const dot = name.lastIndexOf(".");
  return dot > -1 && _RHYTHM_DOC_EXTS.has(name.slice(dot + 1).toLowerCase());
}

async function uploadRhythmCorpus(file) {
  if (!file || _rhythmUploading) return;
  if (!rhythmFileAllowed(file)) {
    rhythmHint("#rhythmIngestHint", "仅支持 TXT、MD、DOCX、PDF、XLSX、CSV、JSON 文本类文档，不接收音视频。", "error");
    return;
  }
  const form = rhythmCorpusForm(), drop = $("#rhythmCorpusDrop");
  const fd = new FormData();
  fd.append("file", file);
  fd.append("corpus_type", form.corpusType);
  if (form.source) fd.append("source", form.source);
  _rhythmUploading = true;
  drop.setAttribute("aria-busy", "true"); drop.style.pointerEvents = "none"; drop.style.opacity = ".65";
  rhythmHint("#rhythmIngestHint", `正在上传并拆解 ${file.name}…`);
  try {
    const result = rhythmResultOk(await api("/api/rhythm-library/corpora/upload", { method: "POST", body: fd }));
    const templates = rhythmArray(result, "templates"), corpus = (result && result.corpus) || {};
    if (["error", "failed"].includes(String(corpus.status || "").toLowerCase())) {
      rhythmHint("#rhythmIngestHint", `文档已保存，但提炼失败：${corpus.error_msg || "请在语料列表中重试"}`, "error");
    } else {
      rhythmHint("#rhythmIngestHint", `已导入 ${file.name}，生成 ${templates.length || Number(corpus.template_count) || 0} 条待审核节奏模板。`, "ok");
    }
    await loadRhythmLibrary();
  } catch (e) {
    rhythmHint("#rhythmIngestHint", `文档入库失败：${fmtErr(e)}`, "error");
  } finally {
    _rhythmUploading = false;
    drop.removeAttribute("aria-busy"); drop.style.pointerEvents = ""; drop.style.opacity = "";
  }
}

async function retryRhythmCorpus(corpus, button) {
  if (!corpus || corpus.id == null) return;
  button.disabled = true; button.textContent = "重试中…";
  rhythmHint("#rhythmIngestHint", "正在重新调用大模型提炼该语料…");
  try {
    const result = rhythmResultOk(await api(`/api/rhythm-library/corpora/${encodeURIComponent(String(corpus.id))}/retry`, { method: "POST" }));
    const templates = rhythmArray(result, "templates"), retried = (result && result.corpus) || {};
    await loadRhythmLibrary();
    if (["error", "failed"].includes(String(retried.status || "").toLowerCase())) {
      rhythmHint("#rhythmIngestHint", `重新提炼仍失败：${retried.error_msg || "请检查模型配置或语料内容后再试"}`, "error");
    } else {
      rhythmHint("#rhythmIngestHint", `重新提炼完成，生成 ${templates.length || Number(retried.template_count) || 0} 条模板。`, "ok");
    }
  } catch (e) {
    rhythmHint("#rhythmIngestHint", `重新提炼失败：${fmtErr(e)}`, "error");
    button.disabled = false; button.textContent = "重新提炼";
  }
}

async function deleteRhythmCorpus(corpus, button) {
  if (!corpus || corpus.id == null || !confirm("确定删除这条语料及由它生成的节奏模板吗？")) return;
  button.disabled = true; button.textContent = "删除中…";
  try {
    rhythmResultOk(await api(`/api/rhythm-library/corpora/${encodeURIComponent(String(corpus.id))}`, { method: "DELETE" }));
    await loadRhythmLibrary();
    rhythmHint("#rhythmIngestHint", "语料及关联节奏模板已删除。", "ok");
  } catch (e) {
    rhythmHint("#rhythmIngestHint", `删除失败：${fmtErr(e)}`, "error");
    button.disabled = false; button.textContent = "删除";
  }
}

async function actRhythmTemplate(template, action, button) {
  if (!template || template.id == null) return;
  const card = button.closest(".rhl-template");
  if (card) card.querySelectorAll("button").forEach(x => { x.disabled = true; });
  button.textContent = action === "adopt" ? "采纳中…" : "驳回中…";
  const verb = action === "adopt" ? "adopt" : "reject";
  try {
    rhythmResultOk(await api(`/api/rhythm-library/templates/${encodeURIComponent(String(template.id))}/${verb}`, { method: "POST" }));
    await loadRhythmLibrary();
    const hint = $("#rhythmLibraryHint");
    if (hint) hint.textContent = action === "adopt" ? "模板已采纳并进入优秀节奏库。" : "模板已驳回，仍可通过状态筛选查看。";
  } catch (e) {
    const hint = $("#rhythmLibraryHint");
    if (hint) hint.textContent = `${action === "adopt" ? "采纳" : "驳回"}失败：${fmtErr(e)}`;
    if (card) card.querySelectorAll("button").forEach(x => { x.disabled = false; });
    button.textContent = action === "adopt" ? "✓ 采纳入库" : "驳回";
  }
}

$("#btnRhythmCorpusText").onclick = submitRhythmCorpusText;
$("#btnRhythmRefresh").onclick = loadRhythmLibrary;
["#rhythmStatusFilter", "#rhythmTypeFilter", "#rhythmSceneFilter"].forEach(id => { $(id).onchange = () => renderRhythmTemplates(); });
(() => {
  const drop = $("#rhythmCorpusDrop"), input = $("#rhythmCorpusFile");
  if (!drop || !input) return;
  const choose = () => { if (!_rhythmUploading) input.click(); };
  drop.onclick = choose;
  drop.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); choose(); } };
  input.onchange = () => { if (input.files[0]) uploadRhythmCorpus(input.files[0]); input.value = ""; };
  drop.ondragover = e => { e.preventDefault(); if (!_rhythmUploading) drop.classList.add("drag"); };
  drop.ondragleave = () => drop.classList.remove("drag");
  drop.ondrop = e => {
    e.preventDefault(); drop.classList.remove("drag");
    if (!_rhythmUploading && e.dataTransfer.files[0]) uploadRhythmCorpus(e.dataTransfer.files[0]);
  };
})();

// 历史列表导出按钮
async function exportSession(id) { go(`/api/sessions/${id}/export.md`); }

// ===================== 登录（本机应急口令框 / 统一登录提示 / 退出） =====================
// 公网访问一律走平台统一登录（见 authBounce）；这个口令框只在服务器本机直连时出现。
function showLogin() {
  // 登录态过期：断开现场、清当前场次，避免重登后串到旧场次
  wsWantOpen = false;
  if (ws) { try { ws.close(); } catch (e) { } ws = null; }
  curSession = null;
  _histCache = [];
  ["#homeRecent", "#histRows", "#streamFlag", "#streamScript", "#summaryGrid"].forEach(selector => {
    const node = $(selector); if (node) node.innerHTML = "";
  });
  closeModal();
  $("#loginForm").style.display = "";
  $("#loginOverlay").style.display = "flex";
  setTimeout(() => $("#loginPwd").focus(), 50);
}
function showAuthNotice(text) {
  const box = $("#loginOverlay"); if (!box) return;   // 签名外链页没有这个框
  $("#loginForm").style.display = "none";
  $("#loginHint").textContent = text;
  box.style.display = "flex";
}
function hideLogin() { $("#loginOverlay").style.display = "none"; }
$("#loginBtn").onclick = async () => {
  try {
    await jpost("/api/login", { password: $("#loginPwd").value });
    hideLogin(); location.reload();
  } catch (e) { $("#loginHint").textContent = e && e.status === 401 ? "口令错误，请重试" : fmtErr(e, "登录失败"); }
};
$("#loginPwd").addEventListener("keydown", e => { if (e.key === "Enter") $("#loginBtn").click(); });
// 退出：清本系统 cookie（应急会话）后整页跳平台 /logout（平台清掉统一登录态并回到登录页）
$("#btnLogout").onclick = async () => {
  try { await fetch(withBase("/api/logout"), { method: "POST" }); } catch (e) { }
  if (["emergency", "password"].includes(_authMe.auth)) { location.reload(); return; }
  location.href = _authMe.logout_url || "/logout";
};

// ===================== 主播免密只读直达页（业绩后台「查看完整复盘」跳转） =====================
// URL 形如 /?view=48&s=1&exp=...&sig=...：验签后展示该主播本场全貌（视频/逐字稿/点评/总结），
// 无登录、无导航、无任何写操作入口。
async function renderViewerMode(qs) {
  document.documentElement.classList.remove("fupan-authing");
  document.title = "复盘详情";
  document.body.innerHTML = `<div id="viewerRoot" style="max-width:1280px;margin:0 auto;padding:16px 16px 48px">
    <div class="faint" style="padding:40px;text-align:center">复盘数据加载中…</div></div>`;
  const root = document.getElementById("viewerRoot");
  let d;
  try {
    const r = await fetch(withBase(`/api/partner/view/${encodeURIComponent(qs.get("view"))}?s=${encodeURIComponent(qs.get("s"))}&exp=${encodeURIComponent(qs.get("exp"))}&sig=${encodeURIComponent(qs.get("sig"))}`));
    if (!r.ok) throw await r.json().catch(() => ({ detail: r.statusText }));
    d = await r.json();
  } catch (e) {
    root.innerHTML = `<div class="card" style="padding:32px;text-align:center">⚠️ ${esc(fmtErr(e, "加载失败"))}
      <div class="faint" style="margin-top:8px">链接可能已过期（有效期 4 小时），请回业绩后台重新点击「查看完整复盘」</div></div>`;
    return;
  }

  const color = "#63A7FF";
  const name = d.streamer.name;
  const tr = d.transcripts || [];
  const cr = d.critiques || [];
  const flags = cr.filter(c => c.level === "sentence" || c.level === "screen");
  const segs = cr.filter(c => c.level === "segment");
  const sums = cr.filter(c => c.level === "summary");
  const hasPlayer = Boolean(d.media && d.media.available && d.media.url);

  const dur = (d.turns || []).reduce((a, t) => a + Math.max(0, (t.end_ms || 0) - (t.start_ms || 0)), 0);
  const head = `<div class="card" style="margin-bottom:12px;padding:14px 18px">
    <div style="display:flex;flex-wrap:wrap;gap:10px;align-items:baseline">
      <span class="dot" style="width:12px;height:12px;background:${color}"></span>
      <b style="font-size:16px">${esc(name)} 的复盘</b>
      <span class="faint">${esc(d.session.title || "")}</span>
      <span class="faint" style="margin-left:auto">${esc((d.session.started_at || "").replace("T", " ").slice(0, 16))} · 上播 ${Math.round(dur / 60000)} 分钟 · ${tr.length} 句 · ${flags.length} 个问题点</span>
    </div></div>`;

  let playerHtml = "";
  if (hasPlayer) {
    const elt = d.media.kind === "video"
      ? `<video id="detailPlayer" controls preload="metadata" style="height:56vh;aspect-ratio:9/16;width:auto;max-width:100%;background:#000;border-radius:10px;object-fit:contain"></video>`
      : `<audio id="detailPlayer" controls preload="metadata" style="width:100%"></audio>`;
    playerHtml = `<div class="card"><div class="card-head"><h3>${d.media.kind === "video" ? "🎬 直播回看" : "🔊 录音回放"} · 点右侧话术句跳转</h3></div>${elt}</div>`;
  } else {
    playerHtml = `<div class="card" style="margin-bottom:12px;padding:12px 16px"><span class="faint">🎬 回放暂不可用（直播中或录像仍在处理，稍后刷新业绩后台重新打开）</span></div>`;
  }

  const sumHtml = sums.map(c => {
    const sd = parseJSON(c.data) || {};
    sd.summary = sd.summary || c.comment;
    if (sd.overall_score == null) sd.overall_score = c.score;
    sd.llm = sd.llm || c.llm_provider;
    sd.llm_requested = sd.llm_requested || c.llm_requested_provider;
    sd.is_fallback = Boolean(sd.is_fallback || c.is_fallback);
    return `<div class="sum-card" style="margin-bottom:12px">${reportCardHTML(name, color, sd)}</div>`;
  }).join("");

  const _vStartedAt = (d.session && d.session.started_at) || null;
  const lines = tr.map(t =>
    `<div class="line" data-seek="${t.start_ms}"${hasPlayer ? ' style="cursor:pointer"' : ""}><div class="meta"><span class="t">${wallT(_vStartedAt, t.start_ms)}</span></div>${esc(t.corrected_text)}</div>`).join("") || '<div class="faint">无逐字稿</div>';
  const flagHtml = flags.map(c =>
    `<div class="line fl ${c.severity === "error" ? "error" : ""} ${c.level === "screen" ? "screen" : ""}" data-seek="${c.start_ms}" data-ckey="${esc(viewCritKey(c))}"${hasPlayer ? ' style="cursor:pointer"' : ""}><div class="meta"><span class="tag cat">${esc(c.category)}</span>${c.level === "screen" ? '<span class="tag src">画面</span>' : ""}<span class="t">${wallT(_vStartedAt, c.start_ms)}</span></div>${esc(c.comment)}<div class="quote">原话：${esc(c.quote)}</div></div>`).join("") || '<div class="faint">无</div>';
  const segHtml = segs.map(c => {
    const sd = parseJSON(c.data) || {};
    const seek = hasPlayer ? `<button class="btn ghost sm" data-seek="${c.start_ms}" style="margin-top:6px">▶ 播放本段（${wallT(detailState.startedAt, c.start_ms)}）</button>` : "";
    return `<div class="line fl seg" data-start="${c.start_ms}" data-ckey="${esc(viewCritKey(c))}">${c.score != null ? "<b>" + c.score + "分</b> " : ""}${sceneHTML(sd.scene_label)}${sd.asr_emotion_label ? emotionHTML(sd.asr_emotion_label, true) : emotionHTML(sd.emotion)}${toneHTML(sd.pitch)}${llmBadge(sd.llm || c.llm_provider, sd.is_fallback || c.is_fallback, sd.llm_requested || c.llm_requested_provider)}${esc(c.comment)}${emotionMatchHTML(sd.emotion_match)}${loopHTML(sd.loop)}${rhythmHTML(sd.metrics)}${seek}</div>`;
  }).join("");

  const mainV = hasPlayer
    ? `<div class="av-grid">
        ${playerHtml}
        <div class="card"><div class="card-head"><h3>逐字稿（${tr.length} 句）</h3></div><div class="stream" id="dtScript" style="height:56vh">${lines}</div></div>
        <div class="card"><div class="card-head"><h3>问题（${flags.length}）+ 段级点评</h3></div><div class="stream" style="height:56vh">${flagHtml}${segHtml}</div></div>
      </div>`
    : `<div class="live-grid" style="margin-top:0">
        <div class="card"><div class="card-head"><h3>逐字稿（${tr.length} 句）</h3></div><div class="stream" id="dtScript" style="height:56vh">${lines}</div></div>
        <div class="card"><div class="card-head"><h3>句级/画面 问题（${flags.length}）+ 段级点评</h3></div><div class="stream" style="height:56vh">${flagHtml}${segHtml}</div></div>
      </div>`;
  // viewer 页同款页签化(与详情弹窗一致):总览(总结)/回放对照/归因分析
  root.innerHTML = `${head}
    <div class="dt-tabs" id="vViewTabs">
      <span class="dt-tab${sumHtml ? " on" : ""}" data-pane="overview">📊 总览</span>
      <span class="dt-tab${sumHtml ? "" : " on"}" data-pane="replay">🎬 回放对照</span>
      <span class="dt-tab" data-pane="attr">💰 归因分析</span>
    </div>
    <div class="dt-pane" data-pane="overview"${sumHtml ? "" : " hidden"}>${sumHtml || ""}</div>
    <div class="dt-pane" data-pane="replay"${sumHtml ? " hidden" : ""}>${mainV}</div>
    <div class="dt-pane" data-pane="attr" hidden><div id="danmakuBoxV"></div><div id="attrBoxV"></div></div>`;
  root.querySelectorAll("#vViewTabs .dt-tab").forEach(t => t.onclick = () => {
    root.querySelectorAll("#vViewTabs .dt-tab").forEach(x => x.classList.toggle("on", x === t));
    root.querySelectorAll(".dt-pane").forEach(pn => { pn.hidden = pn.dataset.pane !== t.dataset.pane; });
  });

  // 双列联动 + 播放器同步（与详情弹窗同款交互）
  const pl = document.getElementById("detailPlayer");
  if (pl && hasPlayer) {
    const mSrc = String(d.media.url).startsWith("http") ? d.media.url : withBase(d.media.url);
    attachMedia(pl, mSrc, Boolean(d.media.hls));
  }
  const script = document.getElementById("dtScript");
  const tLines = script ? [...script.querySelectorAll(".line[data-seek]")] : [];
  const rItems = [...root.querySelectorAll(".line.fl")].map(e => ({ e, ts: +(e.dataset.seek || e.dataset.start || 0) }));
  const tsOf = (e) => +(e.dataset.seek || e.dataset.start || 0);
  const nearest = (list, ms) => {
    if (!list.length) return null;
    let best = null, bd = Infinity;
    for (const x of list) { const dd = Math.abs((x.ts != null ? x.ts : tsOf(x)) - ms); if (dd < bd) { bd = dd; best = x; } }
    return best && (best.e || best);
  };
  const flash = (el) => {
    if (!el) return;
    scrollInBox(el, true);
    el.classList.add("assoc-hl");
    setTimeout(() => el.classList.remove("assoc-hl"), 2200);
  };
  root.querySelectorAll("[data-seek],[data-start]").forEach(elm => elm.addEventListener("click", (ev) => {
    if (elm.classList.contains("seg") && ev.target.tagName === "BUTTON") return;
    const ms = tsOf(elm);
    if (pl) { try { pl.currentTime = ms / 1000; pl.play().catch(() => { }); } catch (e) { } }
    if (script && script.contains(elm)) flash(nearest(rItems, ms));
    else if (elm.closest(".fl")) flash(nearest(tLines, ms));
  }));
  if (pl) {
    const sItems = [...root.querySelectorAll(".line.fl.seg[data-start]")];
    const pick = (arr, tms, key) => { let c = null; for (const e of arr) { if ((+e.dataset[key] || 0) <= tms) c = e; else break; } return c; };
    let lastT = null, lastS = null;
    pl.addEventListener("timeupdate", () => {
      const tms = pl.currentTime * 1000;
      const ct = pick(tLines, tms, "seek");
      if (ct !== lastT) { if (lastT) lastT.classList.remove("playing"); if (ct) { ct.classList.add("playing"); scrollInBox(ct); } lastT = ct; }
      const cs = pick(sItems, tms, "start");
      if (cs !== lastS) { if (lastS) lastS.classList.remove("playing"); if (cs) { cs.classList.add("playing"); scrollInBox(cs); } lastS = cs; }
    });
  }
  // 阅读行为上报（业绩系统拉取）：与上面的交互互不依赖，任何异常都吞掉
  try { startViewTracking(qs, root, pl); } catch (e) { }

  // 整场归因（弱信号 + 成交），只读展示
  if (d.danmaku && d.danmaku.total) {
    const r = d.danmaku;
    const chips = (r.dist || []).map(x => `<span class="rchip"><i>${esc(x.label)}</i>${x.count}</span>`).join("");
    const tips = (r.attribution || []).map(t => `<li>${esc(t)}</li>`).join("");
    document.getElementById("danmakuBoxV").innerHTML = `<div class="card" style="margin-top:12px"><div class="card-head"><h3>📣 公屏弱信号归因</h3>
      <span class="faint" style="font-size:12px">${r.danmaku_total} 条公屏 · ${r.total} 条含信号</span></div>
      <div class="rhythm">${chips}</div>${tips ? `<ul class="tight" style="margin-top:8px">${tips}</ul>` : ""}</div>`;
  }
  if (d.attribution && d.attribution.total) {
    const r = d.attribution;
    const rows = (r.events || []).slice(0, 20).map(e => `<div class="attr-ev"><span class="t">${mmss(e.at)}</span><span class="attr-badge ${e.verdict === "话术驱动" ? "ok" : "dim"}">${esc(e.verdict)}</span><span class="attr-src">${esc(e.source)}</span><span class="attr-note">${esc(e.note)}</span></div>`).join("");
    const tips = (r.tips || []).map(t => `<li>${esc(t)}</li>`).join("");
    document.getElementById("attrBoxV").innerHTML = `<div class="card" style="margin-top:12px"><div class="card-head"><h3>💰 成交归因</h3>
      <span class="faint" style="font-size:12px">话术驱动 ${r.driven} · 自然/缺失 ${r.natural}</span></div>
      ${tips ? `<ul class="tight">${tips}</ul>` : ""}<div class="attr-list">${rows}</div></div>`;
  }
}

// ---- 直达页阅读行为上报（2026-09-29，业绩系统拉取主播"打开没、看了多久、看了哪些问题"）----
// 问题 key 与业绩系统同口径："{level}|{start_ms}|{category}"（start_ms 取整数毫秒，无类别记空串）。
function viewCritKey(c) {
  return `${c.level}|${Math.round(Number(c.start_ms) || 0)}|${c.category || ""}`;
}

// open：加载成功发一次；beat：每 15 秒报一次本周期增量（本周期什么都没有就不发）；
// 页面隐藏/离开（visibilitychange→hidden、pagehide）时用 sendBeacon 冲掉未报部分。
// active：逐秒累计，仅当页面在前台且（30 秒内有滚动/点击/触摸/键盘/鼠标移动，或录像在播）；
// video：timeupdate 时间差累加，拖动跳转（seeking 或单次增量 >2 秒）不算；
// clips：点问题/逐字稿等跳转播放的次数；seen：问题卡片可见面积 ≥50% 累计 ≥2 秒，或被点击。
// 上报失败一律静默；链接过期等 4xx 后停止上报。绝不影响页面原有功能。
function startViewTracking(qs, root, pl) {
  const url = withBase(`/api/partner/view/${encodeURIComponent(qs.get("view"))}/beacon?s=${encodeURIComponent(qs.get("s"))}&exp=${encodeURIComponent(qs.get("exp"))}&sig=${encodeURIComponent(qs.get("sig"))}`);
  let active = 0, video = 0, clips = 0, stopped = false, tickTimer = 0, beatTimer = 0;
  let lastInput = Date.now(), lastTick = performance.now();
  const seenNew = new Set(), seenDone = new Set();
  const onInput = () => { lastInput = Date.now(); };
  ["scroll", "wheel", "click", "touchstart", "keydown", "mousemove"].forEach(t =>
    document.addEventListener(t, onInput, { capture: true, passive: true }));

  // 问题卡片：IntersectionObserver 只维护"当前可见面积 ≥50%"的集合，计时交给逐秒记账
  const cards = [...root.querySelectorAll("[data-ckey]")];
  const inView = new Set(), dwell = new Map();
  let io = null;
  const markSeen = (k) => {
    if (!k || seenNew.has(k) || seenDone.has(k)) return;
    seenNew.add(k);
    cards.forEach(c => { if (c.dataset.ckey === k) { inView.delete(c); if (io) io.unobserve(c); } });
  };
  if (window.IntersectionObserver && cards.length) {
    io = new IntersectionObserver(ents => ents.forEach(en => {
      if (en.isIntersecting && en.intersectionRatio >= 0.49) inView.add(en.target); else inView.delete(en.target);
    }), { threshold: [0, 0.5, 1] });
    cards.forEach(c => io.observe(c));
  }

  // 逐秒记账；计时器被挂起/设备休眠造成的大间隔最多记 2 秒
  const accrue = (visible) => {
    const now = performance.now();
    const dt = Math.min(Math.max(0, (now - lastTick) / 1000), 2);
    lastTick = now;
    if (!visible || !dt) return;
    const playing = Boolean(pl && !pl.paused && !pl.ended);
    if (playing || Date.now() - lastInput <= 30000) active += dt;
    inView.forEach(c => {
      const k = c.dataset.ckey;
      const ms = (dwell.get(k) || 0) + dt * 1000;
      dwell.set(k, ms);
      if (ms >= 2000) markSeen(k);
    });
  };

  if (pl) {
    let lastVT = null;
    pl.addEventListener("seeking", () => { lastVT = null; });
    pl.addEventListener("timeupdate", () => {
      const t = pl.currentTime;
      if (lastVT != null && !pl.seeking) { const dv = t - lastVT; if (dv > 0 && dv <= 2) video += dv; }
      lastVT = t;
    });
  }
  // 只数真正挂了跳转播放的元素（与上面 [data-seek],[data-start] 点击处理同一批），一次点击记一次
  const seekEls = new Set(root.querySelectorAll("[data-seek],[data-start]"));
  root.addEventListener("click", (ev) => {
    const t = ev.target;
    if (!t || !t.closest) return;
    const card = t.closest("[data-ckey]");
    if (card) markSeen(card.dataset.ckey);
    if (pl && seekEls.has(t.closest("[data-seek],[data-start]"))) clips += 1;
  });

  const pending = () => active >= 0.5 || video >= 0.5 || clips > 0 || seenNew.size > 0;
  const take = (kind) => {
    const p = { kind, active: Math.round(active * 10) / 10, video: Math.round(video * 10) / 10, clips, seen: [...seenNew].slice(0, 100) };
    active = 0; video = 0; clips = 0;
    p.seen.forEach(k => { seenNew.delete(k); seenDone.add(k); });
    return p;
  };
  // 失败时只把问题 key 放回下轮重报（服务端去重，重报无害）；秒数/次数不补，免得重复记账
  const requeue = (p) => { p.seen.forEach(k => { seenDone.delete(k); seenNew.add(k); }); };
  const stop = () => {
    stopped = true;
    clearInterval(tickTimer); clearInterval(beatTimer);
    if (io) io.disconnect();
  };
  const post = (p) => {
    if (stopped) return;
    try {
      fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(p) })
        .then(r => { if (r.status >= 400 && r.status < 500) stop(); else if (!r.ok) requeue(p); })
        .catch(() => requeue(p));
    } catch (e) { requeue(p); }
  };
  // 服务端每次只收 100 个 key，积压多时分批冲（最多 6 批）
  const flush = () => {
    for (let i = 0; i < 6 && !stopped && pending(); i++) {
      const body = JSON.stringify(take("beat"));
      let queued = false;
      try { queued = Boolean(navigator.sendBeacon && navigator.sendBeacon(url, body)); } catch (e) { }
      if (!queued) { try { fetch(url, { method: "POST", body, keepalive: true }).catch(() => { }); } catch (e) { } }
    }
  };

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") { accrue(true); flush(); }
    else lastTick = performance.now();   // 回到前台：后台那段不算
  });
  window.addEventListener("pagehide", () => { accrue(document.visibilityState === "visible"); flush(); });
  tickTimer = setInterval(() => accrue(document.visibilityState === "visible"), 1000);
  beatTimer = setInterval(() => { if (!stopped && pending()) post(take("beat")); }, 15000);
  post({ kind: "open", active: 0, video: 0, clips: 0, seen: [] });
}

// 初始：签名直达页优先；否则验证登录后进工作台
const _viewQS = new URLSearchParams(location.search);
if (_viewQS.get("view") && _viewQS.get("sig")) {
  renderViewerMode(_viewQS);
} else {
  api("/api/me").then((me) => {
    authReady(me);
    // 直达模式（2026-07-15 深度融合）：#detail/<场次id> 时用全屏过场压住工作台、
    // 详情弹窗就绪即揭开——去掉"先见工作台再跳详情"的过渡。
    const _m = location.hash.match(/^#detail\/(\d+)/);
    if (_m) {
      const veil = document.createElement("div");
      veil.id = "directVeil";
      veil.style.cssText = "position:fixed;inset:0;z-index:300;background:#0a0d13;display:flex;align-items:center;justify-content:center;color:#8a93a6;font-size:14px;letter-spacing:.1em";
      veil.textContent = "正在打开复盘…";
      document.body.appendChild(veil);
      loadHome();
      loadDetail(Number(_m[1])).catch(() => {}).finally(() => veil.remove());
    } else {
      loadHome();
    }
  }).catch(() => {});
}

// ===================== v2 深度复盘（2026-07-15 页面改版）=====================
// 五个分析页签（语气语调/话术结构/质检/弹幕回复 + 罗盘分析插入 attr 页）——
// 三段式版式（结论条→折叠问题卡→总结区）+ 右侧播放坞（点击证据片段即坞内 HLS 定位播放）。
let _v2HlsDispose = null;
let _v2Ctx = null;   // {id, tr, media, startedAt}

function v2SevCls(s) { return s === "red" ? "r" : (s === "green" ? "g" : (s === "pending" ? "p" : "o")); }
// 视频-逐字稿对齐偏移(毫秒,按场次存 localStorage;正值=视频比逐字稿晚)
function avOffset() { return +(detailState.avOffset || 0); }
// 自动线性校准(2026-07-15):HLS 标称总长比墙钟虚高≈0.5%(片段时长舍入累计),10 小时漂移 3 分钟。
// scale = 视频总长 / 墙钟总长,seek 时 逐字稿ms×scale+offset → 视频秒;跟随高亮反算。
function avScale() { const s = +(detailState.avScale || 0); return s > 0.9 && s < 1.1 ? s : 1; }
function avToVideoSec(ms) { return (ms * avScale() + avOffset()) / 1000; }
function avFromVideoMs(sec) { return (sec * 1000 - avOffset()) / avScale(); }
function avCalcScale(pl) {
  try {
    const wallMs = detailState.endedAt && detailState.startedAt
      ? (new Date(detailState.endedAt) - new Date(detailState.startedAt)) : 0;
    if (pl && pl.duration > 600 && wallMs > 600_000) detailState.avScale = pl.duration * 1000 / wallMs;
  } catch (e) { }
}
function v2ChipCls(s) { return s === "red" ? "bad" : (s === "orange" ? "warn" : ""); }

function v2EvBtns(evs) {
  return (evs || []).map((e, i) =>
    `<button class="clip-btn" data-dock="${e.start_ms || 0}" data-lab="${esc((e.wall || "") + " " + (e.quote || "").slice(0, 16))}">▶ ${esc(e.wall || ("片段" + (i + 1)))}</button>`).join("");
}
function v2Verdict(d) {
  if (!d) return "";
  const chips = (d.chips || []).map(c => {
    const t = String(c);
    const cls = /红线|问题|流失|错位|缺|✗|失败/.test(t) ? "bad" : (/弱|拖|慢|⚠/.test(t) ? "warn" : "");
    return `<span class="vchip ${cls}">${esc(t)}</span>`;
  }).join("");
  return `<div class="v2verdict"><div class="vl">${esc(d.verdict || "")}</div>${chips ? `<div class="vc">${chips}</div>` : ""}</div>`;
}
function v2Card(no, sev, title, bodyHtml, evs, open) {
  return `<div class="v2q${open ? " open" : ""}"><div class="v2q-head" data-tg>
    <span class="v2sev ${v2SevCls(sev)}"></span><span class="v2no">${esc(no)}</span>
    <span class="v2title">${esc(title)}</span>
    <span class="v2clips">${v2EvBtns(evs)}</span><span class="v2arrow">▶</span></div>
    <div class="v2body">${bodyHtml}</div></div>`;
}
function v2Quote(ev) {
  if (!ev) return "";
  return `<div class="v2quote"><span class="t">${esc(ev.wall || "")}</span><span>${esc(ev.quote || "")}</span></div>`;
}

// ---------- 各页 HTML ----------
function v2PaneTone(d) {
  const cards = (d.issues || []).map((it, i) => v2Card(`问题 ${i + 1}`, it.sev, it.title,
    `<div class="v2lbl">当时在讲什么</div>${(it.evidence || []).map(v2Quote).join("")}
     <div class="v2lbl">为什么不匹配</div><div>${esc(it.detail || "")}</div>
     ${it.suggest_emotion ? `<div class="v2lbl">建议使用的情绪</div><div>💡 <span class="v2fix">${esc(it.suggest_emotion)}</span></div>` : ""}
     ${it.suggest_line ? `<div class="v2lbl">建议的代入感话术（可照读）</div><div class="v2fix">“${esc(it.suggest_line)}”</div>` : ""}`,
    it.evidence, i === 0)).join("");
  return v2Verdict(d) + (cards || '<div class="faint" style="padding:14px">本页无问题记录</div>');
}

const _V2COLORS = { "讲": "#0E6E5C", "答": "#4A90B8", "互": "#B07A18", "促": "#C2453A", "逼": "#C2453A", "开": "#7A5CA8", "服": "#3E7D74", "闲": "#6b7280" };
function v2SegColor(lbl) {
  for (const k in _V2COLORS) if ((lbl || "").includes(k)) return _V2COLORS[k];
  return "#5C6B64";
}
function v2PaneStruct(d) {
  const tl = d.timeline || [];
  const total = tl.length ? Math.max(...tl.map(x => x.end_ms)) - Math.min(...tl.map(x => x.start_ms)) : 0;
  const strip = tl.length ? `<div class="v2lg">${["讲款", "答疑", "互动", "促单", "闲场"].map((n, i) =>
    `<span><i style="background:${["#0E6E5C", "#4A90B8", "#B07A18", "#C2453A", "#6b7280"][i]}"></i>${n}</span>`).join("")}
    <span style="flex:1"></span>
    <span class="v2zoombar">缩放 <button data-zoom="1" class="on">1×</button><button data-zoom="3">3×</button><button data-zoom="6">6×</button><button data-zoom="12">12×</button></span></div>
    <div class="v2strip-wrap"><div class="v2strip" id="v2strip" style="min-width:100%">${tl.map(x => {
      const w = total ? Math.max(1, Math.round((x.end_ms - x.start_ms) / total * 100)) : 1;
      return `<div class="v2seg" style="flex:${w};background:${v2SegColor(x.label)}" data-dock="${x.start_ms}" data-lab="${esc(x.wall + " " + x.label)}" title="${esc(x.wall + " " + x.label)}">${w >= 7 ? esc(x.label.slice(0, 4)) : ""}</div>`;
    }).join("")}</div></div>
    <div class="faint" style="font-size:11.5px;margin-bottom:10px">▲ 本场实际结构（点色块回放该段,可缩放拖动细看）</div>` : "";
  const cards = (d.issues || []).map((it, i) => v2Card(`问题 ${i + 1}`, it.sev, it.title,
    `<div class="v2lbl">为什么是问题</div><div>${esc(it.why || "")}</div>
     <div class="v2lbl">应该怎么做</div><div class="v2fix">${esc(it.how || "")}</div>
     <div class="v2lbl">证据片段</div>${(it.evidence || []).map(v2Quote).join("")}`, it.evidence, i === 0)).join("");
  const frame = (d.frame || []).length ? `<div class="card" style="margin-top:12px;padding:12px 14px">
    <b style="font-size:13px">📐 标准结构对照（点格子看该环节示范话术）</b>
    <div class="v2frame">${d.frame.map(f => {
      const cls = f.status === "miss" ? "miss" : "";
      const mark = f.status === "ok" ? "✅" : (f.status === "weak" ? "⚠️" : "❌");
      return `<div class="v2fcell ${cls}" data-fc><b>${esc(f.name)}</b><span>${mark} ${esc(f.note || "")}</span><em>示范：“${esc(f.demo || "")}”</em></div>`;
    }).join("")}</div></div>` : "";
  return v2Verdict(d) + strip + (cards || "") + frame;
}

function v2PaneQc(d) {
  const cards = (d.issues || []).map((it, i) => v2Card(`问题 ${i + 1} · ${it.kind || ""}`, it.sev, it.title,
    `<div class="v2lbl">原话与位置</div>${(it.evidence || []).map(v2Quote).join("")}`, it.evidence, i === 0)).join("");
  return v2Verdict(d) + (cards || '<div class="faint" style="padding:14px">本场无质检问题 🎉</div>');
}

function v2DmRow(x, i, kindLabel) {
  const sevMap = { good: "green", bad: "red", ignored: "red", slow: "orange" };
  const vMap = { good: "答得好", bad: "答偏了", ignored: "漏答/答非所问", slow: "拖沓" };
  const strength = x.strength === "strong" ? "🔥强意向 · " : (x.strength === "weak" ? "弱意向 · " : "");
  return v2Card(`${kindLabel} ${i + 1} · ${strength}${vMap[x.verdict] || x.verdict}`, sevMap[x.verdict] || "orange",
    (x.dm || "").slice(0, 40),
    `<div class="v2lbl">弹幕</div><div><span class="v2dmb">${esc(x.name ? x.name + "：" : "")}${esc(x.dm || "")}</span> <span class="faint" style="font-size:11px">${esc(x.wall || "")}</span></div>
     ${x.basis ? `<div class="v2lbl">意向判定依据</div><div class="v2basis">${esc(x.basis)}</div>` : ""}
     <div class="v2lbl">${x.verdict === "good" ? "亮点" : "问题在哪"}</div><div>${esc(x.problem || "")}</div>
     ${x.better ? `<div class="v2lbl">应该怎么答（可照读）</div><div class="v2fix">“${esc(x.better)}”</div>` : ""}`,
    x.evidence, false);
}
function v2PaneDm(d) {
  const intent = (d.intent || []).map((x, i) => v2DmRow(x, i, "意向")).join("");
  const question = (d.question || []).map((x, i) => v2DmRow(x, i, "提问")).join("");
  return v2Verdict(d)
    + `<div style="font-weight:700;font-size:13px;margin:10px 0 6px">🛒 成交意向回复（${(d.intent || []).length}）</div>`
    + (intent || '<div class="faint">无</div>')
    + `<div style="font-weight:700;font-size:13px;margin:14px 0 6px">❓ 观众提问回复（${(d.question || []).length}）</div>`
    + (question || '<div class="faint">无</div>');
}

function v2PaneCompass(d) {
  const blocks = [["sales", "💰 成交分析"], ["fans", "➕ 增粉分析"], ["interact", "🔥 互动分析"]];
  const html = blocks.map(([k, label]) => {
    const rows = (d[k] || []).map((x, i) => {
      const moments = (x.moments || []).map(m =>
        `<div class="v2mo"><span class="mt">${esc(m.wall || "")}</span><span class="ma">${esc(m.act || "")}</span>
          <span style="flex:1">"${esc(m.quote || "")}"<div class="faint" style="font-size:11px">${esc(m.why || "")}</div></span>
          <button class="clip-btn" data-dock="${(m.evidence && m.evidence[0] || {}).start_ms || 0}" data-lab="${esc((m.wall || "") + " " + (m.act || ""))}">▶</button></div>`).join("");
      return v2Card(`${x.wall || ""} ${x.delta || ""}`, "green", x.title || "",
        `<div class="v2lbl">因果链（此前 30 分钟）</div><div>${esc(x.chain || "")}</div>
         ${moments ? `<div class="v2lbl">关键时刻线（促成下单的具体动作,逐条可回放）</div>${moments}` : ""}
         ${x.advice ? `<div class="v2lbl">可复制建议</div><div class="v2fix">${esc(x.advice)}</div>` : ""}`,
        x.evidence, i === 0 && k === "sales");
    }).join("");
    return `<div style="font-weight:700;font-size:13px;margin:12px 0 6px">${label}（${(d[k] || []).length}）</div>` + (rows || '<div class="faint">无事件</div>');
  }).join("");
  // 原型同款:三指标卡容器(数值与 sparkline 由 fillCompassKpis 异步填充,点峰值柱回放)
  const kpis = `<div class="v2kpi3" id="v2ckpis">
      <div class="v2kpi" data-ck="pay"><span>成交</span><b>…</b><div class="spark"></div></div>
      <div class="v2kpi" data-ck="fans"><span>增粉</span><b>…</b><div class="spark"></div></div>
      <div class="v2kpi" data-ck="cm"><span>互动(评论)</span><b>…</b><div class="spark"></div></div>
    </div>
    <div class="v2hint">点击上方任一指标峰值柱 → 回放该时段；下方是三维度的因果归纳（前后 30 分钟话术窗）。</div>`;
  return kpis + html;
}

// 罗盘三指标卡异步填充:拉 /compass 点位,分 10 桶算差分 sparkline,峰值柱可点回放
async function fillCompassKpis(id) {
  const host = document.getElementById("v2ckpis"); if (!host) return;
  const pts = await api(`/api/sessions/${id}/compass`).catch(() => []);
  if (!pts || !pts.length) return;
  const num = v => { const n = parseFloat(String(v == null ? "" : v).replace(/[,¥]/g, "")); return isNaN(n) ? null : n; };
  const series = key => pts.map(p => ({ ms: p.start_ms, v: num(p.data && p.data[key]) })).filter(x => x.v != null && x.v < 1e7);
  const defs = { pay: "直播间用户支付金额", fans: "新增粉丝数", cm: "评论次数" };
  const fmt = { pay: v => "¥" + Math.round(v).toLocaleString(), fans: v => "+" + Math.round(v), cm: v => Math.round(v) + " 条" };
  for (const ck in defs) {
    const cell = host.querySelector(`[data-ck="${ck}"]`); if (!cell) continue;
    const s = series(defs[ck]); if (s.length < 2) continue;
    const total = Math.max(0, s[s.length - 1].v - s[0].v);
    cell.querySelector("b").textContent = fmt[ck](total);
    const N = 10, lo = s[0].ms, span = Math.max(1, s[s.length - 1].ms - lo);
    const buckets = Array.from({ length: N }, () => ({ d: 0, ms: 0 }));
    for (let i = 1; i < s.length; i++) {
      const d = Math.max(0, s[i].v - s[i - 1].v);
      const bi = Math.min(N - 1, Math.floor((s[i].ms - lo) / span * N));
      buckets[bi].d += d; if (!buckets[bi].ms) buckets[bi].ms = s[i].ms;
    }
    const mx = Math.max(...buckets.map(b => b.d), 1);
    const th = [...buckets.map(b => b.d)].sort((a, b) => b - a)[2] || 0;   // 前三算峰
    cell.querySelector(".spark").innerHTML = buckets.map((b, i) => {
      const ms = b.ms || (lo + span / N * (i + 0.5));
      const pk = b.d > 0 && b.d >= th && th > 0;
      return `<i class="${pk ? "pk" : ""}" style="height:${Math.max(6, Math.round(b.d / mx * 100))}%" data-dock="${Math.round(ms)}" data-lab="${wallT(detailState.startedAt, ms)} ${cell.querySelector("span").textContent}峰" title="${wallT(detailState.startedAt, ms)}"></i>`;
    }).join("");
  }
}

function v2OverviewIdx(v2) {
  const item = (pane, name, d) => {
    if (!d) return "";
    const n = (d.issues || []).length || (d.intent || []).length + (d.question || []).length || (d.sales || []).length;
    return `<div class="v2q"><div class="v2q-head" data-jump="${pane}">
      <span class="v2sev ${n && /qc|tone|struct/.test(pane) ? "o" : "g"}"></span>
      <span class="v2no">${esc(name)}</span><span class="v2title">${esc(d.verdict || "")}</span><span class="v2arrow">→</span></div></div>`;
  };
  return `<div class="v2idx">
    ${item("v2tone", "语气语调", v2.tone)}${item("v2struct", "话术结构", v2.structure)}
    ${item("v2qc", "质检", v2.qc)}${item("v2dm", "弹幕回复", v2.danmaku)}${item("attr", "罗盘分析", v2.compass)}</div>`;
}

// ---------- 播放坞 ----------
function v2EnsureDock() {
  let dock = document.getElementById("v2dock");
  if (!dock) {
    const tmp = document.createElement("div"); tmp.innerHTML = v2DockHtml();
    dock = tmp.firstElementChild;
  }
  const box = $("#modalBody");
  const slot = box && box.querySelector(`.v2dockslot[data-slot="${detailState.viewPane}"]`);
  if (slot && !slot.contains(dock)) slot.appendChild(dock);
  return dock;
}
function v2DockHtml() {
  return `<div class="v2dock" id="v2dock">
    <div class="v2dock-head"><b id="v2dockTitle">▶ 片段回放</b><span class="v2dock-x" id="v2dockX" title="停止">✕</span></div>
    <video id="v2dockPlayer" controls playsinline preload="metadata"></video>
    <div class="v2dock-sub">逐字稿（±40 秒 · 点句跳转）</div>
    <div class="v2dock-script" id="v2dockScript"><div class="faint" style="padding:8px">点击左侧任意 ▶ 片段开始回放</div></div>
  </div>`;
}
function v2DockStop() {
  const pl = document.getElementById("v2dockPlayer");
  if (pl) { try { pl.pause(); } catch (e) { } }
  const dk = document.getElementById("v2dock");
  if (dk) dk.classList.remove("on");
  document.querySelectorAll(".clip-btn.playing").forEach(b => b.classList.remove("playing"));
  const t = document.getElementById("v2dockTitle"); if (t) t.textContent = "▶ 片段回放";
}
function v2DockPlay(ms, label, btn) {
  const ctx = _v2Ctx; if (!ctx) return;
  v2EnsureDock();
  const pl = document.getElementById("v2dockPlayer"); if (!pl) return;
  document.querySelectorAll(".clip-btn.playing").forEach(b => b.classList.remove("playing"));
  if (btn && btn.classList) btn.classList.add("playing");
  const t = document.getElementById("v2dockTitle");
  if (t) t.textContent = "▶ " + (label || wallT(ctx.startedAt, ms));
  if (ctx.media && ctx.media.available && !pl.dataset.bound) {
    const mSrc = String(ctx.media.url).startsWith("http") ? ctx.media.url : withBase(ctx.media.url);
    if (_v2HlsDispose) { _v2HlsDispose(); _v2HlsDispose = null; }
    _v2HlsDispose = attachMedia(pl, mSrc, Boolean(ctx.media.hls));
    pl.dataset.bound = "1";
  }
  try { pl.currentTime = avToVideoSec(ms); pl.play().catch(() => { }); } catch (e) { }
  // 让坞可见：窄屏滑出底部浮层；宽屏若坞在视口外则平滑滚到坞——否则用户以为"点了没反应"
  const dk = document.getElementById("v2dock");
  if (dk) {
    dk.classList.add("on");
    const r = dk.getBoundingClientRect();
    if (r.bottom < 70 || r.top > window.innerHeight - 90) dk.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
  // 迷你逐字稿：±40s
  const box = document.getElementById("v2dockScript");
  if (box) {
    const win = (ctx.tr || []).filter(x => Math.abs(x.start_ms - ms) <= 40_000);
    box.innerHTML = win.map(x =>
      `<div class="v2sline${Math.abs(x.start_ms - ms) < 4000 ? " hot" : ""}" data-dock="${x.start_ms}"><span class="st">${wallT(ctx.startedAt, x.start_ms)}</span><span>${esc(x.corrected_text || "")}</span></div>`
    ).join("") || '<div class="faint" style="padding:6px">该时段无逐字稿</div>';
    const hot = box.querySelector(".v2sline.hot"); if (hot) hot.scrollIntoView({ block: "center" });
  }
}

// ---------- 生成任务 ----------
// 一次生成要跑 8~10 次深度模型（2026-09-30 后端改成并行，一般 3–6 分钟；之前串行 10–20 分钟）。
// 这期间用户会切主播、关弹窗、刷新页面，
// 所以「哪一场哪位主播正在生成」不能只记在按钮上（2026-09-30：切个主播按钮就变回原样、
// 再点被悄悄忽略，看着像没反应）。按「场次:主播」记在这里并落 localStorage，每次渲染都照它画；
// 请求断了（合盖/断网/刷新）后台仍会跑完，改为定时查结果。
const V2_JOBS_STORAGE = "fupan_v2_jobs";
const V2_JOB_MAX_MS = 40 * 60_000;   // 请求断开后最多等到这么久，还没结果就放开重试
const V2_POLL_MS = 45_000;
const _v2Jobs = new Map();           // "场次:主播" -> {id, sid, status: running|failed|ready, startedAt, prev, attached, error}
let _v2JobTimer = null, _v2PollAt = 0, _v2Polling = false;
const v2JobKey = (id, sid) => `${Number(id)}:${sid ? Number(sid) : ""}`;
// 某主播现有 v2 的生成时间戳（没有则为空串）；任务开始时记一份，之后变了就是新结果到了
function v2StampOf(streamers, sid) {
  const s = (streamers || []).find(x => String(x.streamer_id) === String(sid));
  const v2 = s && s.summary ? (parseJSON(s.summary.data) || {}).v2 : null;
  return v2 ? String(v2.generated_at || "有") : "";
}
function v2JobsSave() {
  const keep = {};
  _v2Jobs.forEach((j, k) => { if (j.status === "running") keep[k] = { id: j.id, sid: j.sid, startedAt: j.startedAt, prev: j.prev }; });
  try { localStorage.setItem(V2_JOBS_STORAGE, JSON.stringify(keep)); } catch (e) { }
}
function v2JobsRestore() {   // 刷新页面后：没超时的任务接着显示「生成中」，靠查结果等它跑完
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(V2_JOBS_STORAGE) || "{}") || {}; } catch (e) { }
  for (const k in saved) {
    const j = saved[k];
    if (!j || !j.id || !(Date.now() - j.startedAt < V2_JOB_MAX_MS)) continue;
    _v2Jobs.set(v2JobKey(j.id, j.sid), { id: Number(j.id), sid: j.sid ? Number(j.sid) : null, status: "running", startedAt: Number(j.startedAt), prev: j.prev || "", attached: false });
  }
  v2JobsWatch();
}
function v2JobsWatch() {
  const busy = [..._v2Jobs.values()].some(j => j.status === "running");
  if (busy && !_v2JobTimer) _v2JobTimer = setInterval(v2JobsTick, 1000);
  if (!busy && _v2JobTimer) { clearInterval(_v2JobTimer); _v2JobTimer = null; }
}
function v2JobsTick() {
  document.querySelectorAll("[data-v2elapsed]").forEach(x => { x.textContent = v2Elapsed(+x.dataset.v2elapsed); });
  const now = Date.now();
  _v2Jobs.forEach((j, k) => {
    if (j.status === "running" && !j.attached && now - j.startedAt > V2_JOB_MAX_MS) v2JobFail(k, j, "等了 40 分钟还没有结果，可能中途断了，请重试");
  });
  if (now >= _v2PollAt && !document.hidden) { _v2PollAt = now + V2_POLL_MS; void v2JobsPoll(); }
}
// 只查「请求已断开、且正开着这一场」的任务；没开着就不查，下次打开详情时自然拿到最新数据
async function v2JobsPoll() {
  const id = Number(detailState.id);
  if (_v2Polling || !id || !modalOpen()) return;
  const waiting = [..._v2Jobs].filter(([, j]) => j.status === "running" && !j.attached && j.id === id);
  if (!waiting.length) return;
  _v2Polling = true;
  try {
    const latest = await api(`/api/sessions/${id}/streamers`);
    for (const [k, j] of waiting) {
      if (_v2Jobs.get(k) !== j) continue;
      const stamp = v2StampOf(latest, j.sid);
      if (stamp && stamp !== j.prev) { v2JobDone(k, j, latest); continue; }
      // 服务器会报哪位主播正在生成（v2_job，2026-09-30 起）：它说没在跑、结果又没变，就是这次没成
      // （出错或服务重启过），直接放开重试，不用干等 40 分钟。旧后端没有这个字段，照旧等。
      const s = (latest || []).find(x => String(x.streamer_id) === String(j.sid || 0));
      if (s && "v2_job" in s && !s.v2_job) v2JobFail(k, j, "后台这次没有生成出结果（可能中途出错或服务重启过），请重试");
    }
  } catch (e) { }
  finally { _v2Polling = false; }
}
// 服务器说某位主播正在生成、本机却没记着（别的设备点的，或本机以为失败了）：照样显示「生成中」，
// 之后靠定时查结果等它跑完；这时再点生成也不会多跑一份（后端同一场同一主播只跑一份）。
// 只能拿刚从服务器取回的 streamers 来认领（打开详情时）；拿旧快照会把已判定失败的任务又改回生成中
function v2JobsAdopt(id, streamers) {
  id = Number(id);
  let changed = false;
  (streamers || []).forEach(s => {
    const run = s && s.v2_job;
    if (!run || run.status !== "running") return;
    const sid = s.streamer_id ? Number(s.streamer_id) : null;
    const key = v2JobKey(id, sid), old = _v2Jobs.get(key);
    if (old && old.status === "running") return;
    _v2Jobs.set(key, { id, sid, status: "running", attached: false,
      startedAt: Date.now() - (Number(run.elapsed_sec) || 0) * 1000, prev: v2StampOf(streamers, sid) });
    changed = true;
  });
  if (changed) { v2JobsSave(); v2JobsWatch(); }
}
function v2MediaPlaying() {
  return ["detailPlayer", "v2dockPlayer"].some(pid => { const p = document.getElementById(pid); return !!(p && !p.paused && !p.ended); });
}
function v2JobFail(key, job, msg) {
  _v2Jobs.set(key, { ...job, status: "failed", attached: false, error: msg });
  v2JobsSave(); v2JobsWatch(); v2GenBoxesRefresh();
}
function v2JobDone(key, job, latest) {
  _v2Jobs.delete(key); v2JobsSave(); v2JobsWatch();
  if (Number(detailState.id) !== job.id || !modalOpen()) return;   // 没开着这一场：下次打开就是新的
  detailState.streamers = latest;
  const cur = detailState.currentSid || "";
  if (cur && Number(cur) !== job.sid) return;                      // 正在看别的主播：切回来时按新数据画
  if (v2MediaPlaying()) {                                          // 正在放录像：整页重画会打断播放，先给个入口
    _v2Jobs.set(key, { ...job, status: "ready", attached: false }); v2GenBoxesRefresh(); return;
  }
  void renderDetailFor(detailState.id, cur || null);
}
async function v2StartJob(id, sid) {
  id = Number(id); sid = sid ? Number(sid) : null;
  const key = v2JobKey(id, sid), old = _v2Jobs.get(key);
  if (old && old.status === "running") return;   // 已经在跑：按钮本来就显示「生成中」，不重复发请求
  const job = { id, sid, status: "running", startedAt: Date.now(), attached: true,
    prev: Number(detailState.id) === id ? v2StampOf(detailState.streamers, sid) : "" };
  _v2Jobs.set(key, job); v2JobsSave(); v2JobsWatch(); v2GenBoxesRefresh();
  const detach = () => { job.attached = false; _v2PollAt = 0; };   // 交给定时查结果
  try {
    const r = await api(`/api/sessions/${id}/reanalyze_v2${sid ? `?streamer_id=${sid}` : ""}`, { method: "POST" });
    if (_v2Jobs.get(key) !== job) return;
    if (!r || !r.ok) { v2JobFail(key, job, (r && r.error) || "生成失败"); return; }
    if (Number(detailState.id) !== id || !modalOpen()) { _v2Jobs.delete(key); v2JobsSave(); v2JobsWatch(); return; }
    let latest = null;
    try { latest = await api(`/api/sessions/${id}/streamers`); } catch (e) { }
    if (_v2Jobs.get(key) !== job) return;
    if (latest) v2JobDone(key, job, latest); else detach();
  } catch (e) {
    if (_v2Jobs.get(key) !== job) return;
    // 服务器明确回了错 → 失败；刚点就连不上 → 请求没发出去；跑了一阵才断 → 后台多半还在跑，改为查结果
    if ((e && e.status) || Date.now() - job.startedAt < 15_000) v2JobFail(key, job, fmtErr(e, "生成失败"));
    else detach();
  }
}
function v2Elapsed(ts) {
  const s = Math.max(0, Math.floor((Date.now() - ts) / 1000));
  return s < 60 ? `${s} 秒` : `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
}
function v2GenBoxInner(id, sid) {
  const job = _v2Jobs.get(v2JobKey(id, sid));
  const st = sid ? (detailState.streamers || []).find(x => String(x.streamer_id) === String(sid)) : null;
  const who = sid ? `「${esc(nameOf(sid, st && st.name))}」` : "本场";
  const tip = (t, color) => `<div class="faint" style="margin-top:8px;font-size:12px${color ? ";color:" + color : ""}">${t}</div>`;
  if (job && job.status === "running")
    return `正在生成${who}的 v2 深度复盘。<br><br>
      <button class="btn sm" disabled>⏳ 生成中 · 已等 <span data-v2elapsed="${job.startedAt}">${v2Elapsed(job.startedAt)}</span></button>
      ${tip("一般要 3–6 分钟，不用重复点。可以切页签、关掉弹窗去忙别的，后台会一直跑，好了这里自动显示。")}`;
  if (job && job.status === "ready")
    return `${who}的 v2 深度复盘生成好了。<br><br>
      <button class="btn sm" data-v2view>查看结果</button>
      ${tip("刚才在放录像，没有自动刷新，免得打断播放。")}`;
  const failed = !!(job && job.status === "failed");
  return `${failed ? `${who}的 v2 深度复盘没有生成成功。` : `还没有${who}的 v2 深度复盘。`}<br><br>
    <button class="btn sm" data-v2gen>${failed ? "✨ 重试生成" : "✨ 生成 v2 深度复盘"}</button>
    ${failed ? tip("原因：" + esc(job.error || "生成失败"), "var(--red)") : ""}
    ${tip("一般要 3–6 分钟（直播越长越久）。点一次就行，之后可以切页签、关掉弹窗去忙别的。")}`;
}
// 同一个生成框会出现在语气/结构/质检/弹幕/罗盘五个页签里：不用 id（重复 id 只有第一个绑得上事件），
// 统一带 data-v2genbox，点击走 #modalBody 上的事件委托
function v2GenBoxHTML(id, sid) {
  return `<div class="v2gen" data-v2genbox data-sess="${Number(id)}" data-sid="${sid ? Number(sid) : ""}">${v2GenBoxInner(id, sid)}</div>`;
}
function v2GenBoxesRefresh() {
  document.querySelectorAll("[data-v2genbox]").forEach(b => { b.innerHTML = v2GenBoxInner(b.dataset.sess, b.dataset.sid || null); });
}
if (!(_viewQS.get("view") && _viewQS.get("sig"))) v2JobsRestore();

// ---------- 主入口 ----------
function renderV2Panes(id, v2Sid, v2, tr, media) {
  _v2Ctx = { id, tr, media, startedAt: detailState.startedAt };
  const box = $("#modalBody"); if (!box) return;
  const panes = { v2tone: ["tone", v2PaneTone], v2struct: ["structure", v2PaneStruct], v2qc: ["qc", v2PaneQc], v2dm: ["danmaku", v2PaneDm] };
  // 这一场里已经拿到新结果的任务（失败过/待查看/断线等结果的）不用再记
  _v2Jobs.forEach((j, k) => {
    if (j.id === Number(id) && !(j.status === "running" && j.attached) && v2StampOf(detailState.streamers, j.sid) !== (j.prev || "")) _v2Jobs.delete(k);
  });
  v2JobsSave(); v2JobsWatch();
  const genBtn = v2GenBoxHTML(id, v2Sid);
  for (const paneName in panes) {
    const el = box.querySelector(`.dt-pane[data-pane="${paneName}"]`);
    if (!el) continue;
    const [key, fn] = panes[paneName];
    el.innerHTML = v2 && v2[key]
      ? `<div class="v2-grid"><div style="min-width:0">${fn(v2[key])}</div><div class="v2dockslot" data-slot="${paneName}"></div></div>`
      : genBtn;
  }
  // 罗盘分析（attr pane 顶部插入 v2.compass；旧归因内容保留在下方）
  const attrPane = box.querySelector('.dt-pane[data-pane="attr"]');
  if (attrPane) {
    let host = attrPane.querySelector("#v2compassBox");
    if (!host) {
      host = document.createElement("div"); host.id = "v2compassBox";
      attrPane.insertBefore(host, attrPane.firstChild);
    }
    host.innerHTML = v2 && v2.compass
      ? `<div class="v2-grid"><div style="min-width:0">${v2PaneCompass(v2.compass)}</div><div class="v2dockslot" data-slot="attr"></div></div>
         <details style="margin:8px 0 14px"><summary class="faint" style="cursor:pointer;font-size:12.5px">🗂 原始归因明细（旧版视图）</summary></details>`
      : (v2 ? "" : genBtn);
    if (v2 && v2.compass) void fillCompassKpis(id);
  }
  // 坞:单例 DOM,切 pane 时自愈挂载到当前 pane 的 slot
  v2EnsureDock();
  const oldApply = detailState.applyPane;
  if (!oldApply || !oldApply._v2wrapped) {
    const wrapped = () => { if (oldApply) oldApply(); v2EnsureDock(); };
    wrapped._v2wrapped = true;
    detailState.applyPane = wrapped;
  }
  // 事件委托:折叠卡/片段按钮/框架格/驾驶舱跳转
  if (!box.dataset.v2bound) {
    box.dataset.v2bound = "1";
    box.addEventListener("click", (ev) => {
      const gen = ev.target.closest("[data-v2gen]");
      if (gen) {
        const gb = gen.closest("[data-v2genbox]");
        if (gb) void v2StartJob(gb.dataset.sess, gb.dataset.sid || null);
        return;
      }
      if (ev.target.closest("[data-v2view]")) { void renderDetailFor(detailState.id, detailState.currentSid || null); return; }
      const dockBtn = ev.target.closest("[data-dock]");
      if (dockBtn) {
        ev.stopPropagation();
        v2DockPlay(+dockBtn.dataset.dock || 0, dockBtn.dataset.lab || "", dockBtn.classList.contains("clip-btn") ? dockBtn : null);
        return;
      }
      const jump = ev.target.closest("[data-jump]");
      if (jump) { detailState.viewPane = jump.dataset.jump; detailState.applyPane(); return; }
      const fc = ev.target.closest("[data-fc]");
      if (fc) { fc.classList.toggle("open"); return; }
      const zm = ev.target.closest("[data-zoom]");
      if (zm) {
        const strip = document.getElementById("v2strip");
        if (strip) strip.style.minWidth = (100 * (+zm.dataset.zoom || 1)) + "%";
        zm.closest(".v2zoombar").querySelectorAll("button").forEach(b => b.classList.toggle("on", b === zm));
        return;
      }
      const head = ev.target.closest(".v2q-head[data-tg]");
      if (head && !ev.target.closest("button")) {
        head.parentElement.classList.toggle("open");
        // 整场回放页:点问题卡头 = 展开详情 + 视频进度条跳到该段(2026-07-15 联动需求)
        const chMs = head.parentElement.dataset.chs;
        if (chMs && head.closest('.dt-pane[data-pane="replay"]')) {
          const pl = document.getElementById("detailPlayer");
          if (pl) { try { pl.currentTime = avToVideoSec(+chMs); pl.play().catch(() => { }); } catch (e) { } }
        }
      }
      if (ev.target.id === "v2dockX") v2DockStop();
    });
  }
}

// ===================== 规定话术 =====================
function scriptRuleBody() {
  const num = id => Number($(id).value);
  const scopeType = $("#scriptScopeType").value, scopeIds = $("#scriptScopeIds").value.split(/[,，]/).map(Number).filter(Boolean);
  const scopeKey = scopeType === "streamers" ? "streamer_ids" : "room_ids";
  const body = {
    name: $("#scriptName").value.trim(), standard_script: $("#scriptStandardText").value.trim(),
    repeat_interval_sec: Math.round(num("#scriptInterval") * 60), target_duration_sec: Math.round(num("#scriptDuration")),
    interval_tolerance_sec: Math.round(num("#scriptTimeTolerance")),
    duration_tolerance_seconds: num("#scriptDurationTolerance"),
    similarity_threshold: num("#scriptSimilarity") / 100, effective_scope: scopeType === "all" ? { type: "all" } : { type: scopeType, [scopeKey]: scopeIds }, enabled: $("#scriptEnabled").checked,
  };
  if (!body.name) throw new Error("规则名称不能为空");
  if (!body.standard_script) throw new Error("标准话术不能为空");
  if (!(body.repeat_interval_sec > 0) || !(body.target_duration_sec > 0)) throw new Error("频率、时长必须为正数");
  if (body.interval_tolerance_sec < 0 || body.duration_tolerance_seconds < 0) throw new Error("允许误差不能为负数");
  if (!(body.similarity_threshold >= 0 && body.similarity_threshold <= 1)) throw new Error("相似度只能填写0～100");
  if (scopeType !== "all" && !scopeIds.length) throw new Error("指定作用域时必须填写ID");
  body.duration_tolerance_sec = Math.round(body.duration_tolerance_seconds); delete body.duration_tolerance_seconds;
  return body;
}

function resetScriptForm() {
  $("#scriptRuleId").value = ""; $("#scriptName").value = ""; $("#scriptStandardText").value = "";
  $("#scriptInterval").value = ""; $("#scriptDuration").value = "";
  $("#scriptTimeTolerance").value = "0"; $("#scriptDurationTolerance").value = "0";
  $("#scriptSimilarity").value = "80"; $("#scriptEnabled").checked = true;
  $("#scriptScopeType").value = "all"; $("#scriptScopeIds").value = "";
  $("#scriptFormTitle").textContent = "新增规定话术"; $("#scriptFormHint").textContent = "";
}

async function loadScriptRules() {
  const response = await api("/api/script-frequency-rules").catch(e => {
    const box = $("#scriptFormHint"); if (box) box.textContent = "加载失败：" + fmtErr(e); return [];
  });
  prescribedRules = response.rules || [];
  const rows = $("#scriptRuleRows"); if (!rows) return;
  rows.innerHTML = prescribedRules.map(r => `<tr><td><b>${esc(r.name)}</b></td><td>${esc(r.standard_script)}</td>
    <td>每 ${Number(r.repeat_interval_sec / 60)} 分钟 / ${Number(r.target_duration_sec)} 秒</td>
    <td>时间 ±${Number(r.interval_tolerance_sec)} 秒<br>时长 ±${Number(r.duration_tolerance_sec)} 秒</td>
    <td>${Number(r.similarity_threshold * 100)}%<br><span class="faint">${esc((r.effective_scope || {}).type || "all")}</span></td><td><span class="badge ${r.enabled ? "done" : "created"}">${r.enabled ? "已启用" : "已停用"}</span><br>v${Number(r.version)}</td>
    <td><button class="btn ghost sm" data-script-edit="${r.id}">编辑</button> <button class="btn ghost sm" data-script-toggle="${r.id}">${r.enabled ? "停用" : "启用"}</button> <button class="btn ghost sm" data-script-versions="${r.id}">版本</button></td></tr>`).join("") || '<tr><td colspan="7"><div class="empty">暂无规定话术规则</div></td></tr>';
  rows.querySelectorAll("[data-script-edit]").forEach(b => b.onclick = () => {
    const r = prescribedRules.find(x => x.id === Number(b.dataset.scriptEdit)); if (!r) return;
    $("#scriptRuleId").value = r.id; $("#scriptName").value = r.name; $("#scriptStandardText").value = r.standard_script;
    $("#scriptInterval").value = r.repeat_interval_sec / 60; $("#scriptDuration").value = r.target_duration_sec;
    $("#scriptTimeTolerance").value = r.interval_tolerance_sec; $("#scriptDurationTolerance").value = r.duration_tolerance_sec;
    $("#scriptSimilarity").value = r.similarity_threshold * 100; $("#scriptEnabled").checked = r.enabled;
    const scope = r.effective_scope || { type: "all" }; $("#scriptScopeType").value = scope.type || "all";
    $("#scriptScopeIds").value = (scope.streamer_ids || scope.room_ids || []).join(",");
    $("#scriptFormTitle").textContent = `编辑规定话术 · v${r.version}`; $("#scriptName").focus();
  });
  rows.querySelectorAll("[data-script-toggle]").forEach(b => b.onclick = async () => {
    const r = prescribedRules.find(x => x.id === Number(b.dataset.scriptToggle)); b.disabled = true;
    try { await api(`/api/script-frequency-rules/${r.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: !r.enabled }) }); await loadScriptRules(); }
    catch (e) { alert("状态保存失败：" + fmtErr(e)); b.disabled = false; }
  });
  rows.querySelectorAll("[data-script-versions]").forEach(b => b.onclick = () => loadScriptVersions(Number(b.dataset.scriptVersions)));
}

async function loadScriptVersions(id) {
  const box = $("#scriptVersions"); box.style.display = "block"; box.innerHTML = "正在读取版本…";
  try {
    const response = await api(`/api/script-frequency-rules/${id}/versions`), versions = response.versions || [];
    box.innerHTML = `<div class="card-head"><h3>规则版本记录</h3></div>${versions.map(v => `<details><summary>v${v.version} · ${esc(v.created_at || "")}</summary><pre style="white-space:pre-wrap">${esc(JSON.stringify(v.snapshot, null, 2))}</pre></details>`).join("") || '<div class="empty">暂无版本</div>'}`;
  } catch (e) { box.innerHTML = `<div class="empty">版本加载失败：${esc(fmtErr(e))}</div>`; }
}

$("#btnSaveScript") && ($("#btnSaveScript").onclick = async () => {
  if (scriptSaveGate.has("rule")) return;
  const button = $("#btnSaveScript"); button.disabled = true; $("#scriptFormHint").textContent = "保存中…";
  try {
    const body = scriptRuleBody(), id = Number($("#scriptRuleId").value || 0);
    await scriptSaveGate.run("rule", () => api(id ? `/api/script-frequency-rules/${id}` : "/api/script-frequency-rules", {
      method: id ? "PATCH" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }));
    resetScriptForm(); await loadScriptRules(); $("#scriptFormHint").textContent = "保存成功";
  } catch (e) { $("#scriptFormHint").textContent = "保存失败：" + fmtErr(e); }
  finally { button.disabled = false; }
});
$("#btnCancelScript") && ($("#btnCancelScript").onclick = resetScriptForm);

let managedComplianceRules = [];
const scopeLabels = { all: "全部", streamers: "指定主播", sessions: "指定场次", rooms: "指定直播间" };
function complianceRuleBody(kind) {
  const prefix = kind === "forbidden" ? "forbidden" : "exemption";
  const type = $("#" + prefix + "Scope").value;
  const ids = $("#" + prefix + "ScopeIds").value.split(/[,，]/).map(Number).filter(Boolean);
  const key = { streamers: "streamer_ids", sessions: "session_ids", rooms: "room_ids" }[type];
  if (type !== "all" && !ids.length) throw new Error("指定作用域时必须填写ID");
  return {
    action: kind === "forbidden" ? "forbid" : "allow",
    keyword: $("#" + prefix + "Phrase").value.trim(),
    priority: Number($("#" + prefix + "Priority").value),
    enabled: $("#" + prefix + "Enabled").checked,
    scope_type: type === "all" ? "all" : ({ streamers: "streamer", sessions: "session", rooms: "room" }[type]),
    scope_id: type === "all" ? "" : ids.join(","),
  };
}
function resetComplianceForm(kind) {
  const p = kind === "forbidden" ? "forbidden" : "exemption";
  $("#" + p + "RuleId").value = ""; $("#" + p + "Phrase").value = ""; $("#" + p + "Scope").value = "all"; $("#" + p + "ScopeIds").value = ""; $("#" + p + "Priority").value = "100"; $("#" + p + "Enabled").checked = true;
}
async function loadComplianceRules() {
  try {
    const response = await api("/api/compliance-rules?page_size=200");
    managedComplianceRules = (response.items || []).map(r => {
      const plural = { streamer: "streamers", session: "sessions", room: "rooms" }[r.scope_type] || "all";
      const ids = String(r.scope_id || "").split(",").map(Number).filter(Boolean);
      const key = { streamers: "streamer_ids", sessions: "session_ids", rooms: "room_ids" }[plural];
      return { ...r, kind: r.action === "forbid" ? "forbidden" : "exemption", phrase: r.keyword,
        effective_scope: plural === "all" ? { type: "all" } : { type: plural, [key]: ids },
        reference_count: 0, conflicts: [], limited_release: r.action === "allow" && r.scope_type !== "all" };
    });
  }
  catch (e) { ["forbidden", "exemption"].forEach(k => { const h = $("#" + k + "RuleHint"); if (h) h.textContent = "加载失败：" + fmtErr(e); }); return; }
  for (const kind of ["forbidden", "exemption"]) {
    const body = $("#" + kind + "Rows"); if (!body) continue;
    const rows = managedComplianceRules.filter(x => x.kind === kind);
    body.innerHTML = rows.map(r => { const scope = r.effective_scope || {}; const conflicts = (r.conflicts || []).join("；"); return `<tr><td><b>${esc(r.phrase)}</b>${r.source === "legacy" ? '<br><span class="faint">历史违禁词已迁移</span>' : ""}${r.limited_release ? '<br><span class="badge created">仅在指定范围放行</span>' : ""}</td><td>${esc(scopeLabels[scope.type] || scope.type)} ${esc((scope.streamer_ids || scope.session_ids || scope.room_ids || []).join(","))}</td><td>${Number(r.priority)}</td><td>${kind === "forbidden" ? Number(r.reference_count) + " 个场次" : (conflicts ? `<span style="color:var(--red)">${esc(conflicts)}</span>` : "无冲突")}</td><td>${r.enabled ? "已启用" : "已停用"}</td><td><button class="btn ghost sm" data-compliance-edit="${r.id}">编辑</button> <button class="btn ghost sm" data-compliance-toggle="${r.id}">${r.enabled ? "停用" : "启用"}</button> <button class="btn danger sm" data-compliance-delete="${r.id}" ${r.reference_count ? "disabled" : ""}>删除</button></td></tr>`; }).join("") || '<tr><td colspan="6"><div class="empty">暂无规则</div></td></tr>';
  }
  document.querySelectorAll("[data-compliance-edit]").forEach(b => b.onclick = () => { const r = managedComplianceRules.find(x => x.id === Number(b.dataset.complianceEdit)); const p = r.kind; $("#" + p + "RuleId").value = r.id; $("#" + p + "Phrase").value = r.phrase; $("#" + p + "Priority").value = r.priority; $("#" + p + "Enabled").checked = r.enabled; const s = r.effective_scope || { type: "all" }; $("#" + p + "Scope").value = s.type; $("#" + p + "ScopeIds").value = (s.streamer_ids || s.session_ids || s.room_ids || []).join(","); });
  document.querySelectorAll("[data-compliance-toggle]").forEach(b => b.onclick = async () => { const r = managedComplianceRules.find(x => x.id === Number(b.dataset.complianceToggle)); if (!r || scriptSaveGate.has("compliance:" + r.id)) return; b.disabled = true; try { await scriptSaveGate.run("compliance:" + r.id, () => api(`/api/compliance-rules/${r.id}`, { method: "PATCH", body: JSON.stringify({ enabled: !r.enabled }) })); await loadComplianceRules(); } catch (e) { alert("状态保存失败：" + fmtErr(e)); b.disabled = false; } });
  document.querySelectorAll("[data-compliance-delete]").forEach(b => b.onclick = async () => { if (b.disabled || !confirm("确认删除这条未使用规则？")) return; b.disabled = true; try { await api(`/api/compliance-rules/${b.dataset.complianceDelete}`, { method: "DELETE" }); await loadComplianceRules(); } catch (e) { alert("删除失败：" + fmtErr(e)); b.disabled = false; } });
}
for (const kind of ["forbidden", "exemption"]) {
  const button = $("#btnSave" + (kind === "forbidden" ? "Forbidden" : "Exemption"));
  if (button) button.onclick = async () => { const key = "save-compliance:" + kind; if (scriptSaveGate.has(key)) return; button.disabled = true; const hint = $("#" + kind + "RuleHint"); try { const payload = complianceRuleBody(kind); const id = Number($("#" + kind + "RuleId").value || 0); await scriptSaveGate.run(key, () => api(id ? `/api/compliance-rules/${id}` : "/api/compliance-rules", { method: id ? "PATCH" : "POST", body: JSON.stringify(payload) })); resetComplianceForm(kind); await loadComplianceRules(); hint.textContent = "保存成功"; } catch (e) { hint.textContent = "保存失败：" + fmtErr(e); } finally { button.disabled = false; } };
}
document.querySelectorAll("[data-compliance-tab]").forEach(tab => tab.onclick = () => { document.querySelectorAll("[data-compliance-tab]").forEach(x => x.classList.toggle("on", x === tab)); document.querySelectorAll("[data-compliance-pane]").forEach(x => { x.hidden = x.dataset.compliancePane !== tab.dataset.complianceTab; }); loadComplianceRules(); });

// ===================== 观摩学习 =====================
let observationPoll = null;
function stopObservationPolling() { if (observationPoll) clearTimeout(observationPoll); observationPoll = null; }
async function loadObservations() {
  stopObservationPolling();
  try {
    const [rooms, sessions] = await Promise.all([api("/api/rooms"), api("/api/sessions?include_error=true")]);
    const select = $("#observeRoom"); if (select) select.innerHTML = '<option value="">选择常用直播间</option>' + rooms.map(r => `<option value="${r.id}">${esc(r.title || r.room_id || "直播间")}</option>`).join("");
    select.dataset.rooms = JSON.stringify(rooms.map(r => ({ id: r.id, share_link: r.share_link })));
    const baseRows = (sessions || []).filter(x => x.session_type === "observation");
    const rows = await Promise.all(baseRows.map(async job => {
      const analysis = await api(`/api/observation-sessions/${job.id}/analysis`).catch(() => ({}));
      return { ...job, extraction_status: analysis.status || "not_started", extraction_error: analysis.error_message || "",
        transcript_count: analysis.transcript_count || 0, danmaku_count: analysis.danmaku_count || 0,
        save_audio: true, emotion_enabled: Boolean(analysis.include_emotion) };
    })), host = $("#observationList");
    host.innerHTML = rows.map(job => `<article class="card"><div class="card-head"><h3>${esc(job.title)}</h3><span class="badge ${esc(job.status)}">${sessionStatusLabel(job)}</span></div><div class="row"><span class="badge created">不绑定主播</span><span class="badge created">只采集，不实时点评</span><span class="hint">音频：${job.save_audio ? "保存" : "不保存"} · 情绪：${job.emotion_enabled ? "已启用" : "未启用"}</span></div><div class="script-exec-summary" style="margin-top:10px"><div class="kpi"><i>逐字稿</i><b>${Number(job.transcript_count)}</b></div><div class="kpi"><i>弹幕</i><b>${Number(job.danmaku_count)}</b></div><div class="kpi"><i>提炼状态</i><b>${esc({not_started:"未开始",processing:"提炼中",success:"待审核",failed:"失败"}[job.extraction_status] || job.extraction_status)}</b></div></div><div class="row" style="margin-top:10px">${["live","processing"].includes(job.status) ? `<button class="btn danger sm" data-observe-stop="${job.id}">停止观摩</button>` : ""}<button class="btn ghost sm" data-observe-results="${job.id}">查看结果</button><button class="btn gold sm" data-observe-extract="${job.id}" ${job.extraction_status === "processing" ? "disabled" : ""}>${job.extraction_status === "failed" ? "重试提炼" : "一键提炼优秀话术"}</button>${job.extraction_error ? `<span style="color:var(--red)">${esc(job.extraction_error)}</span>` : ""}</div></article>`).join("") || '<div class="empty">暂无观摩场次</div>';
    host.querySelectorAll("[data-observe-stop]").forEach(b => b.onclick = async () => { if (b.disabled) return; b.disabled = true; try { await api(`/api/sessions/${b.dataset.observeStop}/stop`, { method: "POST" }); await loadObservations(); } catch (e) { alert("停止失败：" + fmtErr(e)); b.disabled = false; } });
    host.querySelectorAll("[data-observe-extract]").forEach(b => b.onclick = async () => { if (b.disabled) return; const retry = b.textContent.includes("重试"); b.disabled = true; b.textContent = "提炼中…"; try { await api(`/api/observation-sessions/${b.dataset.observeExtract}/${retry ? "retry" : "extract"}`, { method: "POST", body: JSON.stringify({ include_emotion: $("#observeEmotion").checked }) }); await loadObservationResults(Number(b.dataset.observeExtract)); await loadObservations(); } catch (e) { alert("提炼失败：" + fmtErr(e)); b.disabled = false; b.textContent = "重试提炼"; } });
    host.querySelectorAll("[data-observe-results]").forEach(b => b.onclick = () => loadObservationResults(Number(b.dataset.observeResults)));
    if (rows.some(x => ["created","parsing","live","processing"].includes(x.status) || x.extraction_status === "processing")) observationPoll = setTimeout(loadObservations, document.hidden ? 30000 : 9000);
  } catch (e) { $("#observationList").innerHTML = `<div class="empty">观摩列表加载失败：${esc(fmtErr(e))}</div>`; }
}
async function loadObservationResults(id) {
  const data = await api(`/api/observation-sessions/${id}/analysis`), host = $("#observationResults");
  const highlights = (data.heat_slices || []).map((x, i) => `<article class="card"><div class="card-head"><h3>#${i + 1} 高互动片段 · ${mmss(x.start_ms)}—${mmss(x.end_ms)}</h3><b>热度 ${Number(x.heat_score)}</b></div><div class="hint">弹幕 ${Number(x.danmaku_count)} · 独立用户 ${Number(x.unique_user_count)} · 来源定位 ${mmss(x.start_ms)}</div><p>${esc(x.corresponding_script || "该时间段暂无逐字稿")}</p></article>`).join("") || '<div class="empty">尚无可展示的高互动片段</div>';
  const templates = (data.templates || []).map(t => `<article class="card"><div class="card-head"><h3>${esc(t.title)}</h3><span class="badge ${t.status === "pending" ? "processing" : "done"}">${t.status === "pending" ? "待审核" : t.status === "adopted" ? "已采纳" : "已驳回"}</span></div><div>${esc(t.summary)}</div><div class="hint">话术结构：${esc((t.steps || []).map(x => x.action || x.content || x.title).filter(Boolean).join(" → "))}<br>示例话术：${esc(t.source_excerpt || "—")} · 建议时长 ${Number(t.total_duration_sec || 0)} 秒 · 情绪增强 ${data.include_emotion ? "已启用" : "未启用"}</div>${t.status === "pending" ? `<div class="row"><button class="btn sm" data-observe-adopt="${t.id}">采纳</button><button class="btn ghost sm" data-observe-reject="${t.id}">驳回</button></div>` : ""}</article>`).join("");
  host.innerHTML = `<div class="card-head"><h3>观摩提炼结果</h3><button class="btn ghost sm" id="goRhythmReview">跳转优秀节奏库</button></div>${highlights}${templates}`;
  $("#goRhythmReview").onclick = () => { switchView("kb"); setTimeout(() => $("#rhythmLibraryCard")?.scrollIntoView({ behavior: "smooth" }), 100); };
  host.querySelectorAll("[data-observe-adopt]").forEach(b => b.onclick = async () => { b.disabled = true; try { await api(`/api/rhythm-library/templates/${b.dataset.observeAdopt}/adopt`, { method: "POST" }); await loadObservationResults(id); } catch (e) { alert("采纳失败：" + fmtErr(e)); b.disabled = false; } });
  host.querySelectorAll("[data-observe-reject]").forEach(b => b.onclick = async () => { b.disabled = true; try { await api(`/api/rhythm-library/templates/${b.dataset.observeReject}/reject`, { method: "POST" }); await loadObservationResults(id); } catch (e) { alert("驳回失败：" + fmtErr(e)); b.disabled = false; } });
}
$("#btnObserveStart") && ($("#btnObserveStart").onclick = async () => { const b = $("#btnObserveStart"); if (scriptSaveGate.has("observe-start")) return; b.disabled = true; $("#observeHint").textContent = "正在创建观摩场次…"; try { await scriptSaveGate.run("observe-start", async () => { const roomId = Number($("#observeRoom").value) || null; const rooms = JSON.parse($("#observeRoom").dataset.rooms || "[]"); const shareLink = $("#observeLink").value.trim() || rooms.find(x => x.id === roomId)?.share_link || ""; if (!shareLink) throw new Error("请选择常用直播间或填写直播链接"); const session = await jpost("/api/sessions", { title: "", share_link: shareLink, streamer_ids: [], allow_unassigned: true, session_type: "observation", compliance_rule_ids: [] }); await jpost(`/api/sessions/${session.id}/start`, { streamer_ids: [], compliance_rule_ids: [] }); }); $("#observeHint").textContent = "观摩采集已启动"; await loadObservations(); } catch (e) { $("#observeHint").textContent = "启动失败：" + fmtErr(e); } finally { b.disabled = false; } });

async function loadSessionScriptPicker() {
  const box = $("#sessionScriptPicker"); if (!box) return;
  try {
    const [response, compliance] = await Promise.all([api("/api/script-frequency-rules?enabled=true"), api("/api/compliance-rules?enabled=true&page_size=200")]), rules = response.rules || [];
    box.innerHTML = rules.map(r => `<label class="script-choice"><input type="checkbox" data-rule-id="${r.id}"/><span><b>${esc(r.name)}</b><small>每 ${Number(r.repeat_interval_sec / 60)} 分钟讲 ${Number(r.target_duration_sec)} 秒 · 相似度 ≥ ${Number(r.similarity_threshold * 100)}%</small><small>${esc(r.standard_script)}</small></span></label>`).join("") || '<div class="empty">未配置已启用的规定话术</div>';
    const managed = (compliance.items || []).filter(r => r.enabled).map(r => ({ ...r, kind: r.action === "forbid" ? "forbidden" : "exemption", phrase: r.keyword, effective_scope: { type: ({streamer:"streamers",session:"sessions",room:"rooms"}[r.scope_type] || "all") } }));
    const renderManaged = (kind, target) => { const list = managed.filter(r => r.kind === kind); $(target).innerHTML = list.map(r => `<label class="script-choice"><input type="checkbox" data-compliance-rule-id="${r.id}"><span><b>${esc(r.phrase)}</b><small>${esc(scopeLabels[(r.effective_scope || {}).type] || "全部")} · 优先级 ${Number(r.priority)}</small></span></label>`).join("") || '<div class="faint">未配置</div>'; };
    renderManaged("forbidden", "#sessionForbiddenPicker"); renderManaged("exemption", "#sessionExemptionPicker");
    const updateCounts = () => { $("#sessionScriptCount").textContent = document.querySelectorAll('#sessionScriptPicker input:checked').length; $("#sessionForbiddenCount").textContent = document.querySelectorAll('#sessionForbiddenPicker input:checked').length; $("#sessionExemptionCount").textContent = document.querySelectorAll('#sessionExemptionPicker input:checked').length; const f = [...document.querySelectorAll('#sessionForbiddenPicker input:checked')].map(x => managed.find(r => r.id === Number(x.dataset.complianceRuleId))?.phrase); const e = [...document.querySelectorAll('#sessionExemptionPicker input:checked')].map(x => managed.find(r => r.id === Number(x.dataset.complianceRuleId))?.phrase); const conflict = f.filter(x => e.includes(x)); $("#sessionRuleConflict").textContent = conflict.length ? "⚠ 冲突：以下内容同时选择违禁与豁免：" + conflict.join("、") : ""; };
    [box, $("#sessionForbiddenPicker"), $("#sessionExemptionPicker")].forEach(x => x.addEventListener("change", updateCounts)); updateCounts();
  } catch (e) { box.innerHTML = `<div class="empty">规则加载失败：${esc(fmtErr(e))}</div>`; }
}

async function loadPrescribedAnalysis(sessionId) {
  const host = $("#prescribedAnalysis"); if (!host) return;
  try {
    const response = await api(`/api/sessions/${sessionId}/script-compliance`), rows = response.plans || [];
    if (!rows.length) { host.innerHTML = '<div class="r3-empty">本场未配置规定话术</div>'; return; }
    host.innerHTML = `<div class="row" style="margin-bottom:10px"><button class="btn gold" id="btnAnalyzePrescribed">${rows.some(x => x.result && x.result.status === "ready") ? "重新分析" : "开始分析"}</button><span class="hint" id="prescribedHint"></span></div>` + rows.map(item => {
      const rule = item.rule_snapshot || {}, result = item.result || {};
      const state = result.status === "analyzing" ? '<div class="r3-empty">分析中…</div>' : result.status === "failed" ? `<div class="r3-empty">分析失败：${esc(result.error_msg)}</div>` : result.status !== "ready" ? '<div class="r3-empty">尚未分析</div>' : `
        <div class="script-exec-summary">${[["应讲次数",result.expected_count],["实际次数",result.actual_count],["达标次数",result.matched_count],["漏讲次数",result.missing_count],["达标率",`${Math.round(Number(result.compliance_rate || 0) * 1000) / 10}%`],["平均间隔",result.average_interval_sec == null ? "—" : `${result.average_interval_sec}秒`],["最长/最短间隔",result.max_interval_sec == null ? "—" : `${result.max_interval_sec}/${result.min_interval_sec}秒`]].map(x => `<div class="kpi"><i>${x[0]}</i><b>${x[1]}</b></div>`).join("")}</div>
        ${(result.occurrences || []).map(e => { const view = Client.scriptOccurrencePresentation(e); return `<article class="script-exec ${esc(view.statusKey)}" data-script-seek="${Number(e.actual_start_ms || 0)}" data-script-sources="${(e.transcript_ids || []).map(Number).join(",")}"><b>第 ${Number(e.ordinal)} 次 · ${esc(view.statusLabel)}</b><div class="hint">实际时间 ${esc(view.actualTime)} · 时长 ${esc(view.durationText)} · 距上次 ${esc(view.intervalText)} · 相似度 ${esc(view.similarityText)}</div>${e.raw_transcript ? `<div>${esc(e.raw_transcript)}</div>` : ""}${view.hasIssues ? `<div style="color:var(--red)">${esc(view.reasonText)}</div>` : ""}</article>`; }).join("")}`;
      return `<section class="card"><div class="card-head"><h3>${esc(rule.name)} · 快照 v${Number(item.rule_version)}</h3><span class="hint">每 ${Number(rule.repeat_interval_sec / 60)} 分钟 / ${Number(rule.target_duration_sec)} 秒 / ≥${Number(rule.similarity_threshold * 100)}%</span></div><details><summary class="hint">查看已保存规则快照</summary><div>${esc(rule.standard_script)}</div></details>${state}</section>`;
    }).join("");
    const analyze = $("#btnAnalyzePrescribed"); if (analyze) analyze.onclick = async () => {
      if (scriptSaveGate.has(`analyze:${sessionId}`)) return;
      analyze.disabled = true; $("#prescribedHint").textContent = "分析中…";
      try { await scriptSaveGate.run(`analyze:${sessionId}`, () => jpost(`/api/sessions/${sessionId}/script-compliance/analyze`, {})); await loadPrescribedAnalysis(sessionId); }
      catch (e) { $("#prescribedHint").textContent = "分析失败：" + fmtErr(e); analyze.disabled = false; }
    };
    host.querySelectorAll("[data-script-sources]").forEach(card => card.onclick = () => {
      const ids = card.dataset.scriptSources.split(",").filter(Boolean);
      if (!ids.length) return;
      detailState.viewPane = "replay"; if (detailState.applyPane) detailState.applyPane();
      const sourceRows = ids.map(id => document.querySelector(`[data-transcript-id="${Number(id)}"]`)).filter(Boolean);
      sourceRows.forEach(row => { row.classList.add("assoc-hl"); setTimeout(() => row.classList.remove("assoc-hl"), 3000); });
      if (sourceRows[0]) scrollInBox(sourceRows[0], true);
      const player = $("#detailPlayer"); if (player) { player.currentTime = avToVideoSec(Number(card.dataset.scriptSeek)); player.play().catch(() => {}); }
    });
  } catch (e) { host.innerHTML = `<div class="r3-empty">规定话术结果加载失败：${esc(fmtErr(e))}</div>`; }
}
