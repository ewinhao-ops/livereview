"""知识库向量化与检索（关键词粗筛 + 语义精排）。

Embedding 走百炼 OpenAI 兼容 /embeddings（复用 ALIYUN_DASHSCOPE_API_KEY）。
检索：先用 query 里的词在 chunk.text 子串命中做粗筛，候选不足则取全部；再用
query 向量与各 chunk 向量算余弦，关键词命中加权，"提炼版"再加权，返回 top_k。
无 key / 出错时回退纯关键词检索，且【记录状态】供前端感知（不再无声降级）。
"""

from __future__ import annotations

import json
import math
import re

import httpx
from sqlalchemy import select

from ..config import get_settings
from ..db import SessionLocal
from ..models import ExperienceTip, KnowledgeChunk, RhythmTemplate
from .safe_error import safe_error_text, safe_exception_code

_EMB_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
_EMB_MODEL = "text-embedding-v3"
_TOKEN_SPAN_RE = re.compile(r"[A-Za-z0-9]{2,}|[一-鿿]{2,}")
_MAX_KEYWORD_TOKENS = 300
_QUERY_VECTOR_UNSET = object()

# 可观测状态：让前端/接口知道语义检索到底有没有在工作（避免"以为在用其实退化成关键词"）
_emb_state: dict = {"configured": False, "last_ok": None, "last_error": ""}


def embedding_health() -> dict:
    s = get_settings()
    _emb_state["configured"] = bool(s.aliyun_dashscope_api_key)
    return dict(_emb_state)


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """批量向量化；无 key 返回 []（mock，正常）；真实失败也返回 [] 但记录 last_error。"""
    s = get_settings()
    key = s.aliyun_dashscope_api_key
    _emb_state["configured"] = bool(key)
    if not key or not texts:
        return []
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    out: list[list[float]] = []
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            # 百炼单次最多 10 条，分批
            for i in range(0, len(texts), 10):
                batch = texts[i:i + 10]
                r = await c.post(_EMB_URL, headers=headers,
                                 json={"model": _EMB_MODEL, "input": batch})
                r.raise_for_status()
                data = sorted(r.json()["data"], key=lambda d: d["index"])
                out.extend(d["embedding"] for d in data)
        _emb_state["last_ok"] = True
        _emb_state["last_error"] = ""
        return out
    except Exception as e:  # noqa: BLE001
        _emb_state["last_ok"] = False
        _emb_state["last_error"] = "向量服务暂时不可用"
        print(
            "[embed] status=failed; "
            f"code={safe_exception_code(e, prefix='EMBED')}; "
            f"error={safe_error_text(e)}"
        )
        return []


async def embed_one(text: str) -> list[float] | None:
    vs = await embed_texts([text])
    return vs[0] if vs else None


def _evenly_sample(items: list[str], limit: int) -> list[str]:
    """确定性地覆盖整个序列，避免长文本只保留开头 token。"""
    if len(items) <= limit:
        return items
    if limit <= 1:
        return [items[len(items) // 2]] if limit == 1 else []
    last = len(items) - 1
    return [items[index * last // (limit - 1)] for index in range(limit)]


def _tokens(text: str) -> list[str]:
    """英文按词、中文按 2/3-gram，支持自然问句与知识片段的局部重合。"""
    tokens: list[str] = []
    for match in _TOKEN_SPAN_RE.finditer(text or ""):
        span = match.group(0)
        if span.isascii():
            tokens.append(span.lower())
            continue
        if len(span) <= 3:
            tokens.append(span)
        # 同一中文段按文本位置交错产生 2/3-gram，便于长文本等距取样。
        for index in range(len(span)):
            for width in (2, 3):
                if index + width <= len(span):
                    tokens.append(span[index:index + width])
    unique = list(dict.fromkeys(tokens))
    return _evenly_sample(unique, _MAX_KEYWORD_TOKENS)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


async def retrieve_detailed(
    query: str,
    top_k: int = 5,
    scene: str = "",
    category: str = "",
    exclude_category: str = "",
    exclude_categories: tuple[str, ...] = (),
    query_vector: list[float] | None | object = _QUERY_VECTOR_UNSET,
) -> list[dict]:
    """返回最相关的知识库片段（带来源/分类/场景/置信/是否语义命中），供点评标注出处。

    scene 非空时，属于该话术场景的切片额外加权（分场景检索，soft boost 不硬过滤，
    避免该场景知识为空时召回不到东西）。
    """
    with SessionLocal() as db:
        rows = db.execute(select(KnowledgeChunk)).scalars().all()
        # “优秀节奏/本店经验”是审核权限边界，必须验证结构化来源，不能只信可伪造的 category 文本。
        if category == "优秀节奏":
            adopted_ids = set(db.execute(select(RhythmTemplate.id).where(
                RhythmTemplate.status == "adopted"
            )).scalars().all())
            rows = [r for r in rows
                    if r.origin_type == "rhythm_template" and r.origin_id in adopted_ids]
        elif category == "本店经验":
            adopted_tips = db.execute(select(ExperienceTip.id, ExperienceTip.chunk_id).where(
                ExperienceTip.status == "adopted"
            )).all()
            tip_ids = {row.id for row in adopted_tips}
            legacy_chunk_ids = {row.chunk_id for row in adopted_tips if row.chunk_id is not None}
            rows = [r for r in rows if (
                (r.origin_type == "experience_tip" and r.origin_id in tip_ids)
                or r.id in legacy_chunk_ids
            )]
    if category:
        rows = [r for r in rows if getattr(r, "category", "") == category]
    excluded = set(exclude_categories)
    if exclude_category:
        excluded.add(exclude_category)
    if excluded:
        rows = [r for r in rows if getattr(r, "category", "") not in excluded]
    if not rows:
        return []

    q_tokens = set(_tokens(query))

    def kw_hits(text: str) -> int:
        low = (text or "").lower()
        return sum(1 for token in q_tokens if token in low)

    # 关键词粗筛。无向量能力时只返回真实关键词/同场景命中，不用全表随机片段补位。
    scored_kw = [(r, kw_hits(r.text)) for r in rows]
    candidates = [r for r, h in scored_kw if h > 0]
    qv = await embed_one(query) if query_vector is _QUERY_VECTOR_UNSET else query_vector
    if qv:
        # 无索引切片只能凭真实关键词/同场景进入候选，不能靠 distilled 固定加权随机补位。
        candidates = [r for r in rows if r.embedding or kw_hits(r.text) > 0
                      or (scene and scene != "general" and r.scene == scene)]
    elif scene and scene != "general":
        have = {id(r) for r in candidates}
        candidates += [r for r in rows
                       if getattr(r, "scene", "") == scene and id(r) not in have]
    if not candidates:
        return []
    ranked: list[tuple[float, bool, KnowledgeChunk]] = []
    for r in candidates:
        kw = kw_hits(r.text)
        same_scene = bool(scene and scene != "general" and getattr(r, "scene", "") == scene)
        sim = 0.0
        semantic = False
        if qv and r.embedding:
            try:
                sim = _cosine(qv, json.loads(r.embedding))
                semantic = True
            except Exception:  # noqa: BLE001
                sim = 0.0
                semantic = False
        if semantic and sim < 0.2 and kw == 0 and not same_scene:
            continue
        if not semantic and kw == 0 and not same_scene:
            continue
        boost = 0.15 if (getattr(r, "kind", "doc") == "distilled") else 0.0  # 提炼版优先
        if same_scene:  # 同场景加权（general 兜底桶不参与）
            boost += 0.2
        score = sim + 0.1 * kw + boost
        ranked.append((score, semantic, r))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return [
        {"text": r.text, "source": r.source, "kind": getattr(r, "kind", "doc"),
         "category": getattr(r, "category", ""), "scene": getattr(r, "scene", ""),
         "origin_type": getattr(r, "origin_type", ""), "origin_id": getattr(r, "origin_id", None),
         "score": round(sc, 3), "semantic": sem}
        for sc, sem, r in ranked[:top_k]
    ]


async def retrieve(query: str, top_k: int = 5) -> list[str]:
    """兼容旧调用：只返回文本列表。"""
    return [d["text"] for d in await retrieve_detailed(query, top_k)]
