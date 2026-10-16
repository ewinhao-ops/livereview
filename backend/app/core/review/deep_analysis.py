"""v2 深度复盘（2026-07-15 页面改版配套）：五块分析，每条结论强制携带证据链。

产出结构（合并进该主播 summary critique 的 data["v2"]）：
  tone      语气语调：情绪表达(内容↔语调匹配) + 代入感 + 建议情绪/话术
  structure 话术结构：实际结构时间轴 + 结构病 + 标准框架对照(缺失环节+示范话术)
  qc        质检：产品准确性 / 违规极限词（聚合段评与句级 flag，纯算法零 LLM）
  danmaku   弹幕回复：成交意向回复 / 观众提问回复（过滤官方与"已拍"类）
  compass   罗盘分析：成交 / 增粉 / 互动 三维度前后因果（30 分钟滞后窗）

设计约束：
  - evidence = [{quote, start_ms, end_ms, wall}]，wall 为北京时间 HH:MM（用户只看北京时间）；
  - 给 LLM 的每段素材都带 [S{i}|HH:MM] 锚号，要求输出引用锚号，由本模块映射回 evidence——
    模型给不出锚号的结论直接丢弃（宁缺勿滥，杜绝无证据结论）；
  - 全部步骤失败可独立降级（单块失败不影响其他块）；
  - 各块并行跑（2026-09-30：串行一次要 14~17 分钟）：互不依赖的块同时发，章节的各批也同时发，
    产品判词依赖章节、排在章节之后；同时在跑的深度模型请求数有上限（V2_LLM_CONCURRENCY）。
"""

from __future__ import annotations

import asyncio
import json
import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select

from ...config import get_settings
from ...db import SessionLocal
from ...models import CompassPoint, Critique, CritiqueLevel, OcrRecord, ReplaySession, Transcript
from ..llm import LLMCallError, get_llm
from ..safe_error import safe_error_text, safe_exception_code

# 官方账号弹幕过滤（弹幕文本形如 "用户名：内容"，取冒号前昵称匹配）
_OFFICIAL_PAT = re.compile(r"(hifiman|海菲曼)?(官方|旗舰店|自营|客服)", re.I)
# 纯告知类弹幕（"已拍""拍了"等，无复盘价值）
_NOISE_PAT = re.compile(r"^(已拍|拍了|拍好了|付了|已付款?|下单了|买了)[!！。~\s]*$")
_INTENT_KW = ("多少钱", "价格", "链接", "怎么拍", "想要", "要一个", "能便宜", "优惠", "改价",
              "拍哪个", "几号", "预算", "入手", "值不值", "发货", "下单", "购", "券", "定金", "现货")
_QUESTION_KW = ("吗", "?", "？", "怎么", "什么", "哪个", "哪款", "推荐", "区别", "对比", "适合", "能不能",
                "支持", "呢", "么", "有没有", "如何", "怎样", "为什么", "啥", "多重", "多大", "戴",
                "舒服", "音质", "声音", "感觉", "糊", "解析", "低音", "高音", "人声", "游戏", "直推",
                "前端", "耳放", "续航", "蓝牙", "降噪", "保修", "退", "换", "发", "到", "货")

_SEV_RED = ("违禁", "违规", "极限词", "绝对化", "破价", "低于底价", "低于官方", "导流", "回关")
_KW_ACCURACY = ("参数", "口误", "混淆", "说错", "讲错")
# 型号/价格"与知识库不符"多为知识库未收录或短期促销价——降级为「待核实」,不算红线
# (2026-07-15 用户规则:价格不做实时质检,只报破价;质检重心=介绍有误+违规绝对词)
_KW_PENDING = ("知识库", "型号", "不存在", "未提及", "未收录", "不在知识库")
_KW_PRICE = ("价格", "报价", "售价", "元")
# 2026-10-04：「元」会把「动圈单元」也当成价格；价格只认这些说法，且提到知识库的价格差异也不进「待核实」
_PRICE_RE = re.compile(r"价格|报价|售价|价位|多少钱|\d+\s*元")
# 纯节奏/结构类建议不属于质检（占比、闭环完成度之类归话术结构页），除非同时命中红线词
_KW_RHYTHM_ONLY = ("占比", "完成度", "闭环", "冷场", "语速", "节奏", "互动率", "标准动作")


def _wall(started_at: datetime | None, ms: int) -> str:
    if not started_at:
        return f"{ms // 60000}分"
    return (started_at + timedelta(milliseconds=int(ms or 0))).strftime("%H:%M")


def _ev(started_at, quote: str, s_ms: int, e_ms: int) -> dict:
    return {"quote": (quote or "")[:80], "start_ms": int(s_ms or 0), "end_ms": int(e_ms or 0),
            "wall": _wall(started_at, s_ms)}


def _parse(out: str) -> dict:
    try:
        return json.loads(out)
    except Exception:  # noqa: BLE001
        m = re.search(r"\{.*\}", out or "", re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:  # noqa: BLE001
                return {}
    return {}


# ---------- 深度模型调用：限并发 + 撞上限流先等一等 ----------
_RATE_LIMIT_WAITS = (5.0, 15.0)   # 撞上限流(429)后等几秒再发；等过两次还不行，这一块按失败处理
_slots: asyncio.Semaphore | None = None
_slots_loop: asyncio.AbstractEventLoop | None = None
# 本次生成里模型调用的成败计数（_analyze 开始时放进来，各块的子任务看到的是同一份）
_calls: ContextVar[dict | None] = ContextVar("v2_llm_calls", default=None)


def _llm_slots() -> asyncio.Semaphore:
    """同时在跑的深度模型请求数上限。全进程共用一份：两位主播同时生成也不会翻倍。"""
    global _slots, _slots_loop
    loop = asyncio.get_running_loop()
    if _slots is None or _slots_loop is not loop:
        try:
            limit = int(getattr(get_settings(), "v2_llm_concurrency", 4) or 4)
        except (TypeError, ValueError):
            limit = 4
        _slots = asyncio.Semaphore(max(1, min(limit, 8)))
        _slots_loop = loop
    return _slots


async def _deep_json(system: str, user: str) -> dict:
    """深度档调一次模型并解析成 dict。占一个并发名额；撞上限流时占着名额等一等再发
    （被限流的请求没被处理、不计费，重发不会多花钱）。"""
    stats = _calls.get()
    async with _llm_slots():
        waits = iter(_RATE_LIMIT_WAITS)
        while True:
            try:
                out = await get_llm("deep").complete(system, user, json_mode=True)
                break
            except Exception as exc:  # noqa: BLE001
                limited = isinstance(exc, LLMCallError) and exc.code == "LLM_RATE_LIMITED"
                wait = next(waits, None) if limited else None
                if wait is None:
                    if stats is not None:
                        stats["failed"] += 1
                    raise
                await asyncio.sleep(wait)
    if stats is not None:
        stats["ok"] += 1
    return _parse(out)


async def _settled(coros) -> list:
    """并行跑一组调用，逐个给出结果或异常（一个失败不拖累其他）；被取消时全部一起停。"""
    async def one(coro):
        try:
            return await coro
        except Exception as exc:  # noqa: BLE001
            return exc

    async with asyncio.TaskGroup() as tg:
        tasks = [tg.create_task(one(c)) for c in coros]
    return [t.result() for t in tasks]


def _load(session_id: int, streamer_id):
    """一次性取素材：session、该主播段评卡、句级flag、转写、弹幕、罗盘。"""
    db = SessionLocal()
    try:
        sess = db.get(ReplaySession, session_id)
        segs = db.execute(select(Critique).where(
            Critique.session_id == session_id, Critique.level == CritiqueLevel.segment,
            Critique.streamer_id == streamer_id).order_by(Critique.start_ms)).scalars().all()
        flags = db.execute(select(Critique).where(
            Critique.session_id == session_id, Critique.level == CritiqueLevel.sentence,
            Critique.streamer_id == streamer_id).order_by(Critique.start_ms)).scalars().all()
        trs = db.execute(select(Transcript).where(
            Transcript.session_id == session_id, Transcript.streamer_id == streamer_id)
            .order_by(Transcript.start_ms)).scalars().all()
        ocr = db.execute(select(OcrRecord).where(OcrRecord.session_id == session_id)
                         .order_by(OcrRecord.start_ms)).scalars().all()
        comp = db.execute(select(CompassPoint).where(CompassPoint.session_id == session_id)
                          .order_by(CompassPoint.start_ms)).scalars().all()
        seg_rows = [{"start_ms": c.start_ms or 0, "end_ms": c.end_ms or 0, "quote": c.quote or "",
                     "comment": c.comment or "", "score": c.score,
                     "data": json.loads(c.data) if c.data else {}} for c in segs]
        flag_rows = [{"start_ms": c.start_ms or 0, "end_ms": c.end_ms or 0, "quote": c.quote or "",
                      "comment": c.comment or "", "severity": getattr(c.severity, "value", str(c.severity))}
                     for c in flags]
        tr_rows = [{"start_ms": t.start_ms, "end_ms": t.end_ms,
                    "text": (t.corrected_text or t.raw_text or "").strip()} for t in trs]
        dm_rows = [{"start_ms": o.start_ms, "text": (o.text or "").strip()} for o in ocr]
        comp_rows = []
        for p in comp:
            try:
                comp_rows.append({"ms": p.start_ms, **json.loads(p.data)})
            except Exception:  # noqa: BLE001
                pass
        return sess, seg_rows, flag_rows, tr_rows, dm_rows, comp_rows
    finally:
        db.close()


def _talk_window(trs: list[dict], ms: int, before: int, after: int, cap: int = 500) -> str:
    xs = [t["text"] for t in trs if ms - before <= t["start_ms"] <= ms + after and t["text"]]
    return " / ".join(xs)[:cap]


# ---------- ① 语气语调（LLM deep ×1） ----------
async def _tone(sess, segs: list[dict]) -> dict:
    st = sess.started_at
    # 选素材段：有情绪标签的段 + 低分段，按时间均匀取样 ≤26 段
    cand = [s for s in segs if s["data"].get("emotion") or (s["score"] is not None and s["score"] <= 6)]
    step = max(1, len(cand) // 26)
    picked = cand[::step][:26]
    if not picked:
        return {"verdict": "本场无足够情绪样本", "chips": [], "issues": []}
    lines = []
    for i, s in enumerate(picked):
        d = s["data"]
        lines.append(f"[S{i}|{_wall(st, s['start_ms'])}] 情绪={d.get('emotion','?')} 场景={d.get('scene_label','')} "
                     f"分={s['score']} 话术节选:{s['quote'][:90]}")
    sysp = (
        "你是直播情绪教练，只做两件事：①情绪表达——内容与语调是否匹配（讲优点该有欣赏感、讲福利该有兴奋度、"
        "被质疑该稳）；②代入感——讲场景（音乐厅/通勤/游戏）时话术有没有把人带进场景。"
        "只输出 JSON：{\"verdict\":\"一句话总评≤50字\",\"chips\":[\"标签≤8字\"×2-4],"
        "\"issues\":[{\"title\":\"≤26字\",\"sev\":\"red|orange\",\"anchors\":[\"S序号\"],"
        "\"detail\":\"当时在讲什么+为什么不匹配≤60字\",\"suggest_emotion\":\"建议使用的情绪≤20字\","
        "\"suggest_line\":\"建议的代入感话术,可照读≤60字\"}]}。"
        "规矩：每条 issue 必须给 anchors（引用上面的 S 序号），给不出锚号就不要输出该条；"
        "最多 5 条，优先内容-语调错位与代入感缺失；不得编造参数价格。"
    )
    data = await _deep_json(sysp, "【本场情绪素材（按时间）】\n" + "\n".join(lines))
    issues = []
    for it in (data.get("issues") or [])[:5]:
        evs = []
        for a in it.get("anchors") or []:
            m = re.search(r"\d+", str(a))
            if m and int(m.group(0)) < len(picked):
                s = picked[int(m.group(0))]
                evs.append(_ev(st, s["quote"], s["start_ms"], s["end_ms"]))
        if evs:
            issues.append({"title": it.get("title", ""), "sev": it.get("sev", "orange"),
                           "detail": it.get("detail", ""), "suggest_emotion": it.get("suggest_emotion", ""),
                           "suggest_line": it.get("suggest_line", ""), "evidence": evs})
    return {"verdict": data.get("verdict", ""), "chips": (data.get("chips") or [])[:4], "issues": issues}


# ---------- ② 话术结构（时间轴纯算法 + LLM deep ×1） ----------
_FRAME = ["开场留人", "讲款种草", "报价锚定", "促单逼单", "互动宠粉", "成交服务"]

def _timeline(sess, segs: list[dict]) -> list[dict]:
    st = sess.started_at
    out: list[dict] = []
    for s in segs:
        lbl = s["data"].get("scene_label") or s["data"].get("seg_type") or "讲解"
        if out and out[-1]["label"] == lbl and s["start_ms"] - out[-1]["end_ms"] < 120_000:
            out[-1]["end_ms"] = s["end_ms"]
        else:
            out.append({"label": lbl, "start_ms": s["start_ms"], "end_ms": s["end_ms"],
                        "wall": _wall(st, s["start_ms"])})
    return out


async def _structure(sess, segs: list[dict], summary_data: dict) -> dict:
    st = sess.started_at
    tl = _timeline(sess, segs)
    flow = summary_data.get("structure") or {}
    loop = summary_data.get("loop") or {}
    # 供 LLM 引用的段锚（低分段+促单/报价相关段优先）
    cand = sorted(segs, key=lambda s: (s["score"] if s["score"] is not None else 10))[:18]
    lines = [f"[S{i}|{_wall(st, s['start_ms'])}|{s['data'].get('scene_label','')}] {s['quote'][:80]}"
             for i, s in enumerate(cand)]
    sysp = (
        "你是直播结构教练。基于环节结构与讲款闭环诊断，输出：结构病 + 标准框架对照。"
        "标准框架固定六环节：开场留人/讲款种草/报价锚定/促单逼单/互动宠粉/成交服务。"
        "只输出 JSON：{\"verdict\":\"≤50字\",\"chips\":[\"≤10字\"×2-4],"
        "\"issues\":[{\"title\":\"≤26字\",\"sev\":\"red|orange\",\"anchors\":[\"S序号\"],"
        "\"why\":\"为什么是问题≤50字\",\"how\":\"应该怎么做≤50字\"}],"
        "\"frame\":[{\"name\":\"六环节名\",\"status\":\"ok|weak|miss\",\"note\":\"≤16字\","
        "\"demo\":\"该环节示范话术,可照读≤50字\"}×6]}。"
        "issues 最多 4 条且必须带 anchors；frame 六个环节都要给，示范话术不得编价格参数库存。"
    )
    user = (f"【实际环节结构】{json.dumps(flow, ensure_ascii=False)[:600]}\n"
            f"【讲款闭环诊断】{json.dumps(loop, ensure_ascii=False)[:400]}\n"
            f"【代表性段落(可引用锚号)】\n" + "\n".join(lines))
    data = await _deep_json(sysp, user)
    issues = []
    for it in (data.get("issues") or [])[:4]:
        evs = []
        for a in it.get("anchors") or []:
            m = re.search(r"\d+", str(a))
            if m and int(m.group(0)) < len(cand):
                s = cand[int(m.group(0))]
                evs.append(_ev(st, s["quote"], s["start_ms"], s["end_ms"]))
        if evs:
            issues.append({"title": it.get("title", ""), "sev": it.get("sev", "orange"),
                           "why": it.get("why", ""), "how": it.get("how", ""), "evidence": evs})
    frame = [f for f in (data.get("frame") or []) if f.get("name") in _FRAME][:6]
    return {"verdict": data.get("verdict", ""), "chips": (data.get("chips") or [])[:4],
            "timeline": tl, "issues": issues, "frame": frame}


# ---------- ③ 质检（纯聚合，零 LLM） ----------
def _qc(sess, segs: list[dict], flags: list[dict]) -> dict:
    st = sess.started_at
    items: list[dict] = []
    seen: set[str] = set()

    def classify(text: str) -> tuple[str, str] | None:
        """返回 (sev, kind);None=不纳入质检。规则(2026-07-15 用户定):
        红线=违规绝对词/破价/导流;待核实=型号价格与知识库不符(库可能未收录/促销价);
        产品准确性=参数口误讲错;纯节奏建议不进质检。
        判定顺序有讲究:型号类 issue 文本常自带"违规/编造"字样,必须先于红线词判 pending;
        "未回应/漏答"是回应质量问题,不是违规,也先于红线词摘出。"""
        if any(k in text for k in _KW_RHYTHM_ONLY):
            return None
        if _PRICE_RE.search(text) and not any(k in text for k in _SEV_RED):
            return None       # 价格不做核对(直播间价格随活动变),提到知识库的价格差异也不进「待核实」;只有破价(红线词)才报
        if any(k in text for k in _KW_PENDING):
            return "pending", "待核实(知识库未收录)"
        if re.search(r"未回应|漏答|未提供|未解答|没有回应|答非所问|未明确回答|未答|未讲解|未说明", text):
            return "orange", "表达问题"
        if any(k in text for k in _SEV_RED):
            return "red", "违规红线"
        if _PRICE_RE.search(text):
            return None       # 价格差异不做实时质检(促销/时段价),只有破价(红线词)才报
        if any(k in text for k in _KW_ACCURACY):
            return "orange", "产品准确性"
        return None

    def push(text: str, quote: str, s_ms: int, e_ms: int, forced: tuple | None = None):
        text = (text or "").strip()
        if not text:
            return
        key = re.sub(r"\s", "", text)[:24]
        if key in seen:
            return
        cls = forced or classify(text)
        if not cls:
            return
        seen.add(key)
        sev, kind = cls
        items.append({"title": text[:60], "sev": sev, "kind": kind,
                      "evidence": [_ev(st, quote, s_ms, e_ms)]})

    for s in segs:
        for iss in s["data"].get("issues") or []:
            t = iss if isinstance(iss, str) else json.dumps(iss, ensure_ascii=False)
            push(t, s["quote"], s["start_ms"], s["end_ms"])
    for f in flags:
        if f["severity"] in ("error", "warn"):
            push(f["comment"] or f["quote"], f["quote"], f["start_ms"], f["end_ms"])
    order = {"red": 0, "orange": 1, "pending": 2}
    items.sort(key=lambda x: (order.get(x["sev"], 3), x["evidence"][0]["start_ms"]))
    # 分级保额:红线全保,其余截断;确保"待核实"不被挤出(此前 16 条硬顶把 pending 全截掉)
    reds = [x for x in items if x["sev"] == "red"]
    rest = [x for x in items if x["sev"] != "red"]
    items = reds + rest[:max(4, 20 - len(reds))]
    n_red = sum(1 for x in items if x["sev"] == "red")
    n_pend = sum(1 for x in items if x["sev"] == "pending")
    n_org = len(items) - n_red - n_pend
    return {"verdict": f"本场质检：{n_red} 处红线、{n_org} 处表达/准确性问题、{n_pend} 处待核实(知识库未收录,需产品手册裁决)。",
            "chips": [f"红线 {n_red}", f"准确性 {n_org}", f"待核实 {n_pend}"], "issues": items}


# ---------- ④ 弹幕回复（过滤 + LLM fast ×1 批量） ----------
def _dm_name(text: str) -> str:
    return text.split("：", 1)[0].split(":", 1)[0] if ("：" in text or ":" in text) else ""


def _dm_body(text: str) -> str:
    for sep in ("：", ":"):
        if sep in text:
            return text.split(sep, 1)[1].strip()
    return text.strip()


async def _danmaku(sess, trs: list[dict], dms: list[dict]) -> dict:
    st = sess.started_at
    kept, filtered = [], 0
    for d in dms:
        name, body = _dm_name(d["text"]), _dm_body(d["text"])
        if not body or _OFFICIAL_PAT.search(name) or _NOISE_PAT.match(body):
            filtered += 1
            continue
        kind = "intent" if any(k in body for k in _INTENT_KW) else (
            "question" if any(k in body for k in _QUESTION_KW) else "")
        if kind:
            kept.append({"ms": d["start_ms"], "name": name, "body": body, "kind": kind})
    detected = len(kept)
    kept = kept[:100]      # 上限 30→100(此前 3/4 被静默截断);verdict 里明示检测/分析数
    if not kept:
        return {"verdict": "本场无有效意向/提问弹幕", "chips": [f"已过滤 {filtered} 条"],
                "intent": [], "question": [], "filtered": filtered}
    lines = []
    for i, d in enumerate(kept):
        talk = _talk_window(trs, d["ms"], 15_000, 90_000) or "（该时段无主播话术）"
        lines.append(f"[D{i}|{_wall(st, d['ms'])}] 弹幕:{d['body'][:50]}\n  主播随后的回答:{talk}")
    sysp = (
        "你是直播弹幕回复质检员。逐条分析观众弹幕与主播回应。"
        "只输出 JSON：{\"items\":[{\"i\":D序号数字,\"cat\":\"intent|question|chat\","
        "\"strength\":\"strong|weak\",\"basis\":\"意向判定依据≤40字(仅intent填):看提问维度的全面与具体度——"
        "同时问到预算/场景/参数/物流等多个维度=强意向\","
        "\"verdict\":\"good|bad|slow|ignored\",\"problem\":\"哪句有问题/为什么≤50字,good 时写亮点\","
        "\"better\":\"应该怎么答,可照读≤60字,good 时可空\"}]}。"
        "cat 判定：明确要买/问价问链接问发货=intent；咨询了解=question；纯闲聊起哄跟播=chat(chat 不用填其他字段)。"
        "意向不能只看嘴上说想要——要结合提问维度做语义意图分析,basis 里写清依据。"
        "verdict 判定：答非所问/漏答=ignored；答了但推错方向=bad；拖沓反复=slow；接得准且自然承接=good。"
        "不得编造型号价格库存。"
    )
    data = await _deep_json(sysp, "\n".join(lines))
    by_i = {}
    for it in data.get("items") or []:
        try:
            by_i[int(it.get("i"))] = it
        except Exception:  # noqa: BLE001
            pass
    intent, question, n_chat = [], [], 0
    for i, d in enumerate(kept):
        r = by_i.get(i) or {}
        cat = r.get("cat") or d["kind"]
        if cat == "chat":
            n_chat += 1
            continue
        row = {"dm": d["body"], "name": d["name"], "wall": _wall(st, d["ms"]),
               "verdict": r.get("verdict", "good"), "problem": r.get("problem", ""),
               "better": r.get("better", ""),
               "strength": r.get("strength", ""), "basis": r.get("basis", ""),
               "evidence": [_ev(st, d["body"], d["ms"], d["ms"] + 60_000)]}
        (intent if cat == "intent" else question).append(row)
    n_bad = sum(1 for x in intent + question if x["verdict"] in ("bad", "ignored"))
    over = "(超出分析上限,截取前100)" if detected > 100 else ""
    return {"verdict": f"检测到候选弹幕 {detected} 条{over}：意向 {len(intent)} / 提问 {len(question)} / "
                       f"闲聊剔除 {n_chat}；{n_bad} 条回应有问题；已过滤官方与告知类 {filtered} 条。",
            "chips": [f"检测 {detected}", f"意向 {len(intent)}", f"提问 {len(question)}", f"问题回应 {n_bad}"],
            "intent": intent, "question": question, "filtered": filtered, "detected": detected}


# ---------- ⑤ 罗盘分析（事件检测纯算法 + LLM deep ×1） ----------
def _num(x):
    try:
        return float(str(x).replace(",", "").replace("¥", ""))
    except Exception:  # noqa: BLE001
        return None


def _compass_events(comp: list[dict]) -> dict:
    """三类事件：成交(支付金额跳变) / 增粉(30min窗新增粉丝) / 互动(30min窗评论增量 top)。"""
    ev = {"sales": [], "fans": [], "interact": []}
    prev_pay = None
    for p in comp:
        pay = _num(p.get("直播间用户支付金额"))
        if pay is None or pay > 1e7:      # 罗盘 OCR 偶发异常大数（字段串位），直接丢弃该点
            continue
        if prev_pay is not None and pay > prev_pay:
            delta = round(pay - prev_pay)
            if 0 < delta <= 200_000:      # 单事件超 20 万必是异常值，不进归因
                ev["sales"].append({"ms": p["ms"], "delta": delta})
        prev_pay = pay
    win = 30 * 60 * 1000
    for key, out in (("新增粉丝数", "fans"), ("评论次数", "interact")):
        pts = [(p["ms"], _num(p.get(key))) for p in comp if _num(p.get(key)) is not None]
        cands = []
        for i, (ms, v) in enumerate(pts):
            base = next((bv for bms, bv in reversed(pts[:i]) if ms - bms >= win * 0.8), None)
            if base is not None and v - base > 0:
                cands.append({"ms": ms, "delta": round(v - base)})
        cands.sort(key=lambda x: -x["delta"])
        picked = []
        for c in cands:
            if all(abs(c["ms"] - q["ms"]) > win for q in picked):
                picked.append(c)
            if len(picked) >= 3:
                break
        ev[out] = sorted(picked, key=lambda x: x["ms"])
    ev["sales"] = sorted(ev["sales"], key=lambda x: -x["delta"])[:4]
    ev["sales"].sort(key=lambda x: x["ms"])
    return ev


async def _compass(sess, trs: list[dict], comp: list[dict]) -> dict:
    st = sess.started_at
    evs = _compass_events(comp)
    if not any(evs.values()):
        return {"verdict": "本场无罗盘事件（数据不足或未接入）", "chips": [], "sales": [], "fans": [], "interact": []}
    lines, index = [], []
    anchor_map: dict[int, list] = {}    # 事件i -> 该事件窗口的话术行(锚号→start_ms 映射)
    for kind, label in (("sales", "成交"), ("fans", "增粉"), ("interact", "互动")):
        for e in evs[kind]:
            i = len(index)
            index.append((kind, e))
            unit = "元" if kind == "sales" else ("人" if kind == "fans" else "条")
            # 话术见效滞后 15-30 分钟：窗口向前 30 分钟。成交事件给【全量话术+弹幕】逐行带锚号,
            # 让模型还原"哪一句话/哪个动作促成下单"的关键时刻线(2026-07-15 细化)。
            win_tr = [t for t in trs if e["ms"] - 30 * 60_000 <= t["start_ms"] <= e["ms"] + 60_000 and t["text"]]
            if kind == "sales":
                rows = []
                anchor_map[i] = win_tr
                for j, t in enumerate(win_tr):
                    rows.append(f"  [L{j}|{_wall(st, t['start_ms'])}] {t['text'][:60]}")
                body = "\n".join(rows[-260:]) or "（窗口内无话术）"
                lines.append(f"[E{i}|{_wall(st, e['ms'])}|{label}+{e['delta']}{unit}] 窗口内全量话术(带锚号):\n{body}")
            else:
                talk = " / ".join(t["text"] for t in win_tr)[:700] or "（窗口内无话术）"
                lines.append(f"[E{i}|{_wall(st, e['ms'])}|{label}+{e['delta']}{unit}] 此前30分钟话术采样:{talk}")
    sysp = (
        "你是直播罗盘归因分析师。对每个事件，从此前 30 分钟话术里归纳因果：主播说了什么/做了什么动作"
        "导致了这次成交/增粉/互动（话术见效通常滞后 15-30 分钟，重点看铺垫动作）。"
        "只输出 JSON：{\"items\":[{\"i\":E序号数字,\"title\":\"≤24字概括\","
        "\"chain\":\"因果链:动作1→动作2→结果 ≤80字\",\"advice\":\"可复制建议≤40字\","
        "\"moments\":[{\"anchor\":\"L序号(仅成交事件,引用窗口话术锚号)\",\"act\":\"动作类型≤8字"
        "(如场景种草/对比锚定/风险逆转/价格锚定/需求锁定/行动指令)\",\"why\":\"这句话起了什么作用≤30字\"}]"
        "(仅成交事件填,按时间序 2-5 个关键时刻,还原促成下单的具体动作)}]}。"
        "找不到可信因果就写 title=\"无明确话术因果\"。moments 必须引用真实锚号,给不出锚号的时刻不要编。"
    )
    data = await _deep_json(sysp, "\n".join(lines))
    by_i = {}
    for it in data.get("items") or []:
        try:
            by_i[int(it.get("i"))] = it
        except Exception:  # noqa: BLE001
            pass
    out = {"sales": [], "fans": [], "interact": []}
    for i, (kind, e) in enumerate(index):
        r = by_i.get(i) or {}
        unit = "元" if kind == "sales" else ("人" if kind == "fans" else "条")
        moments = []
        win_tr = anchor_map.get(i) or []
        for m in (r.get("moments") or [])[:5]:
            mm = re.search(r"\d+", str(m.get("anchor", "")))
            if mm and int(mm.group(0)) < len(win_tr):
                t = win_tr[int(mm.group(0))]
                moments.append({"wall": _wall(st, t["start_ms"]), "act": m.get("act", ""),
                                "why": m.get("why", ""), "quote": t["text"][:70],
                                "evidence": [_ev(st, t["text"], t["start_ms"], t["end_ms"])]})
        out[kind].append({"wall": _wall(st, e["ms"]), "delta": f"+{e['delta']}{unit}",
                          "title": r.get("title", "待归因"), "chain": r.get("chain", ""),
                          "advice": r.get("advice", ""), "moments": moments,
                          "evidence": [_ev(st, "", max(0, e["ms"] - 30 * 60_000), e["ms"])]})
    return {"verdict": f"成交事件 {len(out['sales'])} / 增粉时段 {len(out['fans'])} / 互动高峰 {len(out['interact'])}，"
                       "均已按 30 分钟滞后窗归因。",
            "chips": [f"成交 {len(out['sales'])}", f"增粉 {len(out['fans'])}", f"互动 {len(out['interact'])}"],
            **out}


def _fill_quotes(v2: dict, trs: list[dict]) -> None:
    """证据链原话回填:quote 为空的 evidence,从逐字稿取该时点后 1-2 句话术。
    历史段评 quote 全空(2026-07-15 之前),没有原话的证据在前端就是空白行。"""
    def fill(evs):
        for e in evs or []:
            if (e.get("quote") or "").strip():
                continue
            ms = e.get("start_ms", 0)
            win = [t["text"] for t in trs if ms <= t["start_ms"] <= ms + 45_000 and t["text"]]
            if not win:
                win = [t["text"] for t in trs if abs(t["start_ms"] - ms) <= 60_000 and t["text"]][:2]
            e["quote"] = " / ".join(win[:2])[:90]
    for blk in v2.values():
        if not isinstance(blk, dict):
            continue
        for key in ("issues", "intent", "question", "sales", "fans", "interact", "items"):
            for it in blk.get(key) or []:
                fill(it.get("evidence"))
                for m in it.get("moments") or []:
                    fill(m.get("evidence"))


# ---------- ⑥ 章节级整段复盘(同产品/同动作聚合,2026-07-15 用户规则) ----------
_SCENE_GROUP = (("促单", "促单逼单"), ("逼单", "促单逼单"), ("成交", "促单逼单"),
                ("互动", "互动拉粉"), ("宠粉", "互动拉粉"), ("粉", "互动拉粉"),
                ("答疑", "答疑"), ("问", "答疑"),
                ("开场", "开场留人"), ("闲", "闲置过渡"), ("过渡", "闲置过渡"))


def _scene_group(label: str) -> str:
    for k, g in _SCENE_GROUP:
        if k in (label or ""):
            return g
    return "讲品"


def _agg_chapters(segs: list[dict]) -> list[dict]:
    """聚合成"整段":同一产品连续讲=一章(中间夹无产品的过渡不断链);
    断链仅当:①间隔≥4分钟 ②产品明确切换 ③双方都无产品且动作大类不同 ④章节满30分钟。
    再做碎段吸收(时长<90s 并入前章),目标一场 20-50 章。"""
    chs: list[dict] = []
    for s0 in segs:
        prod = ((s0["data"].get("metrics") or {}).get("products") or [""])[0] or ""
        grp = _scene_group(s0["data"].get("scene_label") or s0["data"].get("seg_type") or "")
        cur = chs[-1] if chs else None
        gap = (s0["start_ms"] - cur["end_ms"]) if cur else 10 ** 15
        brk = (cur is None or gap >= 240_000
               or (prod and cur["product"] and prod != cur["product"])
               or (not prod and not cur["product"] and grp != cur["group"])
               or (s0["end_ms"] - cur["start_ms"] >= 30 * 60_000))
        if brk:
            chs.append({"start_ms": s0["start_ms"], "end_ms": s0["end_ms"],
                        "product": prod, "group": grp, "segs": [s0]})
        else:
            cur["end_ms"] = max(cur["end_ms"], s0["end_ms"])
            cur["segs"].append(s0)
            if prod and not cur["product"]:
                cur["product"] = prod
    # 碎段吸收(两轮):<90s 的章并入前章,消灭 1 分钟碎片
    for _ in range(2):
        merged: list[dict] = []
        for c in chs:
            dur = c["end_ms"] - c["start_ms"]
            prev = merged[-1] if merged else None
            if prev and dur < 90_000 and (c["start_ms"] - prev["end_ms"]) < 300_000:
                prev["end_ms"] = max(prev["end_ms"], c["end_ms"])
                prev["segs"] += c["segs"]
                if c["product"] and not prev["product"]:
                    prev["product"] = c["product"]
            else:
                merged.append(c)
        chs = merged
    return chs


async def _chapters(sess, segs: list[dict], dms: list[dict], trs: list[dict] | None = None,
                    *, keep: list[dict] | None = None) -> dict:
    """章节级整段复盘:不逐句挑刺,针对一整段(同产品/同动作)输出
    包含结构/缺失结构/哪里不好/应该怎么讲 + 闲置/偷懒硬规则检测。"""
    st = sess.started_at
    chs = _agg_chapters(segs)
    if not chs:
        return {"verdict": "无可聚合章节", "chips": [], "items": []}
    sysp = (
        "你是带货直播复盘教练。下面每个 [C序号] 是【一整段连续直播内容】(同一产品或同一动作),"
        "对每段做【整体复盘】——综合整段看,不逐句挑刺。"
        "只输出 JSON:{\"items\":[{\"i\":C序号数字,"
        "\"type\":\"讲品|促单逼单|互动拉粉|答疑|开场留人|闲置过渡\","
        "\"structures\":[\"整段包含的话术结构,如 痛点唤醒/卖点讲解/参数适配/场景种草/价格锚定/稀缺紧迫/行动指令/从众证明/风险逆转/需求挖掘,≤5个\"],"
        "\"missing\":[\"该类型本应有但缺失的关键结构,≤3个\"],"
        "\"problems\":[\"整体视角哪里做得不好,≤3条,每条≤40字\"],"
        "\"how_to\":\"这一整段应该怎么讲,结合类型给整体改法≤80字\","
        "\"idle\":\"none|prep|lazy\",\"score\":0-10}]}。"
        "硬规则(必须执行):"
        "①type 看整段实质:只介绍塑造产品、没有催下单动作 = 讲品,不得因出现价格就标促单;"
        "②直播中不允许闲置——与身边人/场控沟通、做准备工作、设备调试、长时间不讲商品 → idle=prep,"
        "problems 里必须点名『准备不足/空余时间过多,闲置时间也必须讲品』;"
        "③反复说『还有没有什么产品要讲/接下来讲什么』之类 → idle=lazy,problems 点名『偷懒行为』;"
        "④促单段评话术结构与转化力、用户能否听懂;讲品段评种草力与能否听懂;互动拉粉段评钩子设计与承接;"
        "⑤价格随直播活动变化,不要拿知识库价格核对、批评主播的报价;逐字稿是语音识别的,型号昵称的同音错字(如中额=中鹅)不算口误。"
    )
    trs = trs or []
    # 补跑时(keep=上次已出的整段)：起止时间还对得上的直接沿用，只跑缺的，不重复花钱
    spans = {(c["start_ms"], c["end_ms"]) for c in chs}
    kept: dict[tuple, dict] = {}
    for it in keep or []:
        span = (it.get("start_ms"), it.get("end_ms")) if isinstance(it, dict) else None
        if span in spans:
            kept.setdefault(span, it)
    items_out = list(kept.values())
    todo = [i for i, c in enumerate(chs) if (c["start_ms"], c["end_ms"]) not in kept]

    async def one_batch(idxs: list[int]) -> list[dict]:
        lines = []
        for i in idxs:
            c = chs[i]
            # 话术直接取逐字稿(历史场段评 quote 为空,靠它就会把整段误判"无话术采样→闲置")
            talk = _talk_window(trs, c["start_ms"], 0, c["end_ms"] - c["start_ms"], cap=1200) \
                or " / ".join((s["quote"] or "")[:60] for s in c["segs"] if s["quote"])[:1200]
            dm_win = [d["text"].split("：", 1)[-1] for d in dms
                      if c["start_ms"] <= d["start_ms"] <= c["end_ms"]][:8]
            mins = round((c["end_ms"] - c["start_ms"]) / 60000)
            lines.append(f"[C{i}|{_wall(st, c['start_ms'])}-{_wall(st, c['end_ms'])}|约{mins}分钟|"
                         f"初判:{c['group']}|产品:{c['product'] or '未识别'}]\n"
                         f"  整段话术采样:{talk or '(无)'}\n  期间弹幕:{' | '.join(dm_win) or '(无)'}")
        data = await _deep_json(sysp, "\n".join(lines))
        got: dict[int, dict] = {}
        for it in data.get("items") or []:
            try:
                idx = int(it.get("i"))
            except Exception:  # noqa: BLE001
                continue
            if idx not in idxs or idx in got:      # 只认这一批给出去的序号，同一段只收一次
                continue
            c = chs[idx]
            got[idx] = {
                "start_ms": c["start_ms"], "end_ms": c["end_ms"],
                "wall": f"{_wall(st, c['start_ms'])}–{_wall(st, c['end_ms'])}",
                "type": it.get("type") or c["group"], "product": c["product"],
                "structures": (it.get("structures") or [])[:5],
                "missing": (it.get("missing") or [])[:3],
                "problems": (it.get("problems") or [])[:3],
                "how_to": it.get("how_to", ""), "idle": it.get("idle", "none"),
                "score": it.get("score"),
                "evidence": [_ev(st, (c["segs"][0].get("quote") or ""), c["start_ms"], c["end_ms"])],
            }
        return list(got.values())

    # 分批(每批≤8章,防输出超 max_tokens 截断致 JSON 解析失败)；各批互不依赖，同时发
    batches = [todo[k:k + 8] for k in range(0, len(todo), 8)]
    errors, lost = [], 0
    for idxs, res in zip(batches, await _settled(one_batch(b) for b in batches)):
        if isinstance(res, Exception):
            errors.append(res)
            lost += len(idxs)
        else:
            items_out.extend(res)
    if errors and not items_out:
        raise errors[0]                            # 一段都没出来：整块按失败处理
    for e in errors:
        print(
            "[deep_analysis] chapters 有一批失败; "
            f"code={safe_exception_code(e, prefix='DEEP_CHAPTERS')}; error={safe_error_text(e)}"
        )
    items_out.sort(key=lambda x: x["start_ms"])
    n_idle = sum(1 for x in items_out if x.get("idle") in ("prep", "lazy"))
    out = {"verdict": f"全场聚合为 {len(items_out)} 个整段(同产品/同动作),其中 {n_idle} 段存在闲置/偷懒,"
                      "每段按『包含结构/缺失/问题/怎么讲』整体复盘。",
           "chips": [f"整段 {len(items_out)}", f"闲置/偷懒 {n_idle}"], "items": items_out}
    if errors:
        # 有的批没成：已出来的先留着（钱已经花了），本块标为待补，补跑时只跑缺的那几段
        out["failed"] = True
        out["verdict"] += f"（另有 {lost} 段没生成出来，可稍后补跑）"
    return out


# ---------- ⑦ 产品级综合复盘(2026-07-15:同一产品全场时段合并,一份整体判词) ----------
async def _products(sess, chapters: dict) -> dict:
    items = (chapters or {}).get("items") or []
    groups: dict[str, list] = {}
    for c in items:
        if c.get("product"):
            groups.setdefault(c["product"], []).append(c)
    picked = {p: cs for p, cs in groups.items()
              if len(cs) >= 2 or sum(x["end_ms"] - x["start_ms"] for x in cs) >= 10 * 60_000}
    if not picked:
        return {"items": []}
    lines = []
    keys = list(picked.keys())
    for i, p in enumerate(keys):
        cs = picked[p]
        mins = round(sum(x["end_ms"] - x["start_ms"] for x in cs) / 60000)
        probs = "；".join(q for c in cs for q in (c.get("problems") or []))[:400]
        miss = "、".join({m for c in cs for m in (c.get("missing") or [])})[:120]
        quotes = " / ".join((c.get("evidence") or [{}])[0].get("quote", "")[:50] for c in cs)[:600]
        lines.append(f"[P{i}] 产品:{p} 共{len(cs)}段/约{mins}分钟 平均分:{[c.get('score') for c in cs]}\n"
                     f"  各段问题汇总:{probs or '无'}\n  各段缺失结构:{miss or '无'}\n  话术采样:{quotes}")
    sysp = (
        "你是带货直播复盘教练。下面每个 [P序号] 是【同一个产品在全场的全部讲解时段汇总】。"
        "对每个产品输出一份【整体复盘判词】——综合全场看这个产品讲得怎么样,不要复述各段细节。"
        "只输出 JSON:{\"items\":[{\"i\":P序号数字,\"verdict\":\"该产品全场讲解的整体判断≤60字\","
        "\"structures\":[\"全场覆盖到的话术结构≤5个\"],\"missing\":[\"全场始终没做的关键结构≤3个\"],"
        "\"problems\":[\"整体最要命的问题≤3条,每条≤40字\"],\"how_to\":\"这个产品今后应该怎么讲≤80字\","
        "\"score\":0-10}]}。不得编造参数价格库存。"
    )
    data = await _deep_json(sysp, "\n".join(lines))
    by_i = {}
    for it in data.get("items") or []:
        try:
            by_i[int(it.get("i"))] = it
        except Exception:  # noqa: BLE001
            pass
    out = []
    for i, p in enumerate(keys):
        r = by_i.get(i) or {}
        cs = picked[p]
        out.append({"product": p, "segments": len(cs),
                    "minutes": round(sum(x["end_ms"] - x["start_ms"] for x in cs) / 60000),
                    "verdict": r.get("verdict", ""), "structures": (r.get("structures") or [])[:5],
                    "missing": (r.get("missing") or [])[:3], "problems": (r.get("problems") or [])[:3],
                    "how_to": r.get("how_to", ""), "score": r.get("score")})
    return {"items": out}


# ---------- 汇总入口 ----------
BLOCKS = ("qc", "tone", "structure", "danmaku", "compass", "chapters", "products")
_FAILED_VERDICT = "生成失败，可稍后重试"


def _failed_block(key: str) -> dict:
    if key == "products":
        return {"items": [], "failed": True}
    return {"verdict": _FAILED_VERDICT, "chips": [], "issues": [], "failed": True}


def failed_blocks(v2: dict | None) -> list[str]:
    """一份 v2 里需要补跑的块：生成失败的（带 failed 标记；2026-09-30 之前存的旧数据认占位文案）
    和缺失的。"""
    v2 = v2 or {}
    out = []
    for key in BLOCKS:
        blk = v2.get(key)
        if not isinstance(blk, dict) or blk.get("failed") or blk.get("verdict") == _FAILED_VERDICT:
            out.append(key)
    return out


def _summary_data(session_id: int, streamer_id) -> dict:
    """该主播「整场总结」那条里已存的数据（与 merge_summary_data 取的是同一行）。"""
    db = SessionLocal()
    try:
        srow = db.execute(select(Critique).where(
            Critique.session_id == session_id, Critique.level == CritiqueLevel.summary,
            Critique.streamer_id == streamer_id, Critique.category == "整场总结",
        ).order_by(Critique.id)).scalars().first()
        raw = srow.data if srow else ""
    finally:
        db.close()
    try:
        data = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        data = {}
    return data if isinstance(data, dict) else {}


async def _analyze(session_id: int, streamer_id, base: dict | None) -> tuple[dict, list[str], dict]:
    """返回 (v2, 这次跑了哪些块, 模型调用成败计数)。base=上次的 v2 时只补跑其中失败/缺失的块。"""
    calls = {"ok": 0, "failed": 0}
    sess, segs, flags, trs, dms, comp = _load(session_id, streamer_id)
    if sess is None:
        return {}, [], calls
    # 弹幕/罗盘按该主播实际在播窗口过滤——否则会把别人时段的弹幕判成"本主播没回应"、
    # 把别人时段的成交归因到本主播头上（多主播轮班场的归属错误）。
    if trs:
        lo, hi = trs[0]["start_ms"] - 60_000, trs[-1]["end_ms"] + 60_000
        dms = [d for d in dms if lo <= d["start_ms"] <= hi]
        comp = [p for p in comp if lo <= p["ms"] <= hi]
    sdata = _summary_data(session_id, streamer_id)   # summary data 供 structure 复用 flow/loop
    wanted = set(BLOCKS) if base is None else set(failed_blocks(base))
    if "chapters" in wanted:
        wanted.add("products")                       # 产品判词建立在章节之上，章节重跑它也得重跑
    old_chapters = (base or {}).get("chapters")
    keep = old_chapters.get("items") if isinstance(old_chapters, dict) else None
    out: dict = {**(base or {}), "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M")}

    async def block(key: str, factory) -> None:
        """跑一块并放进结果；这一块失败只降级它自己。"""
        try:
            value = factory()
            out[key] = await value if asyncio.iscoroutine(value) else value
        except Exception as e:  # noqa: BLE001
            print(
                f"[deep_analysis] {key} 失败 session={session_id} sid={streamer_id}; "
                f"code={safe_exception_code(e, prefix='DEEP_ANALYSIS')}; "
                f"error={safe_error_text(e)}"
            )
            # 补跑时又没成：上次留下的内容（比如只出了一部分的章节）先留着，只标记仍待补
            old = (base or {}).get(key)
            out[key] = {**old, "failed": True} if isinstance(old, dict) and old else _failed_block(key)

    async def chapters_then_products() -> None:
        if "chapters" in wanted:
            await block("chapters", lambda: _chapters(sess, segs, dms, trs, keep=keep))
        if "products" in wanted:
            await block("products", lambda: _products(sess, out.get("chapters") or {}))

    token = _calls.set(calls)
    try:
        if "qc" in wanted:
            await block("qc", lambda: _qc(sess, segs, flags))
        # 各块作为子任务同时跑。本任务被取消（服务关闭）时子任务一起停，
        # 排在章节之后的产品判词还没建出来，不会漏出 "never awaited" 或继续花钱。
        async with asyncio.TaskGroup() as tg:
            # 并发名额先到先得，先后就是这里的顺序：弹幕块单次最慢，章节→产品是最长的一条链，
            # 让它们先占位；其余三块短，排在后面不拖总时长。
            if "danmaku" in wanted:
                tg.create_task(block("danmaku", lambda: _danmaku(sess, trs, dms)))
            if wanted & {"chapters", "products"}:
                tg.create_task(chapters_then_products())
            await asyncio.sleep(0)   # 让出一拍：章节的各批这时才排进队，要排在下面三块前头
            for key, factory in (
                ("compass", lambda: _compass(sess, trs, comp)),
                ("structure", lambda: _structure(sess, segs, sdata)),
                ("tone", lambda: _tone(sess, segs)),
            ):
                if key in wanted:
                    tg.create_task(block(key, factory))
    finally:
        _calls.reset(token)
    try:
        _fill_quotes(out, trs)
    except Exception as e:  # noqa: BLE001
        print(
            "[deep_analysis] 证据回填失败; "
            f"code={safe_exception_code(e, prefix='DEEP_EVIDENCE')}; "
            f"error={safe_error_text(e)}"
        )
    return out, [k for k in BLOCKS if k in wanted], calls


async def analyze_v2(session_id: int, streamer_id, *, base: dict | None = None) -> dict:
    """生成 v2 各块分析；单块失败独立降级。base=上次的 v2 时只补跑其中失败/缺失的块。"""
    return (await _analyze(session_id, streamer_id, base))[0]


@dataclass
class V2Outcome:
    """一次生成的结局。ok=False 时库里的 v2 没被动过，error 是给人看的原因。"""

    ok: bool = False
    v2: dict = field(default_factory=dict)
    reran: list[str] = field(default_factory=list)    # 这次实际跑了哪些块
    failed: list[str] = field(default_factory=list)   # 跑完后仍然失败、等着补跑的块
    error: str = ""


async def generate_v2(session_id: int, streamer_id, *, only_failed: bool = False) -> V2Outcome:
    """生成 v2 并合并进该主播 summary critique 的 data.v2（幂等，可重复执行）。
    only_failed=True 只补跑上次失败/缺失的块、其余原样保留；从没生成过就等同全量。"""
    from .report_data import merge_summary_data

    base = None
    if only_failed:
        stored = _summary_data(session_id, streamer_id).get("v2")
        base = stored if isinstance(stored, dict) and stored else None
        if base is not None and not failed_blocks(base):
            return V2Outcome(ok=True, v2=base)        # 没有失败的块：不调模型、不动数据
    v2, reran, calls = await _analyze(session_id, streamer_id, base)
    if not v2:
        return V2Outcome(error="场次不存在")
    failed = failed_blocks(v2)
    if calls["failed"] and not calls["ok"]:
        # 模型一次都没调成功（欠费/限流/断网）：不存，免得把已有的好结果盖成一排「生成失败」
        return V2Outcome(reran=reran, failed=failed,
                         error="模型调用全部失败（可能是欠费、限流或网络问题），本次没有保存，请稍后重试")
    merged = merge_summary_data(
        session_id, streamer_id, lambda data: data.__setitem__("v2", v2),
    )
    if merged is None:
        return V2Outcome(reran=reran, failed=failed, error="该主播还没有整场总结，深度复盘无处保存")
    return V2Outcome(ok=True, v2=v2, reran=reran, failed=failed)


async def save_v2(session_id: int, streamer_id, *, only_failed: bool = False) -> dict:
    """generate_v2 的简写：存上了返回 v2，没存上返回空 dict。"""
    outcome = await generate_v2(session_id, streamer_id, only_failed=only_failed)
    return outcome.v2 if outcome.ok else {}
