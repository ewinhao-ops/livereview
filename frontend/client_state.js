(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.LiveReviewClient = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const TERMINAL = new Set(["done", "error"]);
  const SCRIPT_STATUS = Object.freeze({
    matched: "达标",
    too_early: "过早",
    late: "过晚",
    too_short: "时长不足",
    too_long: "时长过长",
    low_similarity: "相似度不足",
    missing: "漏讲",
    not_analyzable: "无法分析",
  });
  const TRACKING_PARAMS = new Set([
    "enter_from_merge", "enter_method", "action_type", "from", "share_token",
    "utm_source", "utm_medium", "utm_campaign", "timestamp",
  ]);

  function normalizeLiveLink(value) {
    const raw = String(value || "").trim();
    if (!raw) return "";
    const found = raw.match(/https?:\/\/[^\s，。、；;）)\]】}>"']+/i);
    const candidate = found ? found[0].replace(/\/+$/, "") : raw;
    try {
      const url = new URL(candidate);
      url.hash = "";
      url.hostname = url.hostname.toLowerCase();
      if (url.hostname === "www.live.douyin.com") url.hostname = "live.douyin.com";
      if (url.hostname === "live.douyin.com") {
        url.search = "";
      } else {
        for (const key of [...url.searchParams.keys()]) {
          const lowered = key.toLowerCase();
          if (TRACKING_PARAMS.has(lowered) || lowered.startsWith("utm_")) {
            url.searchParams.delete(key);
          }
        }
        url.searchParams.sort();
      }
      url.pathname = url.pathname.replace(/\/+$/, "") || "/";
      return url.toString().replace(/\/$/, url.pathname === "/" ? "/" : "");
    } catch (_) {
      return raw.replace(/\s+/g, " ").replace(/#.*$/, "").trim();
    }
  }

  function findDuplicateSession(sessions, link) {
    const target = normalizeLiveLink(link);
    if (!target) return null;
    return (Array.isArray(sessions) ? sessions : []).find(session => {
      if (!session || TERMINAL.has(session.status) || session.ended_at) return false;
      return normalizeLiveLink(session.share_link) === target;
    }) || null;
  }

  class RequestGate {
    constructor() { this.pending = new Map(); }
    run(key, task) {
      if (this.pending.has(key)) return this.pending.get(key);
      const promise = Promise.resolve().then(task).finally(() => this.pending.delete(key));
      this.pending.set(key, promise);
      return promise;
    }
    has(key) { return this.pending.has(key); }
  }

  function compassStatus(payload, pending, failed) {
    if (pending) return { key: "importing", label: "导入中", count: 0 };
    if (failed) return { key: "failed", label: "导入失败", count: 0 };
    const count = Number(payload && payload.today_points) || 0;
    if (count > 0 || (payload && payload.last_point_at)) {
      return { key: "success", label: "导入成功", count };
    }
    return { key: "empty", label: "尚未导入", count: 0 };
  }

  function audioPresentation(session) {
    const audio = session && session.audio;
    const serverReason = String(session && session.error_msg || "").trim();
    if (session && session.status === "error" && serverReason) {
      return { playable: false, label: "录音不可用", reason: serverReason };
    }
    if (!audio || audio.status !== "available" || Number(audio.size_bytes) <= 0) {
      return { playable: false, label: "录音不可用", reason: "" };
    }
    return { playable: true, label: "播放录音", reason: "" };
  }

  function fallbackMeta(value) {
    if (value === true) return { key: "fallback", label: "降级结果，仅供参考" };
    if (value === false) return { key: "real", label: "模型点评" };
    return { key: "unknown", label: "" };
  }

  function readinessState(payload) {
    const blockers = Array.isArray(payload && payload.blockers) ? payload.blockers.map(String) : [];
    return { ready: Boolean(payload && payload.ready), blockers,
      message: blockers.length ? blockers.join("；") : "系统已就绪" };
  }

  function safeError(error, fallback) {
    const status = Number(error && error.status) || 0;
    if (status === 409) return "同一直播链接已有未结束场次，请进入原场次";
    if (!status && error && error.name === "TypeError") return "网络连接失败，请检查网络后重试";
    let detail = error && error.detail;
    if (detail && typeof detail === "object") detail = detail.message || detail.code || "";
    let text = detail || error && error.message || (typeof error === "string" ? error : "");
    if (!text) return fallback || "操作失败，请稍后重试";
    text = String(text).split(/\r?\n/)[0]
      .replace(/sk-[A-Za-z0-9._-]+/g, "[已隐藏]")
      .replace(/(token|signature|sign|api[_-]?key|authorization)\s*[=:]\s*[^\s,;]+/ig, "$1=[已隐藏]")
      .replace(/https?:\/\/[^\s]+/g, "[内部地址已隐藏]");
    text = text.replace(/[A-Za-z]:[\\/][^\s]+/g, "[内部路径已隐藏]");
    if (/traceback|stack trace|file \".*\", line \d+/i.test(text)) return fallback || "系统处理失败，请稍后重试";
    return text.slice(0, 180);
  }

  function scriptOccurrencePresentation(occurrence) {
    const item = occurrence && typeof occurrence === "object" ? occurrence : {};
    const issues = Array.isArray(item.issues) ? item.issues.map(String) : [];
    const statusKey = item.status === "matched" ? "matched" : (issues[0] || item.status || "not_analyzable");
    const seconds = item.actual_start_ms == null ? null : Math.floor(Number(item.actual_start_ms) / 1000);
    const actualTime = seconds == null || !Number.isFinite(seconds) ? "—"
      : `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
    const withUnit = (value, unit) => value == null || !Number.isFinite(Number(value))
      ? "—" : `${Number(value)}${unit}`;
    return {
      statusKey,
      statusLabel: SCRIPT_STATUS[statusKey] || "无法分析",
      actualTime,
      durationText: withUnit(item.actual_duration_sec, "秒"),
      intervalText: withUnit(item.interval_from_previous_sec, "秒"),
      similarityText: withUnit(item.similarity_percent, "%"),
      reasonText: issues.length
        ? issues.map(issue => SCRIPT_STATUS[issue] || issue).join("、")
        : SCRIPT_STATUS[statusKey] || "无法分析",
      hasIssues: issues.length > 0,
    };
  }

  return {
    TERMINAL,
    SCRIPT_STATUS,
    normalizeLiveLink,
    findDuplicateSession,
    RequestGate,
    compassStatus,
    audioPresentation,
    fallbackMeta,
    readinessState,
    safeError,
    scriptOccurrencePresentation,
    isTerminal: status => TERMINAL.has(status),
  };
});
