(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AnchorClient = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  class RequestGate {
    constructor() { this.pending = new Map(); }
    run(key, task) {
      if (this.pending.has(key)) return this.pending.get(key);
      const promise = Promise.resolve().then(task).finally(() => this.pending.delete(key));
      this.pending.set(key, promise); return promise;
    }
    has(key) { return this.pending.has(key); }
  }

  class AlertStore {
    constructor() { this.items = new Map(); }
    merge(rows) {
      for (const row of Array.isArray(rows) ? rows : []) {
        if (!row || row.id == null) continue;
        const prior = this.items.get(String(row.id)) || {};
        this.items.set(String(row.id), { ...prior, ...row });
      }
      return this.list();
    }
    markRead(id) {
      const row = this.items.get(String(id));
      if (row) this.items.set(String(id), { ...row, read: true });
    }
    list(unreadOnly) {
      return [...this.items.values()]
        .filter(row => !unreadOnly || !row.read)
        .sort((a, b) => Number(b.id) - Number(a.id));
    }
    unreadCount() { return this.list(true).length; }
    clear() { this.items.clear(); }
  }

  function retryDelay(attempt, base = 1000, cap = 30000) {
    return Math.min(cap, base * (2 ** Math.max(0, Number(attempt) || 0)));
  }

  class NotificationThrottle {
    constructor(windowMs = 60000) { this.windowMs = windowMs; this.sent = new Map(); this.sessionId = null; }
    allow(item, now = Date.now()) {
      const sessionId = String(item && item.session_id || "");
      if (this.sessionId !== null && this.sessionId !== sessionId) this.clear();
      this.sessionId = sessionId;
      const kind = String(item && item.kind || "general");
      const severe = item && item.severity === "error";
      const fingerprint = severe ? `:${String(item.id || item.event_key || item.message || item.quote || "")}` : "";
      const key = `${sessionId}:${kind}${fingerprint}`;
      const last = this.sent.get(key);
      if (last != null && now - last < this.windowMs) return false;
      this.sent.set(key, now); return true;
    }
    clear() { this.sent.clear(); this.sessionId = null; }
  }

  function alertMeta(kind, severity) {
    const kinds = {
      compliance: ["合规用语", "compliance"],
      product_error: ["产品纠错", "product"],
      emotion_mismatch: ["情绪语境", "emotion"],
      danmaku_anomaly: ["弹幕异常", "danmaku"],
      danmaku_answer: ["观众问答", "question"],
    };
    const [label, css] = kinds[kind] || ["直播提醒", "general"];
    const levels = { error: ["严重", "error"], warn: ["警告", "warn"], info: ["提示", "info"] };
    const [severityLabel, severityCss] = levels[severity] || levels.warn;
    return { label, css, severityLabel, severityCss };
  }

  function mediaState(media) {
    if (media && media.available && media.url) return { playable: true, label: "音频可回放", reason: "" };
    return { playable: false, label: "录音不可用", reason: String(media && media.reason || "尚未生成有效录音") };
  }

  function publicError(value, fallback = "操作失败，请稍后重试") {
    let text = value && (value.detail || value.message) || (typeof value === "string" ? value : "");
    text = String(text || fallback).split(/\r?\n/)[0]
      .replace(/sk-[A-Za-z0-9._-]+/g, "[已隐藏]")
      .replace(/(token|signature|sign|api[_-]?key|authorization)\s*[=:]\s*[^\s,;]+/ig, "$1=[已隐藏]")
      .replace(/[A-Za-z]:\\[^\s]+|\/(?:home|var|opt|srv)\/[^\s]+/g, "[内部路径已隐藏]");
    text = text.replace(/[A-Za-z]:[\\/][^\s]+/g, "[内部路径已隐藏]");
    return /traceback|stack trace|file \".*\", line \d+/i.test(text) ? fallback : text.slice(0, 180);
  }

  return { RequestGate, AlertStore, NotificationThrottle, retryDelay, alertMeta, mediaState, publicError };
});
