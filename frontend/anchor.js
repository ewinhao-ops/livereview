const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
const BASE = location.pathname.replace(/\/anchor\.html.*$/, "").replace(/\/$/, "");
const withBase = url => BASE + url;
const Client = window.AnchorClient;
const loginGate = new Client.RequestGate();
const alertStore = new Client.AlertStore();
const notificationThrottle = new Client.NotificationThrottle(60000);
let unreadOnly = false, ws = null, retryTimer = null, retryAttempt = 0, playerCleanup = null, wsGeneration = 0;
const preferenceStore = (() => { try { return window["local" + "Storage"]; } catch (_) { return null; } })();
let notificationEnabled = (() => { try { return preferenceStore && preferenceStore.getItem("fupan_anchor_desktop_notifications") === "1"; } catch (_) { return false; } })();

function ensureNotificationControls() {
  if ($("#notifyToggle")) return;
  const logout = $("#logout"); if (!logout) return;
  logout.insertAdjacentHTML("beforebegin", '<label class="pill"><input id="notifyToggle" type="checkbox"> 桌面通知</label><span id="notifyPermission" class="muted"></span><button id="notifyAuthorize" type="button">授权通知</button>');
  $("#notifyToggle").checked = notificationEnabled;
  $("#notifyToggle").onchange = () => { notificationEnabled = $("#notifyToggle").checked; try { if (preferenceStore) preferenceStore.setItem("fupan_anchor_desktop_notifications", notificationEnabled ? "1" : "0"); } catch (_) {} };
  $("#notifyAuthorize").onclick = async () => { if (!("Notification" in window)) return; await Notification.requestPermission(); renderNotificationPermission(); };
  renderNotificationPermission();
}
function renderNotificationPermission() {
  const supported = "Notification" in window, permission = supported ? Notification.permission : "unsupported";
  if ($("#notifyPermission")) $("#notifyPermission").textContent = ({ granted: "已授权", denied: "已拒绝", default: "未授权", unsupported: "不支持" })[permission];
  if ($("#notifyAuthorize")) $("#notifyAuthorize").hidden = permission !== "default";
}
function desktopNotify(item) {
  if (!notificationEnabled || !("Notification" in window) || Notification.permission !== "granted") return;
  if (!notificationThrottle.allow(item)) return;
  try { new Notification(item.title || "直播提醒", { body: item.suggestion || item.message || "" }); } catch (_) {}
}

function cleanupPlayer() {
  if (playerCleanup) { playerCleanup(); playerCleanup = null; }
  const player = $("#replayPlayer");
  if (player) { try { player.pause(); player.removeAttribute("src"); player.load(); } catch (_) {} }
}
function showLogin(message = "") {
  cleanupPlayer(); cleanupRealtime();
  notificationThrottle.clear();
  alertStore.clear(); $("#app").classList.add("hidden"); $("#login").classList.remove("hidden");
  $("#err").textContent = message; $("#alerts").innerHTML = $("#replays").innerHTML = $("#detail").innerHTML = "";
  $("#unreadCount").textContent = "0";
}
async function api(url, opt = {}) {
  const headers = { ...(opt.headers || {}) };
  if (opt.body != null && !(opt.body instanceof FormData)) headers["Content-Type"] = "application/json";
  const response = await fetch(withBase(url), { credentials: "same-origin", ...opt, headers });
  const payload = await response.json().catch(() => ({}));
  if (response.status === 401) {
    showLogin("登录会话已过期，请重新登录");
    const error = new Error("登录会话已过期，请重新登录"); error.status = 401; throw error;
  }
  if (!response.ok) { const error = new Error(Client.publicError(payload, "请求失败")); error.status = response.status; throw error; }
  return payload;
}
function alertCard(item) {
  const meta = Client.alertMeta(item.kind, item.severity);
  const occurred = item.occurred_at || item.created_at;
  const occurredText = occurred
    ? new Date(occurred).toLocaleString()
    : (Number.isFinite(Number(item.at_ms)) ? `直播时间 ${formatTime(item.at_ms)}` : "");
  return `<article class="card alert ${meta.css} ${meta.severityCss}${item.read ? " read" : ""}" data-aid="${Number(item.id)}" tabindex="0"><div class="alert-head"><span class="pill kind ${meta.css}">${esc(meta.label)}</span><span class="pill severity ${meta.severityCss}">${esc(meta.severityLabel)}</span><span class="sp"></span>${item.read ? '<span class="read-mark">已读</span>' : '<span class="unread-dot" title="未读"></span>'}</div><b>${esc(item.title || meta.label)}</b>${occurredText ? `<div class="muted">发生时间：${esc(occurredText)}</div>` : ""}<div>${esc(item.message)}</div>${item.quote ? `<div class="muted quote">原话：${esc(item.quote)}</div>` : ""}${item.suggestion ? `<div class="suggestion">建议：${esc(item.suggestion)}</div>` : ""}</article>`;
}
function renderAlerts() {
  const rows = alertStore.list(unreadOnly);
  $("#alerts").innerHTML = rows.map(alertCard).join("") || `<div class="empty">${unreadOnly ? "暂无未读提醒" : "暂无提醒"}</div>`;
  $("#unreadCount").textContent = String(alertStore.unreadCount()); $("#unreadToggle").classList.toggle("on", unreadOnly);
}
async function refreshAlerts() { alertStore.merge(await api(`/api/anchor/alerts${unreadOnly ? "?unread_only=true" : ""}`)); renderAlerts(); }
function replayCard(item) {
  const time = [item.started_at, item.ended_at].filter(Boolean).map(x => String(x).replace("T", " ").slice(0, 16)).join(" — ") || "直播时间尚未记录";
  return `<article class="card replay-card"><b>${esc(item.title || `场次 #${item.id}`)}</b><div class="muted">${esc(time)}</div><div class="replay-summary">${esc(item.summary || "整场总结尚未生成")}</div><div class="replay-foot"><span>${item.score == null ? "待评分" : `${Number(item.score)} 分`}</span><button data-rid="${Number(item.id)}">查看完整复盘</button></div></article>`;
}
function renderData(data) {
  if (!data || !Object.keys(data).length) return "";
  const dimensions = Object.entries(data.dimensions || {}).map(([key, value]) => `<span class="dimension"><b>${esc(value)}</b>${esc(key)}</span>`).join("");
  const actions = data.action_items || data.actions || [], structure = data.structure || data.structure_review || "";
  const refs = data.experience_sources || data.experience_refs || data.references || [], match = data.emotion_match;
  const structureText = Array.isArray(structure) ? structure.join(" → ") : (typeof structure === "object" ? (structure.arc || []).join(" → ") : structure);
  return `${dimensions ? `<div class="dimensions">${dimensions}</div>` : ""}${structureText ? `<div class="analysis"><b>话术结构：</b>${esc(structureText)}</div>` : ""}${match ? `<div class="analysis"><b>${match.matched ? "情绪与语境匹配" : "情绪与语境不匹配"}：</b>实际 ${esc(match.actual_emotion || "-")}，应有 ${esc(match.expected_emotion || "-")}${match.reason ? `；${esc(match.reason)}` : ""}</div>` : ""}${actions.length ? `<div class="analysis"><b>优化改进建议：</b>${actions.map(item => esc(typeof item === "string" ? item : (item.action || item.text || item.title || ""))).join("；")}</div>` : ""}${refs.length ? `<div class="analysis muted"><b>经验引用来源：</b>${refs.map(item => esc(typeof item === "string" ? item : (item.source || item.title || "知识库"))).join("；")}</div>` : ""}`;
}
function formatTime(ms) { const seconds = Math.max(0, Math.floor(Number(ms || 0) / 1000)); return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`; }
function aiBadge(row) {
  if (row.is_fallback || row.llm_provider === "mock") return '<span class="fallback">降级结果，仅供参考</span>';
  return row.llm_provider ? '<span class="real-ai">真实AI点评</span>' : "";
}
function bindTimeline() {
  const player = $("#replayPlayer"); if (!player) return;
  const seek = event => { const row = event.target.closest("[data-start]"); if (!row) return; player.currentTime = Math.max(0, Number(row.dataset.start || 0) / 1000); player.play().catch(() => {}); };
  const containers = [$("#transcriptList"), $("#critiqueList")].filter(Boolean); containers.forEach(node => node.addEventListener("click", seek));
  const onTime = () => { const ms = player.currentTime * 1000; document.querySelectorAll("[data-start][data-end]").forEach(row => row.classList.toggle("active", ms >= Number(row.dataset.start) && ms < Number(row.dataset.end))); };
  player.addEventListener("timeupdate", onTime); playerCleanup = () => { containers.forEach(node => node.removeEventListener("click", seek)); player.removeEventListener("timeupdate", onTime); };
}
async function detail(id) {
  cleanupPlayer(); $("#detail").innerHTML = '<div class="card empty">复盘详情加载中…</div>';
  const data = await api(`/api/anchor/replays/${id}`), media = Client.mediaState(data.media);
  const mediaHtml = media.playable ? `<div class="media-card"><div class="section-title">音频回放 <span class="ok">${esc(media.label)}</span></div><${data.media.kind === "video" ? "video" : "audio"} id="replayPlayer" controls preload="metadata" src="${esc(withBase(data.media.url))}"></${data.media.kind === "video" ? "video" : "audio"}></div>` : `<div class="media-unavailable"><b>录音不可用</b><div>${esc(media.reason)}</div></div>`;
  const transcripts = data.transcripts.length ? data.transcripts.map(row => `<div class="timeline-row" data-start="${Number(row.start_ms)}" data-end="${Number(row.end_ms)}"><span class="time">${formatTime(row.start_ms)}</span>${esc(row.text)}</div>`).join("") : '<div class="empty">尚无逐字稿</div>';
  const segmentRows = data.critiques.filter(row => row.level === "segment");
  const segments = segmentRows.length ? segmentRows.map(row => `<div class="timeline-row critique-row" data-start="${Number(row.start_ms)}" data-end="${Number(row.end_ms || row.start_ms + 1)}"><div><span class="time">${formatTime(row.start_ms)}</span><b>${esc(row.category)}</b>${row.score == null ? "" : ` · ${Number(row.score)}分`}${aiBadge(row)}</div><div>${esc(row.comment)}</div>${renderData(row.data)}</div>`).join("") : '<div class="empty">尚无段级点评</div>';
  const summaryRows = data.critiques.filter(row => row.level === "summary");
  const summaries = summaryRows.length ? summaryRows.map(row => `<div class="summary-card"><div class="summary-title"><b>${esc(row.category || "整场总结")}</b>${aiBadge(row)}</div><p>${esc(row.comment)}</p>${renderData(row.data)}</div>`).join("") : '<div class="empty">整场总结尚未生成</div>';
  const compass = data.compass || {}, knowledge = data.knowledge || {};
  const sessionTime = [data.session.started_at, data.session.ended_at].filter(Boolean).map(x => String(x).replace("T", " ").slice(0, 16)).join(" — ") || "直播时间尚未记录";
  $("#detail").innerHTML = `<article class="detail-card"><button class="back" id="closeDetail">← 返回复盘列表</button><h2>${esc(data.session.title || `场次 #${id}`)}</h2><div class="muted">${esc(sessionTime)}</div>${mediaHtml}<section><div class="section-title">整场复盘分析</div>${summaries}</section><section><div class="section-title">段级点评</div><div id="critiqueList">${segments}</div></section><section><div class="section-title">完整逐字稿</div><div id="transcriptList">${transcripts}</div></section><section class="status-grid"><div class="status-card"><b>知识库</b><span>${knowledge.count ? `已录入 ${Number(knowledge.count)} 条` : "尚未录入"}</span></div><div class="status-card"><b>罗盘数据</b><span>${compass.count ? `已接入 ${Number(compass.count)} 条` : "尚未接入"}</span></div></section></article>`;
  $("#closeDetail").onclick = () => { cleanupPlayer(); $("#detail").innerHTML = ""; }; bindTimeline();
}
async function loadWorkspace() {
  const me = await api("/api/anchor/me"); $("#who").textContent = `当前账号：${me.name}`;
  $("#login").classList.add("hidden"); $("#app").classList.remove("hidden");
  const [alerts, replays] = await Promise.all([api("/api/anchor/alerts"), api("/api/anchor/replays")]);
  alertStore.merge(alerts); renderAlerts(); $("#replays").innerHTML = replays.map(replayCard).join("") || '<div class="empty">暂无复盘记录</div>';
  document.querySelectorAll("[data-rid]").forEach(button => button.onclick = () => detail(button.dataset.rid).catch(error => $("#detail").innerHTML = `<div class="card error-box">${esc(Client.publicError(error))}</div>`)); connect();
}
function setWsStatus(state, label) { const node = $("#ws"); node.className = `pill ws-${state}`; node.textContent = label; }
function connect() {
  if ($("#app").classList.contains("hidden")) return;
  if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;
  if (retryTimer) clearTimeout(retryTimer); retryTimer = null;
  setWsStatus(retryAttempt ? "retry" : "connecting", retryAttempt ? "重连中" : "连接中");
  const generation = ++wsGeneration;
  const socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${BASE}/api/anchor/ws`); ws = socket;
  socket.onopen = async () => { if (generation !== wsGeneration) return; const reconnected = retryAttempt > 0; retryAttempt = 0; setWsStatus("online", "在线"); if (reconnected) await refreshAlerts().catch(() => {}); };
  socket.onmessage = event => { if (generation !== wsGeneration) return; let item; try { item = JSON.parse(event.data); } catch (_) { return; } if (item.type !== "alert") return; alertStore.merge([{ ...item, read: false }]); renderAlerts(); desktopNotify(item); };
  socket.onerror = () => { if (generation === wsGeneration) setWsStatus("offline", "离线"); };
  socket.onclose = event => { if (generation !== wsGeneration) return; ws = null; if ($("#app").classList.contains("hidden")) return; if (event.code === 1008) { showLogin("登录会话已过期，请重新登录"); return; } if (retryTimer) return; const delay = Client.retryDelay(retryAttempt++); setWsStatus("retry", `重连中 · ${Math.ceil(delay / 1000)}秒`); retryTimer = setTimeout(() => { retryTimer = null; connect(); }, delay); };
}
function cleanupRealtime() {
  wsGeneration += 1; retryAttempt = 0;
  if (retryTimer) clearTimeout(retryTimer); retryTimer = null;
  const socket = ws; ws = null;
  if (socket) { socket.onopen = socket.onmessage = socket.onerror = socket.onclose = null; try { socket.close(); } catch (_) {} }
}
$("#alerts").onclick = async event => { const card = event.target.closest("[data-aid]"); if (!card) return; try { await api(`/api/anchor/alerts/${card.dataset.aid}/read`, { method: "POST" }); alertStore.markRead(card.dataset.aid); renderAlerts(); } catch (error) { if (error.status !== 401) $("#alertHint").textContent = Client.publicError(error); } };
$("#unreadToggle").onclick = async () => { unreadOnly = !unreadOnly; renderAlerts(); await refreshAlerts().catch(() => {}); };
$("#loginBtn").onclick = async () => {
  if (loginGate.has("login")) return; const button = $("#loginBtn"); button.disabled = true; button.textContent = "登录中…"; $("#err").textContent = "";
  try { await loginGate.run("login", () => api("/api/anchor/login", { method: "POST", body: JSON.stringify({ username: $("#user").value, password: $("#pwd").value }) })); await loadWorkspace(); }
  catch (error) { $("#err").textContent = error.status === 403 ? Client.publicError(error, "账号已禁用，请联系管理员") : Client.publicError(error, "登录失败，请检查账号和密码"); }
  finally { button.disabled = false; button.textContent = "登录"; }
};
$("#logout").onclick = async () => { try { await api("/api/anchor/logout", { method: "POST" }); } finally { showLogin("已安全退出"); } };
window.addEventListener("pagehide", () => { cleanupPlayer(); cleanupRealtime(); });
ensureNotificationControls();
loadWorkspace().catch(error => { if (error.status !== 401) showLogin(""); });
