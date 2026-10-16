"""改进动作的存取、校验与对外数据形状（处理流程在 focus.py，对接接口在 api/partner.py）。

- 所有改动作的写操作都过同一把进程内锁 WRITE_LOCK：服务是单进程（uvicorn 单 worker），
  下播后的自动新增和业绩后台的人工增/换/删可能同时发生，锁住才能保证查重、排序不乱。
- 在练动作不设条数上限（本场能检查的问题有多少出多少），按 sort_order 排重要程度。
- 对外的时间一律北京时间 ISO（库里是本地 naive，服务器 TZ=Asia/Shanghai，补 +08:00）。
- 返回里凡是带主播的地方都给 {id, name, ext_id}（ext_id = 业绩系统的主播 id）；
  动作带 needs_ack（在练且主播还没点「记住了」），业绩系统靠它判断「都记住了没有」。
- 返回文字一律用中性的状态词，不点名任何具体的人。
- 不出动作的话题（2026-10-03 第二版规则）：价格、库存、优惠、券、定价、改价、站外交易（价格不做考核）；
  违禁词、绝对化用语（合规模块另外管）；亚秒级停顿、语速这类逐字稿判断不了的；型号叫全称还是昵称
  （要产品知识、语音识别也转不准）。模型生成的这类动作直接丢掉；人工加的不拦（那是操作人自己的决定）。
- 动作按「类别」归档（CATEGORIES）：同一位主播同一类只留一条在练的（「其他」类除外），
  一个问题的多个要点合成一条，不拆碎；跨场次也靠类别去重。
"""

from __future__ import annotations

import difflib
import json
import re
import threading
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...db import SessionLocal
from ...models import (Critique, CritiqueLevel, FocusAction, FocusCheck, FocusRun, ReplaySession,
                       Streamer)

PASS_STREAK = 3                # 确认后连续做到几场算过关
SILENCE_CAP_SEC = 180          # 超过这个秒数的空白多半是放歌试听或离场，不算冷场
DEFAULT_SILENCE_SEC = 10       # 人工建 silence 动作不填秒数时的默认值
TEXT_LIMIT = 30                # 动作本身的字数上限
TRIGGER_LIMIT = 60
CRITERION_LIMIT = 120
EXAMPLE_LIMIT = 100
ACTOR_LIMIT = 64

KINDS = ("llm", "silence")
REBUILD_MARK = "首批重整"       # 首批重整时下线的旧动作记在 updated_by：不算「人工删掉」，同类问题还能再出
SOFT_MAX_ACTIVE = 8            # 在练动作一般 3~8 条；超过时先让模型再合并一轮，合并不了才保留并记一笔

OTHER = "其他"
SILENCE_CATEGORY = "冷场"
# 动作的类别（同一类只留一条在练的；「其他」除外）。说明会原样放进给模型的提示词。
CATEGORIES = {
    "开场介绍": "开播、换班、新观众进来时，说清直播间定位、本场重点款、能不能试听",
    "讲款完整": "讲一款产品时把要点讲全：需求或适合人群、核心卖点、适配（曲风/前端/使用场景）、促单；"
                "报价前先讲价值、讲完问观众听什么歌也算这一类",
    "报价指令": "报完价后紧跟链接号和下单或试听指令（只管指令，不管价格本身）",
    "答疑承接": "回答观众提问：先复述问题或先给结论、按曲风/前端/预算给建议、答完给推荐或链接、不来回追问",
    "拉回主线": "闲聊、跑题、熟人或个人话题之后，及时拉回到产品",
    "空档讲品": "找链接、换设备、等客服、没人互动、离场前这些空档讲什么（讲品或留人）；停多久归「冷场」",
    "订单售后": "订单号、退换、售后问题引导私信或官方客服，不在公屏处理",
    SILENCE_CATEGORY: "相邻两句话之间空白太长（kind=silence，只管停多久）",
    OTHER: "确实不属于上面任何一类、又能只看逐字稿判断的",
}


def normalize_category(value, kind: str = "llm") -> str:
    if kind == "silence":
        return SILENCE_CATEGORY
    text = str(value or "").strip().strip("[]【】「」")
    if text == SILENCE_CATEGORY:
        return OTHER                    # 话术动作不能占「冷场」类
    return text if text in CATEGORIES else OTHER
STATUS_LABELS = {"active": "在练", "passed": "已过关", "replaced": "已换掉", "removed": "已删掉"}
KIND_LABELS = {"llm": "话术动作", "silence": "不冷场"}
VERDICT_LABELS = {"pass": "做到了", "fail": "没做到", "na": "本场不计"}
RUN_LABELS = {
    "done": "已核对", "partial": "部分完成", "skipped": "讲话太少，没核对", "failed": "处理失败",
}

WRITE_LOCK = threading.Lock()


class FocusError(Exception):
    """要原样告诉调用方的业务错误（HTTP 状态码 + 中文原因）。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# ---------- 小工具 ----------

def iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") + "+08:00" if dt else None


def loads(raw: str | None, default):
    try:
        value = json.loads(raw) if raw else default
    except (json.JSONDecodeError, TypeError, RecursionError):
        return default
    return value if isinstance(value, type(default)) else default


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:max(1, limit - 1)] + "…"


_NORM_RE = re.compile(r"[\s，。！？、；：,.!?;:\"'“”‘’（）()【】\[\]《》<>…—\-~～]+")


def norm(text: str) -> str:
    return _NORM_RE.sub("", str(text or "")).lower()


def similar(a: str, b: str) -> bool:
    """两条动作是不是同一件事（字面上几乎一样）。换说法的重复靠提示词挡，这里只兜底。"""
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if min(len(na), len(nb)) >= 8 and (na in nb or nb in na):
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.85


# 不出动作的话题（第二版规则，用 2026-10-03 线上首批 55 条动作校准过）
_EXCLUDED_TOPICS = re.compile(
    # 价格、库存、优惠、券、定价、改价、站外交易——价格不做考核
    r"库存|现货|优惠|券|折扣|满减|赠品|补贴|红包|返现|权益对比|定价|内部价|议价|改价|保价|锚定|原价|"
    r"日常价|划线价|比价|对比价|价格(?:或|和|与)?(?:权益)?对比|价格异议|站外|私下|线下交易|加微|微信|"
    r"待支付|别支付|不支付|先别付|取消支付|退差价|"
    # 违禁词、绝对化用语——合规模块另外管
    r"绝对化|极限词|违禁|敏感词|最好的|[“\"'‘](?:最好|第一|顶级|最强)|全网第一|行业第一|排名第一|"
    # 亚秒级停顿、语速——逐字稿判断不了
    r"0\.\d|零点\d|半秒|毫秒|亚秒|语速|抢话|"
    # 型号叫全称还是昵称、参数对不对——要产品知识，语音识别也转不准
    r"全称|简称|谐音|昵称|代号|知识库"
)
_ACCURACY_SUBJECT = re.compile(r"价|参数|规格|型号")
_ACCURACY_CHECK = re.compile(
    r"核实|核对|核准|校对|验证|准确|正确|一致|有误|无误|说错|讲错|口误|准不准|对不对|属实|不实"
)
# 「报完价后 / 报价前 / 再报价」只是时间点（比如报价后接链接号），动作本身不是价格，不算
_PRICE_AS_TIME = re.compile(
    r"(?:再|先)?(?:报|说|讲|念|喊)(?:出|完|了)?(?:到手|活动)?(?:价|价格|价钱)(?:之?前|之?后|时|的时候)?"
)


def is_excluded_item(text: str, trigger: str = "", criterion: str = "") -> bool:
    """这条动作是不是不该出的话题。「价格」这个词只看动作本身（时机、标准里常拿报价当时间点）。"""
    blob = " ".join(str(t or "") for t in (text, trigger, criterion))
    if _EXCLUDED_TOPICS.search(blob):
        return True
    if _ACCURACY_SUBJECT.search(blob) and _ACCURACY_CHECK.search(blob):
        return True
    head = _PRICE_AS_TIME.sub("", str(text or ""))
    return bool(re.search(r"价格|价位|价钱|报价", head))


def _bad_clause(part: str) -> bool:
    return bool(_EXCLUDED_TOPICS.search(part)
                or (_ACCURACY_SUBJECT.search(part) and _ACCURACY_CHECK.search(part)))


def clean_clauses(text: str) -> str:
    """时机、标准里顺手加的套话（「参数以知识库为准」「不引导站外私信」之类）只删那半句，不连累整条动作。"""
    original = str(text or "").strip()
    parts = re.split(r"(?<=[，。；,;])", original)
    kept = "".join(part for part in parts if part.strip() and not _bad_clause(part)).strip().rstrip("，,；;")
    if kept and original.endswith("。") and not kept.endswith("。"):
        kept += "。"
    return kept


def clean_example(example: str) -> str:
    """例句是主播会照着说的话：把提到库存、优惠、券、改价、付款方式这类的半句删掉；剩得太少就不要例句。"""
    parts = re.split(r"(?<=[，。！？；,!?;])", str(example or ""))
    kept = [part for part in parts if part.strip() and not _EXCLUDED_TOPICS.search(part)]
    text = "".join(kept).strip().rstrip("，,；;")
    if text and text[-1] not in "。！？!?":
        text += "。" if any(part.strip().endswith(("。", "！", "？")) for part in parts) else ""
    return text if len(norm(text)) >= 6 else ""


def is_accuracy_item(*texts: str) -> bool:   # 旧名字，留给第一版的调用方
    return is_excluded_item(*texts[:3])


def silence_criterion(threshold_sec: int) -> str:
    return (f"整场相邻两句话之间的空白都不到 {threshold_sec} 秒"
            f"（3 分钟以上的放歌试听、离场不算）")


# 上播弹窗里点「记住了」和接班几乎同时发生，录音时间轴记的接班时刻常比点的时刻早一两秒（2026-10-04 小陈 19:11 晚 1 秒）；
# 接班后这么短时间内点的，都算开播前确认
ACK_GRACE = timedelta(minutes=3)


def acked_before(acked_at: datetime | None, started: datetime | None) -> bool:
    """这一场开播（接班）前已确认：确认时刻早于开播时刻 + ACK_GRACE。"""
    return acked_at is not None and started is not None and acked_at < started + ACK_GRACE


def compute_streak(acked_at: datetime | None, history) -> int:
    """按场次先后重算「确认后连续做到」的场数：pass +1、fail 清零、na 不变。

    history：[(该场班次开始时间, verdict, baseline)]。只认确认（acked_at）之后开播、
    且当时不是基线的场次——动作被改过要重新确认，确认前的旧场次自然不再算数。
    """
    if acked_at is None:
        return 0
    streak = 0
    for started, verdict, baseline in sorted(
        history, key=lambda h: (h[0] is None, h[0] or datetime.min),
    ):
        if baseline or started is None or not acked_before(acked_at, started):
            continue
        if verdict == "pass":
            streak += 1
        elif verdict == "fail":
            streak = 0
    return streak


# ---------- 对外数据形状 ----------

def _streak_label(a: FocusAction) -> str:
    if a.status == "passed":
        return "已过关"
    if a.status != "active":
        return ""
    if a.acked_at is None:
        return "主播还没点「记住了」，这期间的核对只算基线，不计连续场数"
    remain = max(0, PASS_STREAK - (a.streak or 0))
    return f"已连续做到 {a.streak or 0} 场，再做到 {remain} 场就过关"


def needs_ack(a: FocusAction) -> bool:
    return a.status == "active" and a.acked_at is None


def action_out(a: FocusAction) -> dict:
    return {
        "id": a.id,
        "streamer_id": a.streamer_id,
        "needs_ack": needs_ack(a),
        "text": a.text,
        "trigger": a.trigger,
        "criterion": a.criterion,
        "example": a.example,
        "example_source": a.example_source,
        "kind": a.kind,
        "kind_label": KIND_LABELS.get(a.kind, a.kind),
        "threshold_sec": a.threshold_sec if a.kind == "silence" else None,
        "status": a.status,
        "status_label": STATUS_LABELS.get(a.status, a.status),
        "streak": a.streak or 0,
        "streak_label": _streak_label(a),
        "acked": a.acked_at is not None,
        "acked_at": iso(a.acked_at),
        "acked_by": a.acked_by or "",
        "created_by": a.created_by,
        "created_by_label": "AI 生成" if a.created_by == "ai" else "人工设定",
        "origin_session_id": a.origin_session_id,
        "created_at": iso(a.created_at),
        "passed_at": iso(a.passed_at),
        "ended_at": iso(a.ended_at),
        "updated_at": iso(a.updated_at),
        "updated_by": a.updated_by or "",
        "sort_order": a.sort_order,
        "category": a.category or OTHER,
        "reopened_from": a.reopened_from,
    }


def check_out(c: FocusCheck, action: FocusAction | None, *, full: bool = True) -> dict:
    metric = loads(c.metric, {})
    out = {
        "action_id": c.action_id,
        "text": action.text if action else "",
        "kind": action.kind if action else "",
        "verdict": c.verdict,
        "verdict_label": VERDICT_LABELS.get(c.verdict, c.verdict),
        "baseline": bool(c.baseline),
        "baseline_label": "基线（主播还没确认这条，不计入连续场数）" if c.baseline else "计入连续场数",
        "done": c.done,
        "total": c.total,
        "summary": metric.get("summary", ""),
        "reason": c.reason or "",
    }
    if full:
        out["metric"] = metric
        out["evidence"] = loads(c.evidence, [])
    return out


def run_out(run: FocusRun | None) -> dict | None:
    if run is None:
        return None
    return {
        "session_id": run.session_id,
        "streamer_id": run.streamer_id,
        "status": run.status,
        "status_label": RUN_LABELS.get(run.status, run.status),
        "error": run.error or "",
        "note": run.note or "",
        "new_count": run.new_count or 0,
        "turn_started_at": iso(run.turn_started_at),
        "date": run.turn_started_at.strftime("%Y-%m-%d") if run.turn_started_at else None,
        "created_at": iso(run.created_at),
        "finished_at": iso(run.finished_at),
    }


def streamer_out(s: Streamer) -> dict:
    return {"id": s.id, "name": s.name, "ext_id": s.ext_id,
            "deleted": s.deleted_at is not None}


def ordered_active(db: Session, streamer_id: int) -> list[FocusAction]:
    return db.execute(select(FocusAction).where(
        FocusAction.streamer_id == streamer_id, FocusAction.status == "active",
    ).order_by(FocusAction.sort_order, FocusAction.id)).scalars().all()


def _history(db: Session, streamer_id: int, limit: int, *, full: bool) -> list[dict]:
    runs = db.execute(select(FocusRun).where(FocusRun.streamer_id == streamer_id).order_by(
        FocusRun.turn_started_at.is_(None), FocusRun.turn_started_at.desc(), FocusRun.id.desc(),
    ).limit(limit)).scalars().all()
    if not runs:
        return []
    checks = db.execute(select(FocusCheck).where(
        FocusCheck.streamer_id == streamer_id,
        FocusCheck.session_id.in_([r.session_id for r in runs]),
    ).order_by(FocusCheck.id)).scalars().all()
    actions = {a.id: a for a in db.execute(select(FocusAction).where(
        FocusAction.id.in_({c.action_id for c in checks} or {0}))).scalars()}
    titles = dict(db.execute(select(ReplaySession.id, ReplaySession.title).where(
        ReplaySession.id.in_([r.session_id for r in runs]))).all())
    by_session: dict[int, list[dict]] = {}
    for c in checks:
        by_session.setdefault(c.session_id, []).append(
            check_out(c, actions.get(c.action_id), full=full))
    out = []
    for r in runs:
        item = run_out(r) or {}
        item["title"] = titles.get(r.session_id, "")
        item["checks"] = by_session.get(r.session_id, [])
        out.append(item)
    return out


# ---------- 读：给接口用 ----------

_RULES = {"pass_streak": PASS_STREAK,
          "note": "在练动作不限条数；主播点「记住了」之后开播的场次才计连续场数，连续 3 场做到算过关"}


def ack_summary(active: list[FocusAction]) -> dict:
    """在练几条、几条还没点「记住了」；all_acked = 在练的都点过了（没有在练动作时也是 true）。"""
    waiting = sum(1 for a in active if needs_ack(a))
    return {"active_count": len(active), "needs_ack_count": waiting,
            "all_acked": waiting == 0}


def streamer_view(db: Session, streamer: Streamer | None) -> dict:
    """某位主播：在练的动作、最近过关的、最近 10 场的核对历史。找不到主播时返回空结果。"""
    if streamer is None:
        return {"ok": True, "found": False, "streamer": None, "active": [], "passed": [],
                "history": [], **ack_summary([]), "rules": _RULES}
    active = ordered_active(db, streamer.id)
    passed = db.execute(select(FocusAction).where(
        FocusAction.streamer_id == streamer.id, FocusAction.status == "passed",
    ).order_by(FocusAction.passed_at.desc(), FocusAction.id.desc()).limit(10)).scalars().all()
    return {
        "ok": True,
        "found": True,
        "streamer": streamer_out(streamer),
        "active": [action_out(a) for a in active],
        **ack_summary(active),
        "passed": [action_out(a) for a in passed],
        "history": _history(db, streamer.id, 10, full=False),
        "rules": _RULES,
    }


def session_view(db: Session, session_id: int, streamer: Streamer | None) -> dict:
    """某场某位主播报告里的 focus 块；next 里每条附上这条动作现在的状态（确认了没有、还在不在练）。"""
    if streamer is None:
        return {"ok": True, "found": False, "session_id": session_id, "streamer": None,
                "available": False, "focus": None, "run": None}
    row = db.execute(select(Critique).where(
        Critique.session_id == session_id, Critique.streamer_id == streamer.id,
        Critique.level == CritiqueLevel.summary, Critique.category == "整场总结",
    ).order_by(Critique.id).limit(1)).scalars().first()
    focus = loads(row.data, {}).get("focus") if row else None
    if not isinstance(focus, dict):
        focus = None
    if focus:
        ids = [n.get("id") for n in focus.get("next") or [] if isinstance(n, dict)]
        now = {a.id: a for a in db.execute(select(FocusAction).where(
            FocusAction.id.in_([i for i in ids if isinstance(i, int)] or [0]))).scalars()}
        for item in focus.get("next") or []:
            a = now.get(item.get("id")) if isinstance(item, dict) else None
            if a is not None:
                item["current"] = {"status": a.status,
                                   "status_label": STATUS_LABELS.get(a.status, a.status),
                                   "needs_ack": needs_ack(a), "acked": a.acked_at is not None,
                                   "streak": a.streak or 0}
    run = db.execute(select(FocusRun).where(
        FocusRun.session_id == session_id, FocusRun.streamer_id == streamer.id,
    )).scalars().first()
    return {"ok": True, "found": True, "session_id": session_id,
            "streamer": streamer_out(streamer), "available": focus is not None,
            "focus": focus, "run": run_out(run)}


def overview(db: Session) -> dict:
    """看板/完成情况页：每位主播在练的动作 + 最近 5 场的核对情况。"""
    ids = set(db.execute(select(FocusAction.streamer_id).distinct()).scalars())
    ids |= set(db.execute(select(FocusRun.streamer_id).distinct()).scalars())
    streamers = db.execute(select(Streamer).where(Streamer.id.in_(ids or {0}))).scalars().all()
    passed_counts = dict(db.execute(select(FocusAction.streamer_id, func.count(FocusAction.id)).where(
        FocusAction.status == "passed").group_by(FocusAction.streamer_id)).all())
    out = []
    for s in sorted(streamers, key=lambda x: (x.deleted_at is not None, x.name or "", x.id)):
        active = ordered_active(db, s.id)
        out.append({
            "streamer": streamer_out(s),
            "streamer_id": s.id,
            "name": s.name,
            "ext_id": s.ext_id,
            "active": [action_out(a) for a in active],
            **ack_summary(active),
            "passed_count": int(passed_counts.get(s.id, 0)),
            "recent": _history(db, s.id, 5, full=False),
        })
    return {"ok": True, "generated_at": iso(datetime.now()), "streamers": out}


# ---------- 校验 ----------

def _text(value, field: str, limit: int, *, required: bool) -> str:
    if value is not None and not isinstance(value, (str, int, float)):
        raise FocusError(400, f"{field}格式不对，要填文字")
    text = " ".join(str(value if value is not None else "").split())
    if required and not text:
        raise FocusError(400, f"{field}不能为空")
    if len(text) > limit:
        raise FocusError(400, f"{field}太长了：{len(text)} 字，限 {limit} 字以内")
    return text


def clean_actor(value, *, default: str = "") -> str:
    actor = _text(value, "操作人 actor", ACTOR_LIMIT, required=False) or default
    if not actor:
        raise FocusError(400, "缺少操作人 actor（写明是谁做的这次操作）")
    return actor


def _threshold(value, default: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    elif isinstance(value, int) and not isinstance(value, bool):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    else:
        raise FocusError(400, "threshold_sec 要填整数秒")
    if not 5 <= number <= SILENCE_CAP_SEC - 10:
        raise FocusError(400, f"不冷场的秒数要在 5~{SILENCE_CAP_SEC - 10} 秒之间")
    return number


def clean_new_fields(payload: dict) -> dict:
    """新建一条动作（手动加 / 换成手填的）时的字段校验。"""
    kind = str(payload.get("kind") or "llm").strip()
    if kind not in KINDS:
        raise FocusError(400, "kind 只能是 llm（话术动作）或 silence（不冷场）")
    text = _text(payload.get("text"), "动作", TEXT_LIMIT, required=True)
    example = _text(payload.get("example"), "例句", EXAMPLE_LIMIT, required=False)
    if kind == "silence":
        threshold = _threshold(payload.get("threshold_sec"), DEFAULT_SILENCE_SEC)
        trigger = _text(payload.get("trigger"), "时机", TRIGGER_LIMIT, required=False) or "整场直播中"
        criterion = (_text(payload.get("criterion"), "做到的标准", CRITERION_LIMIT, required=False)
                     or silence_criterion(threshold))
    else:
        threshold = DEFAULT_SILENCE_SEC
        trigger = _text(payload.get("trigger"), "时机", TRIGGER_LIMIT, required=True)
        criterion = _text(payload.get("criterion"), "做到的标准", CRITERION_LIMIT, required=True)
    raw_category = str(payload.get("category") or "").strip()
    if raw_category and raw_category not in CATEGORIES:
        raise FocusError(400, "category 只能是：" + "、".join(CATEGORIES))
    return {"text": text, "trigger": trigger, "criterion": criterion, "example": example,
            "kind": kind, "threshold_sec": threshold,
            "category": normalize_category(raw_category, kind)}


def clean_generated(item) -> dict | None:
    """模型生成的一条动作：不合格就丢掉（不报错），合格的整理成入库字段。"""
    if not isinstance(item, dict):
        return None
    text = " ".join(str(item.get("text") or "").split()).rstrip("。.;；")
    trigger = " ".join(str(item.get("trigger") or "").split())
    criterion = " ".join(str(item.get("criterion") or "").split())
    example = " ".join(str(item.get("example") or "").split())
    kind = "silence" if str(item.get("kind") or "").strip().lower() == "silence" else "llm"
    if not text or len(text) > TEXT_LIMIT:
        return None
    if is_excluded_item(text):                 # 动作本身就是不该出的话题：整条不要
        return None
    trigger, criterion = clean_clauses(trigger), clean_clauses(criterion)
    if is_excluded_item(text, trigger, criterion):
        return None
    threshold = DEFAULT_SILENCE_SEC
    if kind == "silence":
        try:
            threshold = int(float(item.get("threshold_sec") or 60))
        except (TypeError, ValueError):
            threshold = 60
        threshold = min(150, max(10, threshold))
        trigger = trigger[:TRIGGER_LIMIT] or "整场直播中"
        criterion = silence_criterion(threshold)
    else:
        if not trigger or len(norm(criterion)) < 6:
            return None
        if len(trigger) > TRIGGER_LIMIT or len(criterion) > CRITERION_LIMIT:
            return None
    example = clean_example(example)
    return {"text": text, "trigger": trigger, "criterion": criterion,
            "example": clip(example, EXAMPLE_LIMIT) if example else "",
            "example_source": "AI 示范" if example else "", "kind": kind, "threshold_sec": threshold,
            "category": normalize_category(item.get("category"), kind)}


def _field(other, name: str):
    return other.get(name) if isinstance(other, dict) else getattr(other, name, None)


def same_category(item: dict, others):
    """和 item 同一类（「其他」不算）的第一条；冷场类按 kind 认。"""
    category = normalize_category(item.get("category"), item.get("kind") or "llm")
    if category == OTHER:
        return None
    for other in others:
        o_category = normalize_category(_field(other, "category"), _field(other, "kind") or "llm")
        if o_category == category:
            return other
    return None


def is_rebuild_removed(a) -> bool:
    return _field(a, "status") == "removed" and _field(a, "updated_by") == REBUILD_MARK


def duplicate_of(item: dict, others) -> FocusAction | dict | None:
    """item 和 others（动作行或字段 dict）里哪一条算重复；silence 同秒数算重复。"""
    for other in others:
        o_kind = getattr(other, "kind", None) if not isinstance(other, dict) else other.get("kind")
        o_text = getattr(other, "text", None) if not isinstance(other, dict) else other.get("text")
        o_thr = (getattr(other, "threshold_sec", None) if not isinstance(other, dict)
                 else other.get("threshold_sec"))
        if item["kind"] == "silence" and o_kind == "silence" and o_thr == item["threshold_sec"]:
            return other
        if similar(item["text"], o_text or ""):
            return other
    return None


# ---------- 写：给接口用（都在 WRITE_LOCK 里、各自一个事务） ----------

def _get_action(db: Session, action_id: int, streamer_id: int | None) -> FocusAction:
    a = db.get(FocusAction, action_id)
    if a is None:
        raise FocusError(404, "改进动作不存在")
    if streamer_id is not None and a.streamer_id != streamer_id:
        raise FocusError(403, "这条动作不是这位主播的")
    return a


def _require_active(a: FocusAction) -> None:
    if a.status != "active":
        raise FocusError(409, f"这条动作已经{STATUS_LABELS.get(a.status, a.status)}，不能再改")


def _next_sort(db: Session, streamer_id: int) -> int:
    value = db.execute(select(func.max(FocusAction.sort_order)).where(
        FocusAction.streamer_id == streamer_id)).scalar()
    return int(value or 0) + 1


def _active_for_dedupe(db: Session, streamer_id: int, *, exclude: int | None = None):
    """查重只对在练的：已过关的同一问题又犯了，可以重新加一条。"""
    rows = ordered_active(db, streamer_id)
    return [a for a in rows if a.id != exclude]


def _candidate(db: Session, streamer_id: int, index, session_id) -> tuple[FocusRun, dict]:
    """取候选：默认这位主播最近一场有候选的处理记录；也可指定 session_id。"""
    if isinstance(index, bool) or not isinstance(index, int):
        raise FocusError(400, "candidate_index 要填整数（候选列表里的 index）")
    query = select(FocusRun).where(FocusRun.streamer_id == streamer_id)
    if session_id is not None:
        if isinstance(session_id, bool) or not isinstance(session_id, int):
            raise FocusError(400, "session_id 要填整数")
        query = query.where(FocusRun.session_id == session_id)
    runs = db.execute(query.order_by(
        FocusRun.turn_started_at.is_(None), FocusRun.turn_started_at.desc(), FocusRun.id.desc(),
    )).scalars().all()
    for run in runs:
        items = loads(run.candidates, [])
        if not items:
            continue
        for item in items:
            if isinstance(item, dict) and item.get("index") == index:
                if item.get("used_by"):
                    raise FocusError(409, "这条候选已经用过了")
                if not item.get("usable"):
                    raise FocusError(400, "这条候选没有完整的时机和做到的标准，不能直接用；"
                                          "可以把它改写后手填新增")
                return run, item
        raise FocusError(404, f"候选里没有第 {index} 条")
    raise FocusError(404, "这位主播还没有候选动作")


def _mark_candidate_used(run: FocusRun, index: int, action_id: int) -> None:
    items = loads(run.candidates, [])
    for item in items:
        if isinstance(item, dict) and item.get("index") == index:
            item["used_by"] = action_id
    run.candidates = dumps(items)


def _new_fields(db: Session, streamer_id: int, payload: dict):
    """手填字段或候选 → (入库字段, 候选所在的处理记录, 候选 index)。"""
    if payload.get("candidate_index") is not None:
        run, item = _candidate(db, streamer_id, payload.get("candidate_index"),
                               payload.get("session_id"))
        fields = clean_new_fields(item)
        fields["example_source"] = item.get("example_source") or "AI 示范"
        return fields, run, item.get("index")
    fields = clean_new_fields(payload)
    fields["example_source"] = ""
    return fields, None, None


def _assert_not_duplicate(db: Session, streamer_id: int, fields: dict, *, exclude: int | None) -> None:
    known = _active_for_dedupe(db, streamer_id, exclude=exclude)
    dup = duplicate_of(fields, known)
    if dup is not None:
        raise FocusError(409, f"和在练的动作「{dup.text}」是同一个问题")
    peer = same_category(fields, known)
    if peer is not None:
        raise FocusError(409, f"已经有一条在练的「{fields['category']}」类动作「{peer.text}」，"
                              "同一类只留一条，可以改写那条或把它换掉")
    if fields["kind"] == "silence" and any(a.kind == "silence" for a in known):
        raise FocusError(409, "已经有一条在练的不冷场动作了，同时只练一条")


def _owner_out(db: Session, streamer_id: int) -> dict | None:
    s = db.get(Streamer, streamer_id)
    return streamer_out(s) if s is not None else None


def ack_action(action_id: int, owner_id: int, actor: str | None = None) -> dict:
    """主播点「记住了」：只能确认自己的在练动作；重复点不改第一次的时间和人。"""
    with WRITE_LOCK, SessionLocal() as db:
        a = _get_action(db, action_id, owner_id)
        if a.status != "active":
            raise FocusError(409, f"这条动作已经{STATUS_LABELS.get(a.status, a.status)}，不用再确认")
        changed = a.acked_at is None
        if changed:
            owner = db.get(Streamer, a.streamer_id)
            a.acked_at = datetime.now()
            a.acked_by = actor or (owner.name if owner else "") or "主播"
            db.commit()
        active = ordered_active(db, a.streamer_id)
        return {"ok": True, "changed": changed, "action": action_out(a),
                "streamer": _owner_out(db, a.streamer_id), **ack_summary(active)}


def edit_action(action_id: int, payload: dict, actor: str, owner_id: int | None = None) -> dict:
    with WRITE_LOCK, SessionLocal() as db:
        a = _get_action(db, action_id, owner_id)
        _require_active(a)
        changes: dict = {}
        if "text" in payload:
            changes["text"] = _text(payload.get("text"), "动作", TEXT_LIMIT, required=True)
        if "trigger" in payload:
            changes["trigger"] = _text(payload.get("trigger"), "时机", TRIGGER_LIMIT,
                                       required=a.kind == "llm")
        if "criterion" in payload:
            changes["criterion"] = _text(payload.get("criterion"), "做到的标准", CRITERION_LIMIT,
                                         required=a.kind == "llm")
        if "example" in payload:
            changes["example"] = _text(payload.get("example"), "例句", EXAMPLE_LIMIT, required=False)
        if "threshold_sec" in payload:
            if a.kind != "silence":
                raise FocusError(400, "只有不冷场动作才有秒数")
            changes["threshold_sec"] = _threshold(payload.get("threshold_sec"), a.threshold_sec)
        if not changes:
            raise FocusError(400, "没有要改的内容（可改 text / trigger / criterion / example）")
        changes = {k: v for k, v in changes.items() if getattr(a, k) != v}
        if not changes:
            return {"ok": True, "changed": False, "action": action_out(a),
                    "streamer": _owner_out(db, a.streamer_id)}
        if "text" in changes:
            dup = duplicate_of({"text": changes["text"], "kind": "llm", "threshold_sec": None},
                               _active_for_dedupe(db, a.streamer_id, exclude=a.id))
            if dup is not None:
                raise FocusError(409, f"和在练的动作「{dup.text}」是同一个问题")
        for key, value in changes.items():
            setattr(a, key, value)
        if a.kind == "silence":
            a.trigger = a.trigger or "整场直播中"
            if not a.criterion or ("threshold_sec" in changes and "criterion" not in changes):
                a.criterion = silence_criterion(a.threshold_sec)
        if "example" in changes:
            a.example_source = actor
        # 改过的动作要主播重新确认；连续场数也从头算（之前核对的是旧说法）
        a.acked_at = None
        a.acked_by = ""
        a.streak = 0
        a.updated_at = datetime.now()
        a.updated_by = actor
        db.commit()
        return {"ok": True, "changed": True, "action": action_out(a),
                "streamer": _owner_out(db, a.streamer_id)}


def replace_action(action_id: int, payload: dict, actor: str, owner_id: int | None = None) -> dict:
    """换掉一条在练动作：旧的标 replaced（id 保留），新的另起一个 id、等主播重新确认。"""
    with WRITE_LOCK, SessionLocal() as db:
        old = _get_action(db, action_id, owner_id)
        _require_active(old)
        fields, run, index = _new_fields(db, old.streamer_id, payload)
        _assert_not_duplicate(db, old.streamer_id, fields, exclude=old.id)
        now = datetime.now()
        old.status = "replaced"
        old.ended_at = now
        old.updated_at = now
        old.updated_by = actor
        new = FocusAction(
            streamer_id=old.streamer_id, created_by=actor, created_at=now,
            origin_session_id=run.session_id if run is not None else None,
            sort_order=old.sort_order, status="active", streak=0, **fields,
        )
        db.add(new)
        db.flush()
        if run is not None:
            _mark_candidate_used(run, index, new.id)
        db.commit()
        return {"ok": True, "replaced": action_out(old), "action": action_out(new),
                "streamer": _owner_out(db, old.streamer_id)}


def remove_action(action_id: int, actor: str, owner_id: int | None = None) -> dict:
    with WRITE_LOCK, SessionLocal() as db:
        a = _get_action(db, action_id, owner_id)
        _require_active(a)
        now = datetime.now()
        a.status = "removed"
        a.ended_at = now
        a.updated_at = now
        a.updated_by = actor
        db.commit()
        return {"ok": True, "action": action_out(a), "streamer": _owner_out(db, a.streamer_id)}


def add_action(streamer_id: int, payload: dict, actor: str) -> dict:
    """手动加一条在练动作（手填，或从某场的候选里挑 candidate_index）；不限条数，只查重。"""
    with WRITE_LOCK, SessionLocal() as db:
        if db.get(Streamer, streamer_id) is None:
            raise FocusError(404, "复盘花名册中未找到该主播")
        fields, run, index = _new_fields(db, streamer_id, payload)
        _assert_not_duplicate(db, streamer_id, fields, exclude=None)
        new = FocusAction(
            streamer_id=streamer_id, created_by=actor, created_at=datetime.now(),
            origin_session_id=run.session_id if run is not None else None,
            sort_order=_next_sort(db, streamer_id), status="active", streak=0, **fields,
        )
        db.add(new)
        db.flush()
        if run is not None:
            _mark_candidate_used(run, index, new.id)
        db.commit()
        return {"ok": True, "action": action_out(new), "streamer": _owner_out(db, streamer_id),
                **ack_summary(ordered_active(db, streamer_id))}
