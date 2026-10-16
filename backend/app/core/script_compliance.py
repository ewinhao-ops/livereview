"""Prescribed-script planning and whole-session compliance analysis.

The analyzer uses character similarity only as a local candidate filter. Final
similarity always comes from the configured DashScope text-embedding-v3
service; an unavailable embedding service is an explicit failure.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher
import hashlib
import json
import logging
import math
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from ..db import SessionLocal
from ..models import (
    ReplaySession,
    SessionStatus,
    ScriptComplianceOccurrence,
    ScriptComplianceResult,
    ScriptFrequencyRule,
    SessionScriptCompliancePlan,
    Streamer,
    StreamerTurn,
    Transcript,
)
from .embed import embed_texts
from .safe_error import safe_exception_code


logger = logging.getLogger(__name__)
EmbeddingFunction = Callable[[list[str]], Awaitable[list[list[float]]]]
RESULT_STATUSES = {
    "matched", "late", "too_early", "too_short", "too_long",
    "low_similarity", "missing", "not_analyzable",
}
ANALYSIS_LEASE_MINUTES = 30


class ScriptRuleInUse(RuntimeError):
    pass


class ScriptRuleNotEligible(RuntimeError):
    pass


class ScriptPlanConflict(RuntimeError):
    pass


class ScriptAnalysisInProgress(RuntimeError):
    pass


class ScriptAnalysisSuperseded(RuntimeError):
    pass


class ScriptNotAnalyzable(RuntimeError):
    pass


class EmbeddingUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class _Piece:
    id: int
    start_ms: int
    end_ms: int
    text: str


@dataclass
class _Candidate:
    start_index: int
    end_index: int
    start_ms: int
    end_ms: int
    transcript_ids: list[int]
    text: str
    char_similarity: float
    semantic_similarity: float = 0.0


def _json_loads(raw: str, fallback):  # noqa: ANN001
    try:
        value = json.loads(raw or "")
    except (TypeError, json.JSONDecodeError):
        return fallback
    return value


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _scope(rule: ScriptFrequencyRule) -> dict:
    value = _json_loads(rule.effective_scope, {})
    return value if isinstance(value, dict) else {"type": "all"}


def serialize_rule(rule: ScriptFrequencyRule) -> dict:
    return {
        "id": rule.id,
        "name": rule.name,
        "standard_script": rule.standard_script,
        "repeat_interval_sec": rule.repeat_interval_sec,
        "target_duration_sec": rule.target_duration_sec,
        "interval_tolerance_sec": rule.interval_tolerance_sec,
        "duration_tolerance_sec": rule.duration_tolerance_sec,
        "similarity_threshold": rule.similarity_threshold,
        "effective_scope": _scope(rule),
        "enabled": rule.enabled,
        "version": rule.version,
        "created_at": _iso(rule.created_at),
        "updated_at": _iso(rule.updated_at),
    }


def _snapshot(rule: ScriptFrequencyRule) -> dict:
    # This is deliberately a full copy rather than a reference to mutable rule data.
    return serialize_rule(rule)


def create_rule(values: dict) -> dict:
    now = datetime.now()
    scope = values.get("effective_scope") or {"type": "all"}
    row = ScriptFrequencyRule(
        name=values["name"].strip(),
        standard_script=values["standard_script"].strip(),
        repeat_interval_sec=values["repeat_interval_sec"],
        target_duration_sec=values["target_duration_sec"],
        interval_tolerance_sec=values.get("interval_tolerance_sec", 0),
        duration_tolerance_sec=values.get("duration_tolerance_sec", 0),
        similarity_threshold=values["similarity_threshold"],
        effective_scope=json.dumps(scope, ensure_ascii=False, separators=(",", ":")),
        enabled=values.get("enabled", True),
        version=1,
        created_at=now,
        updated_at=now,
    )
    with SessionLocal() as db:
        db.add(row)
        db.commit()
        db.refresh(row)
        return serialize_rule(row)


def list_rules(*, enabled: bool | None = None) -> list[dict]:
    with SessionLocal() as db:
        query = select(ScriptFrequencyRule)
        if enabled is not None:
            query = query.where(ScriptFrequencyRule.enabled.is_(enabled))
        rows = db.execute(query.order_by(ScriptFrequencyRule.id)).scalars().all()
        return [serialize_rule(row) for row in rows]


def list_rule_versions(rule_id: int) -> list[dict]:
    """Return the current version plus immutable versions already used by sessions."""
    with SessionLocal() as db:
        rule = db.get(ScriptFrequencyRule, rule_id)
        if rule is None:
            raise LookupError("规定话术规则不存在")
        versions = {rule.version: {"version": rule.version, "snapshot": _snapshot(rule),
                                   "created_at": _iso(rule.updated_at), "source": "current"}}
        plans = db.execute(select(SessionScriptCompliancePlan).where(
            SessionScriptCompliancePlan.rule_id == rule_id
        ).order_by(SessionScriptCompliancePlan.rule_version.desc())).scalars().all()
        for plan in plans:
            versions.setdefault(plan.rule_version, {
                "version": plan.rule_version,
                "snapshot": _json_loads(plan.rule_snapshot, {}),
                "created_at": _iso(plan.created_at),
                "source": "session_snapshot",
            })
        return [versions[key] for key in sorted(versions, reverse=True)]


def patch_rule(rule_id: int, values: dict) -> dict:
    editable = {
        "name", "standard_script", "repeat_interval_sec", "target_duration_sec",
        "interval_tolerance_sec", "duration_tolerance_sec", "similarity_threshold",
        "effective_scope", "enabled",
    }
    changes = {key: value for key, value in values.items() if key in editable}
    with SessionLocal() as db:
        row = db.get(ScriptFrequencyRule, rule_id)
        if row is None:
            raise LookupError("规定话术规则不存在")
        changed = False
        for key, value in changes.items():
            if key in {"name", "standard_script"}:
                value = value.strip()
            column = "effective_scope" if key == "effective_scope" else key
            if key == "effective_scope":
                value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            if getattr(row, column) != value:
                setattr(row, column, value)
                changed = True
        if changed:
            row.version += 1
            row.updated_at = datetime.now()
            db.commit()
            db.refresh(row)
        return serialize_rule(row)


def delete_rule(rule_id: int) -> None:
    with SessionLocal() as db:
        row = db.get(ScriptFrequencyRule, rule_id)
        if row is None:
            raise LookupError("规定话术规则不存在")
        used = db.execute(select(SessionScriptCompliancePlan.id).where(
            SessionScriptCompliancePlan.rule_id == rule_id,
        ).limit(1)).scalar_one_or_none()
        if used is not None:
            raise ScriptRuleInUse("该规则已被场次使用，只能停用，不能物理删除")
        db.delete(row)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise ScriptRuleInUse("该规则已被场次使用，只能停用，不能物理删除") from exc


def _session_lineup(session: ReplaySession) -> list[int]:
    value = _json_loads(session.lineup, [])
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, int) and item > 0]


def _ensure_session_streamer(db, session_id: int, streamer_id: int) -> ReplaySession:  # noqa: ANN001
    session = db.get(ReplaySession, session_id)
    if session is None:
        raise LookupError("场次不存在")
    if db.get(Streamer, streamer_id) is None:
        raise LookupError("主播不存在")
    related = streamer_id in _session_lineup(session) or session.current_streamer_id == streamer_id
    # The client may auto-save a draft before the operator finishes choosing
    # the lineup. A plan is still allowed while the session is mutable; the
    # start request writes the final lineup immediately afterwards.
    if not related and session.status in {
        SessionStatus.created, SessionStatus.parsing, SessionStatus.error,
    }:
        related = True
    if not related:
        related = db.execute(select(StreamerTurn.id).where(
            StreamerTurn.session_id == session_id,
            StreamerTurn.streamer_id == streamer_id,
        ).limit(1)).scalar_one_or_none() is not None
    if not related:
        related = db.execute(select(Transcript.id).where(
            Transcript.session_id == session_id,
            Transcript.streamer_id == streamer_id,
        ).limit(1)).scalar_one_or_none() is not None
    if not related:
        raise ValueError("该主播不属于此场直播")
    return session


def _scope_applies(rule: ScriptFrequencyRule, session: ReplaySession, streamer_id: int) -> bool:
    scope = _scope(rule)
    scope_type = str(scope.get("type") or "all").lower()
    if scope_type == "all":
        return True
    if scope_type == "streamers":
        return streamer_id in scope.get("streamer_ids", [])
    if scope_type == "sessions":
        return session.id in scope.get("session_ids", [])
    if scope_type == "rooms":
        return session.room_id in scope.get("room_ids", [])
    return False


def serialize_plan(row: SessionScriptCompliancePlan) -> dict:
    snapshot = _json_loads(row.rule_snapshot, {})
    return {
        "id": row.id,
        "session_id": row.session_id,
        "streamer_id": row.streamer_id,
        "rule_id": row.rule_id,
        "rule_version": row.rule_version,
        "rule_snapshot": snapshot if isinstance(snapshot, dict) else {},
        "created_at": _iso(row.created_at),
    }


def assign_rules(session_id: int, streamer_id: int, rule_ids: list[int]) -> list[dict]:
    rule_ids = list(dict.fromkeys(rule_ids))
    if not rule_ids:
        raise ValueError("至少选择一条规定话术规则")
    with SessionLocal() as db:
        session = _ensure_session_streamer(db, session_id, streamer_id)
        assigned: list[SessionScriptCompliancePlan] = []
        for rule_id in rule_ids:
            existing = db.execute(select(SessionScriptCompliancePlan).where(
                SessionScriptCompliancePlan.session_id == session_id,
                SessionScriptCompliancePlan.streamer_id == streamer_id,
                SessionScriptCompliancePlan.rule_id == rule_id,
            )).scalar_one_or_none()
            if existing is not None:
                assigned.append(existing)
                continue
            rule = db.get(ScriptFrequencyRule, rule_id)
            if rule is None:
                raise LookupError(f"规定话术规则 {rule_id} 不存在")
            if not rule.enabled:
                raise ScriptRuleNotEligible("停用规则不能分配给新场次")
            if not _scope_applies(rule, session, streamer_id):
                raise ScriptRuleNotEligible("规则生效范围不包含该场次或主播")
            row = SessionScriptCompliancePlan(
                session_id=session_id,
                streamer_id=streamer_id,
                rule_id=rule.id,
                rule_version=rule.version,
                rule_snapshot=json.dumps(_snapshot(rule), ensure_ascii=False),
            )
            try:
                with db.begin_nested():
                    db.add(row)
                    db.flush()
                assigned.append(row)
            except IntegrityError:
                existing = db.execute(select(SessionScriptCompliancePlan).where(
                    SessionScriptCompliancePlan.session_id == session_id,
                    SessionScriptCompliancePlan.streamer_id == streamer_id,
                    SessionScriptCompliancePlan.rule_id == rule_id,
                )).scalar_one_or_none()
                if existing is None:
                    raise ScriptPlanConflict("场次规则分配冲突，请重试")
                assigned.append(existing)
        db.commit()
        return [serialize_plan(row) for row in assigned]


def list_plans(session_id: int, *, streamer_id: int | None = None) -> list[dict]:
    with SessionLocal() as db:
        if db.get(ReplaySession, session_id) is None:
            raise LookupError("场次不存在")
        query = select(SessionScriptCompliancePlan).where(
            SessionScriptCompliancePlan.session_id == session_id,
        )
        if streamer_id is not None:
            query = query.where(SessionScriptCompliancePlan.streamer_id == streamer_id)
        rows = db.execute(query.order_by(
            SessionScriptCompliancePlan.streamer_id,
            SessionScriptCompliancePlan.id,
        )).scalars().all()
        return [serialize_plan(row) for row in rows]


def _normalise_text(value: str) -> str:
    return re.sub(r"[\W_]+", "", (value or "").lower(), flags=re.UNICODE)


def _character_similarity(target: str, candidate: str) -> float:
    target = _normalise_text(target)
    candidate = _normalise_text(candidate)
    if not target or not candidate:
        return 0.0
    if len(candidate) <= round(len(target) * 1.35):
        return SequenceMatcher(None, target, candidate, autojunk=False).ratio()
    width = len(target)
    stride = max(1, width // 6)
    starts = list(range(0, max(1, len(candidate) - width + 1), stride))
    last = max(0, len(candidate) - width)
    if last not in starts:
        starts.append(last)
    return max(
        SequenceMatcher(None, target, candidate[start:start + width], autojunk=False).ratio()
        for start in starts
    )


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return max(0.0, min(1.0, dot / (left_norm * right_norm)))


def _pieces(rows: Iterable[Transcript]) -> list[_Piece]:
    pieces: list[_Piece] = []
    for row in rows:
        content = (row.corrected_text or row.raw_text or "").strip()
        if not content:
            continue
        pieces.append(_Piece(
            id=row.id,
            start_ms=max(0, row.start_ms),
            end_ms=max(row.start_ms, row.end_ms),
            text=content,
        ))
    return sorted(pieces, key=lambda item: (item.start_ms, item.id))


def _even_sample(items: list[_Candidate], limit: int) -> list[_Candidate]:
    if len(items) <= limit:
        return items
    if limit == 1:
        return [items[len(items) // 2]]
    last = len(items) - 1
    return [items[index * last // (limit - 1)] for index in range(limit)]


def _character_candidates(
    pieces: list[_Piece], snapshot: dict, *, limit: int = 400,
) -> list[_Candidate]:
    standard = str(snapshot.get("standard_script") or "")
    target = _normalise_text(standard)
    if not target:
        raise ValueError("规则快照中的标准话术为空")
    threshold = float(snapshot.get("similarity_threshold") or 0)
    char_floor = max(0.22, min(0.65, threshold * 0.65))
    # A candidate must contain most of the prescribed wording. This prevents
    # two adjacent halves of one execution from being counted as two runs.
    min_chars = max(2, round(len(target) * 0.55))
    max_chars = max(len(target) + 40, round(len(target) * 2.5))
    max_gap_ms = max(5_000, int(snapshot.get("target_duration_sec") or 1) * 500)
    candidates: list[_Candidate] = []
    for start in range(len(pieces)):
        text_parts: list[str] = []
        ids: list[int] = []
        per_start: list[_Candidate] = []
        previous_end = pieces[start].start_ms
        for end in range(start, min(len(pieces), start + 80)):
            piece = pieces[end]
            if end > start and piece.start_ms - previous_end > max_gap_ms:
                break
            text_parts.append(piece.text)
            ids.append(piece.id)
            previous_end = max(previous_end, piece.end_ms)
            candidate_text = " ".join(text_parts)
            normalised_length = len(_normalise_text(candidate_text))
            if normalised_length >= min_chars:
                char_similarity = _character_similarity(standard, candidate_text)
                if char_similarity >= char_floor:
                    per_start.append(_Candidate(
                        start_index=start,
                        end_index=end,
                        start_ms=pieces[start].start_ms,
                        end_ms=max(piece.end_ms, pieces[start].start_ms),
                        transcript_ids=list(ids),
                        text=candidate_text,
                        char_similarity=char_similarity,
                    ))
            if normalised_length > max_chars and end > start:
                break
        per_start.sort(key=lambda item: (
            item.char_similarity,
            -abs(len(_normalise_text(item.text)) - len(target)),
        ), reverse=True)
        candidates.extend(per_start[:3])
    candidates.sort(key=lambda item: (item.start_index, item.end_index))
    return _even_sample(candidates, limit)


def _weighted_non_overlapping(
    candidates: list[_Candidate], snapshot: dict,
) -> list[_Candidate]:
    if not candidates:
        return []
    threshold = float(snapshot["similarity_threshold"])
    target_duration = max(0.1, float(snapshot["target_duration_sec"]))
    useful = [candidate for candidate in candidates if (
        candidate.semantic_similarity >= max(0.15, threshold * 0.55)
        or candidate.char_similarity >= threshold
    )]
    useful.sort(key=lambda item: (item.end_index, item.start_index))
    ends = [item.end_index for item in useful]
    previous = [bisect_left(ends, item.start_index) - 1 for item in useful]
    scores: list[float] = []
    for item in useful:
        duration = max(0.001, (item.end_ms - item.start_ms) / 1000)
        duration_penalty = min(0.5, abs(duration - target_duration) / target_duration * 0.15)
        # Candidate selection must prefer wording completeness over a duration that merely
        # looks ideal. Otherwise one half of a genuinely overlong execution can hide the
        # too_long violation. Duration remains a small tie-breaker; compliance is judged later.
        quality = item.semantic_similarity + item.char_similarity * 0.75 - duration_penalty
        if item.semantic_similarity >= threshold:
            quality += 0.3
        scores.append(max(0.05, quality))
    best = [0.0] * (len(useful) + 1)
    take = [False] * len(useful)
    for index, score in enumerate(scores, 1):
        included = score + best[previous[index - 1] + 1]
        excluded = best[index - 1]
        if included > excluded:
            best[index] = included
            take[index - 1] = True
        else:
            best[index] = excluded
    selected: list[_Candidate] = []
    index = len(useful) - 1
    while index >= 0:
        included = scores[index] + best[previous[index] + 1]
        if take[index] and included >= best[index + 1] - 1e-9:
            selected.append(useful[index])
            index = previous[index]
        else:
            index -= 1
    return sorted(selected, key=lambda item: (item.start_ms, item.end_ms))


def _active_intervals(
    pieces: list[_Piece], turns: Iterable[StreamerTurn], streamer_id: int,
) -> list[tuple[int, int]]:
    intervals = sorted(
        (max(0, turn.start_ms), max(turn.start_ms, turn.end_ms))
        for turn in turns
        if turn.streamer_id == streamer_id and turn.end_ms > turn.start_ms
    )
    if not intervals and pieces:
        intervals = [(pieces[0].start_ms, max(pieces[-1].end_ms, pieces[0].start_ms + 1))]
    merged: list[tuple[int, int]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _active_elapsed_ms(at_ms: int, intervals: list[tuple[int, int]]) -> int:
    elapsed = 0
    for start, end in intervals:
        if at_ms < start:
            break
        elapsed += min(at_ms, end) - start
        if at_ms <= end:
            break
    return max(0, elapsed)


async def scan_transcripts(
    transcript_rows: list[Transcript],
    snapshot: dict,
    *,
    turns: list[StreamerTurn] | None = None,
    streamer_id: int,
    embedder: EmbeddingFunction | None = None,
) -> dict:
    embedder = embedder or embed_texts
    pieces = _pieces(transcript_rows)
    if not pieces:
        raise ScriptNotAnalyzable("逐字稿缺失或有效文本不足，无法分析规定话术")
    candidates = _character_candidates(pieces, snapshot)
    if candidates:
        vectors = await embedder([
            str(snapshot["standard_script"]), *[candidate.text for candidate in candidates],
        ])
        if (
            not isinstance(vectors, list)
            or len(vectors) != len(candidates) + 1
            or not vectors[0]
        ):
            raise EmbeddingUnavailable(
                "Embedding 服务不可用，未生成规定话术相似度结果",
            )
        dimensions = len(vectors[0])
        if any(
            not isinstance(vector, list)
            or len(vector) != dimensions
            or any(
                not isinstance(value, (int, float)) or not math.isfinite(value)
                for value in vector
            )
            for vector in vectors
        ):
            raise EmbeddingUnavailable(
                "Embedding 服务返回的数据不完整，未生成规定话术相似度结果",
            )
        for candidate, vector in zip(candidates, vectors[1:]):
            candidate.semantic_similarity = _cosine(vectors[0], vector)
    selected = _weighted_non_overlapping(candidates, snapshot)
    intervals = _active_intervals(pieces, turns or [], streamer_id)
    active_duration_ms = sum(end - start for start, end in intervals)
    repeat_ms = int(snapshot["repeat_interval_sec"]) * 1000
    tolerance_ms = int(snapshot["interval_tolerance_sec"]) * 1000
    expected_count = max(1, math.ceil(active_duration_ms / repeat_ms))
    threshold = float(snapshot["similarity_threshold"])
    target_duration = float(snapshot["target_duration_sec"])
    duration_tolerance = float(snapshot["duration_tolerance_sec"])
    occurrences: list[dict] = []
    previous_valid_elapsed: int | None = None
    valid_similarity_count = 0
    valid_intervals: list[float] = []
    for candidate in selected:
        similarity_passed = candidate.semantic_similarity >= threshold
        duration = round(max(0, candidate.end_ms - candidate.start_ms) / 1000, 3)
        duration_passed = (
            target_duration - duration_tolerance
            <= duration
            <= target_duration + duration_tolerance
        )
        issues: list[str] = []
        if not similarity_passed:
            issues.append("low_similarity")
        if duration < max(0, target_duration - duration_tolerance):
            issues.append("too_short")
        elif duration > target_duration + duration_tolerance:
            issues.append("too_long")
        elapsed = _active_elapsed_ms(candidate.start_ms, intervals)
        delta_from_previous = (
            max(0, elapsed - previous_valid_elapsed)
            if previous_valid_elapsed is not None else None
        )
        # Even a low-similarity candidate is an actual detected attempt. Keep its distance
        # from the previous valid execution so the client can display a complete timeline.
        interval_from_previous = (
            round(delta_from_previous / 1000, 3)
            if delta_from_previous is not None else None
        )
        frequency_passed = False
        if similarity_passed:
            valid_similarity_count += 1
            if previous_valid_elapsed is None:
                if elapsed > repeat_ms + tolerance_ms:
                    issues.append("late")
                else:
                    frequency_passed = True
            else:
                valid_intervals.append(interval_from_previous)
                if delta_from_previous < max(0, repeat_ms - tolerance_ms):
                    issues.append("too_early")
                elif delta_from_previous > repeat_ms + tolerance_ms:
                    issues.append("late")
                else:
                    frequency_passed = True
            previous_valid_elapsed = elapsed
        status = issues[0] if issues else "matched"
        occurrences.append({
            "status": status,
            "issues": issues,
            "actual_start_ms": candidate.start_ms,
            "actual_end_ms": candidate.end_ms,
            "actual_duration_sec": duration,
            "interval_from_previous_sec": interval_from_previous,
            "similarity_percent": round(candidate.semantic_similarity * 100, 2),
            "character_similarity_percent": round(candidate.char_similarity * 100, 2),
            "transcript_ids": candidate.transcript_ids,
            "raw_transcript": candidate.text,
            "similarity_passed": similarity_passed,
            "duration_passed": duration_passed,
            "frequency_passed": frequency_passed,
        })
    missing_count = max(0, expected_count - valid_similarity_count)
    for _ in range(missing_count):
        occurrences.append({
            "status": "missing",
            "issues": ["missing"],
            "actual_start_ms": None,
            "actual_end_ms": None,
            "actual_duration_sec": None,
            "interval_from_previous_sec": None,
            "similarity_percent": None,
            "character_similarity_percent": None,
            "transcript_ids": [],
            "raw_transcript": "",
            "similarity_passed": False,
            "duration_passed": False,
            "frequency_passed": False,
        })
    matched_count = sum(
        item["similarity_passed"] and item["duration_passed"] and item["frequency_passed"]
        for item in occurrences
    )
    actual_occurrences = [item for item in occurrences if item["actual_start_ms"] is not None]
    overall_status = "matched"
    if missing_count:
        overall_status = "missing"
    else:
        first_issue = next((item["status"] for item in actual_occurrences
                            if item["status"] != "matched"), None)
        if first_issue:
            overall_status = first_issue
    summary = {
        "expected_count": expected_count,
        "actual_count": len(actual_occurrences),
        "matched_count": matched_count,
        "missing_count": missing_count,
        "compliance_rate": round(min(1.0, matched_count / expected_count), 4),
        "first_occurrence_ms": (
            actual_occurrences[0]["actual_start_ms"] if actual_occurrences else None
        ),
        "actual_time_points_ms": [item["actual_start_ms"] for item in actual_occurrences],
        "actual_intervals_sec": valid_intervals,
        "average_interval_sec": (
            round(sum(valid_intervals) / len(valid_intervals), 3) if valid_intervals else None
        ),
        "min_interval_sec": min(valid_intervals) if valid_intervals else None,
        "max_interval_sec": max(valid_intervals) if valid_intervals else None,
        "actual_durations_sec": [item["actual_duration_sec"] for item in actual_occurrences],
        "similarity_percentages": [
            item["similarity_percent"] for item in actual_occurrences
        ],
        "active_duration_sec": round(active_duration_ms / 1000, 3),
        "overall_status": overall_status,
        "occurrences": occurrences,
    }
    return summary


def _result_with_occurrences(db, result: ScriptComplianceResult) -> dict:  # noqa: ANN001
    rows = db.execute(select(ScriptComplianceOccurrence).where(
        ScriptComplianceOccurrence.result_id == result.id,
    ).order_by(ScriptComplianceOccurrence.ordinal)).scalars().all()
    summary = _json_loads(result.summary, {})
    occurrences = []
    for row in rows:
        item = {
            "id": row.id,
            "ordinal": row.ordinal,
            "status": row.status,
            "issues": _json_loads(row.issues, []),
            "actual_start_ms": row.actual_start_ms,
            "actual_end_ms": row.actual_end_ms,
            "actual_duration_sec": row.actual_duration_sec,
            "interval_from_previous_sec": row.interval_from_previous_sec,
            "similarity_percent": row.similarity_percent,
            "transcript_ids": _json_loads(row.transcript_ids, []),
            "raw_transcript": row.raw_transcript,
            "similarity_passed": row.similarity_passed,
            "duration_passed": row.duration_passed,
            "frequency_passed": row.frequency_passed,
        }
        occurrences.append(item)
    return {
        "id": result.id,
        "plan_id": result.plan_id,
        "session_id": result.session_id,
        "streamer_id": result.streamer_id,
        "rule_id": result.rule_id,
        "rule_version": result.rule_version,
        "status": result.status,
        "overall_status": result.overall_status,
        "expected_count": result.expected_count,
        "actual_count": result.actual_count,
        "matched_count": result.matched_count,
        "missing_count": result.missing_count,
        "compliance_rate": result.compliance_rate,
        "first_occurrence_ms": result.first_occurrence_ms,
        "average_interval_sec": result.average_interval_sec,
        "min_interval_sec": result.min_interval_sec,
        "max_interval_sec": result.max_interval_sec,
        "summary": summary if isinstance(summary, dict) else {},
        "occurrences": occurrences,
        "error_code": result.error_code,
        "error_msg": result.error_msg,
        "created_at": _iso(result.created_at),
        "updated_at": _iso(result.updated_at),
    }


def _claim_result(plan_id: int) -> tuple[int, str | None]:
    run_id = uuid.uuid4().hex
    with SessionLocal() as db:
        plan = db.get(SessionScriptCompliancePlan, plan_id)
        if plan is None:
            raise LookupError("场次规定话术计划不存在")
        result = db.execute(select(ScriptComplianceResult).where(
            ScriptComplianceResult.plan_id == plan_id,
        )).scalar_one_or_none()
        if result is not None and result.status == "ready":
            return result.id, None
        if result is not None and result.status == "analyzing" and result.analysis_run_id:
            lease_started = result.updated_at or result.created_at
            if lease_started and lease_started > datetime.now() - timedelta(
                minutes=ANALYSIS_LEASE_MINUTES,
            ):
                raise ScriptAnalysisInProgress("该规则正在分析，请稍后查询结果")
        now = datetime.now()
        if result is None:
            result = ScriptComplianceResult(
                plan_id=plan.id,
                session_id=plan.session_id,
                streamer_id=plan.streamer_id,
                rule_id=plan.rule_id,
                rule_version=plan.rule_version,
                status="analyzing",
                overall_status="not_analyzable",
                analysis_run_id=run_id,
                created_at=now,
                updated_at=now,
            )
            db.add(result)
        else:
            previous_status = result.status
            previous_run_id = result.analysis_run_id
            claimed = db.execute(update(ScriptComplianceResult).where(
                ScriptComplianceResult.id == result.id,
                ScriptComplianceResult.status == previous_status,
                ScriptComplianceResult.analysis_run_id == previous_run_id,
            ).values(
                status="analyzing",
                analysis_run_id=run_id,
                error_code="",
                error_msg="",
                updated_at=now,
            ))
            if claimed.rowcount != 1:
                db.rollback()
                raise ScriptAnalysisInProgress("该规则正在分析，请稍后查询结果")
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            concurrent = db.execute(select(ScriptComplianceResult).where(
                ScriptComplianceResult.plan_id == plan_id,
            )).scalar_one_or_none()
            if concurrent is not None and concurrent.status == "ready":
                return concurrent.id, None
            raise ScriptAnalysisInProgress("该规则正在分析，请稍后查询结果")
        return result.id, run_id


def _failure_message(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, EmbeddingUnavailable):
        return "EMBEDDING_UNAVAILABLE", "Embedding 服务不可用，请检查配置或云端额度后重试"
    if isinstance(exc, ScriptNotAnalyzable):
        return "NOT_ANALYZABLE", "逐字稿缺失或有效文本不足，无法分析规定话术"
    return safe_exception_code(exc, prefix="SCRIPT_ANALYSIS"), "规定话术分析失败，请稍后重试"


def _mark_failed(result_id: int, run_id: str, exc: Exception) -> None:
    error_code, error_message = _failure_message(exc)
    with SessionLocal() as db:
        row = db.get(ScriptComplianceResult, result_id)
        if row is None or row.analysis_run_id != run_id:
            return
        row.status = "failed"
        row.overall_status = "not_analyzable"
        row.analysis_run_id = ""
        row.error_code = error_code
        row.error_msg = error_message
        row.updated_at = datetime.now()
        db.commit()


async def _analyze_plan(plan_id: int) -> dict:
    result_id, run_id = _claim_result(plan_id)
    if run_id is None:
        with SessionLocal() as db:
            return _result_with_occurrences(db, db.get(ScriptComplianceResult, result_id))
    request_id = hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:12]
    started = time.monotonic()
    try:
        with SessionLocal() as db:
            plan = db.get(SessionScriptCompliancePlan, plan_id)
            snapshot = _json_loads(plan.rule_snapshot, {})
            transcripts = db.execute(select(Transcript).where(
                Transcript.session_id == plan.session_id,
                Transcript.streamer_id == plan.streamer_id,
            ).order_by(Transcript.start_ms, Transcript.seq, Transcript.id)).scalars().all()
            turns = db.execute(select(StreamerTurn).where(
                StreamerTurn.session_id == plan.session_id,
                StreamerTurn.streamer_id == plan.streamer_id,
            ).order_by(StreamerTurn.start_ms, StreamerTurn.seq)).scalars().all()
            # Copy ORM values before the provider call so no DB transaction spans network I/O.
            transcript_values = [Transcript(
                id=row.id,
                session_id=row.session_id,
                streamer_id=row.streamer_id,
                seq=row.seq,
                start_ms=row.start_ms,
                end_ms=row.end_ms,
                raw_text=row.raw_text,
                corrected_text=row.corrected_text,
            ) for row in transcripts]
            turn_values = [StreamerTurn(
                id=row.id,
                session_id=row.session_id,
                streamer_id=row.streamer_id,
                seq=row.seq,
                start_ms=row.start_ms,
                end_ms=row.end_ms,
            ) for row in turns]
            streamer_id = plan.streamer_id
        analysis = await scan_transcripts(
            transcript_values,
            snapshot,
            turns=turn_values,
            streamer_id=streamer_id,
        )
        occurrences = analysis.pop("occurrences")
        with SessionLocal() as db:
            row = db.get(ScriptComplianceResult, result_id)
            if row is None or row.analysis_run_id != run_id or row.status != "analyzing":
                raise ScriptAnalysisSuperseded("分析租约已失效，旧任务不得覆盖新结果")
            db.execute(delete(ScriptComplianceOccurrence).where(
                ScriptComplianceOccurrence.result_id == result_id,
            ))
            for ordinal, item in enumerate(occurrences, 1):
                db.add(ScriptComplianceOccurrence(
                    result_id=result_id,
                    ordinal=ordinal,
                    status=item["status"],
                    issues=json.dumps(item["issues"], ensure_ascii=False),
                    actual_start_ms=item["actual_start_ms"],
                    actual_end_ms=item["actual_end_ms"],
                    actual_duration_sec=item["actual_duration_sec"],
                    interval_from_previous_sec=item["interval_from_previous_sec"],
                    similarity_percent=item["similarity_percent"],
                    transcript_ids=json.dumps(item["transcript_ids"]),
                    raw_transcript=item["raw_transcript"],
                    similarity_passed=item["similarity_passed"],
                    duration_passed=item["duration_passed"],
                    frequency_passed=item["frequency_passed"],
                ))
            row.status = "ready"
            row.overall_status = analysis["overall_status"]
            row.analysis_run_id = ""
            row.expected_count = analysis["expected_count"]
            row.actual_count = analysis["actual_count"]
            row.matched_count = analysis["matched_count"]
            row.missing_count = analysis["missing_count"]
            row.compliance_rate = analysis["compliance_rate"]
            row.first_occurrence_ms = analysis["first_occurrence_ms"]
            row.average_interval_sec = analysis["average_interval_sec"]
            row.min_interval_sec = analysis["min_interval_sec"]
            row.max_interval_sec = analysis["max_interval_sec"]
            row.summary = json.dumps(analysis, ensure_ascii=False)
            row.error_code = ""
            row.error_msg = ""
            row.updated_at = datetime.now()
            db.commit()
            db.refresh(row)
            response = _result_with_occurrences(db, row)
        logger.info(
            "script_compliance request_id=%s result_id=%s status=success "
            "elapsed_ms=%d occurrences=%d",
            request_id, result_id, int((time.monotonic() - started) * 1000),
            len(occurrences),
        )
        return response
    except Exception as exc:
        if not isinstance(exc, ScriptAnalysisSuperseded):
            _mark_failed(result_id, run_id, exc)
        logger.warning(
            "script_compliance request_id=%s result_id=%s status=failed "
            "elapsed_ms=%d code=%s",
            request_id, result_id, int((time.monotonic() - started) * 1000),
            safe_exception_code(exc, prefix="SCRIPT_ANALYSIS"),
        )
        raise


async def analyze_session(
    session_id: int,
    *,
    streamer_id: int | None = None,
    plan_ids: list[int] | None = None,
) -> dict:
    with SessionLocal() as db:
        if db.get(ReplaySession, session_id) is None:
            raise LookupError("场次不存在")
        query = select(SessionScriptCompliancePlan).where(
            SessionScriptCompliancePlan.session_id == session_id,
        )
        if streamer_id is not None:
            query = query.where(SessionScriptCompliancePlan.streamer_id == streamer_id)
        if plan_ids:
            query = query.where(SessionScriptCompliancePlan.id.in_(set(plan_ids)))
        plans = db.execute(query.order_by(SessionScriptCompliancePlan.id)).scalars().all()
        if not plans:
            raise LookupError("场次尚未分配规定话术计划")
        for plan in plans:
            has_transcript = db.execute(select(Transcript.id).where(
                Transcript.session_id == session_id,
                Transcript.streamer_id == plan.streamer_id,
                Transcript.corrected_text != "",
            ).limit(1)).scalar_one_or_none()
            if has_transcript is None:
                has_transcript = db.execute(select(Transcript.id).where(
                    Transcript.session_id == session_id,
                    Transcript.streamer_id == plan.streamer_id,
                    Transcript.raw_text != "",
                ).limit(1)).scalar_one_or_none()
            if has_transcript is None:
                raise ScriptNotAnalyzable(
                    "逐字稿缺失或有效文本不足，未创建规定话术分析结果",
                )
        selected_ids = [plan.id for plan in plans]
    results = []
    for plan_id in selected_ids:
        results.append(await _analyze_plan(plan_id))
    return {"session_id": session_id, "results": results}


def list_session_compliance(session_id: int, *, streamer_id: int | None = None) -> dict:
    plans = list_plans(session_id, streamer_id=streamer_id)
    with SessionLocal() as db:
        result_by_plan = {
            row.plan_id: _result_with_occurrences(db, row)
            for row in db.execute(select(ScriptComplianceResult).where(
                ScriptComplianceResult.session_id == session_id,
                *(
                    [ScriptComplianceResult.streamer_id == streamer_id]
                    if streamer_id is not None else []
                ),
            )).scalars().all()
        }
    for plan in plans:
        plan["result"] = result_by_plan.get(plan["id"])
    return {"session_id": session_id, "plans": plans}


def get_result(session_id: int, result_id: int) -> dict:
    with SessionLocal() as db:
        row = db.get(ScriptComplianceResult, result_id)
        if row is None or row.session_id != session_id:
            raise LookupError("规定话术分析结果不存在")
        return _result_with_occurrences(db, row)
