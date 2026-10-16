"""知识库管理：产品规格 / 话术规范 / 违禁词 的增删改 + Excel 批量导入。"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import tempfile
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core import course
from ..core import rhythm_library as rhythm_core
from ..core.document_text import TEXT_DOCUMENT_EXTENSIONS, extract_document_text
from ..core.embed import embed_texts, embedding_health
from ..core.knowledge import load_kb
from ..core.review import experience as experience_core
from ..core.scene import detect_scene, scene_label
from ..core.safe_error import safe_error_text, safe_exception_code
from ..db import SessionLocal, get_db
from ..models import KnowledgeChunk, KnowledgeItem
from ..schemas import KnowledgeItemIn, KnowledgeItemOut

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

# 上传体积上限（P1-5）：文档 30MB / 音视频 500MB
_DOC_EXT = TEXT_DOCUMENT_EXTENSIONS
_DOC_MAX = 30 * 1024 * 1024
_MEDIA_MAX = 500 * 1024 * 1024


@router.get("", response_model=list[KnowledgeItemOut])
def list_items(kind: str | None = None, db: Session = Depends(get_db)):
    stmt = select(KnowledgeItem)
    if kind:
        stmt = stmt.where(KnowledgeItem.kind == kind)
    return db.execute(stmt.order_by(KnowledgeItem.id)).scalars().all()


@router.post("", response_model=KnowledgeItemOut)
def create_item(payload: KnowledgeItemIn, db: Session = Depends(get_db)):
    item = KnowledgeItem(**payload.model_dump())
    db.add(item)
    db.flush()
    if item.kind == "forbidden":
        from ..core.compliance_rules import sync_legacy_forbidden
        sync_legacy_forbidden(db, item)
    db.commit()
    db.refresh(item)
    return item


@router.put("/{item_id}", response_model=KnowledgeItemOut)
def update_item(item_id: int, payload: KnowledgeItemIn, db: Session = Depends(get_db)):
    item = db.get(KnowledgeItem, item_id)
    if not item:
        raise HTTPException(404, "条目不存在")
    was_forbidden = item.kind == "forbidden"
    for k, v in payload.model_dump().items():
        setattr(item, k, v)
    from ..core.compliance_rules import remove_legacy_forbidden, sync_legacy_forbidden
    if item.kind == "forbidden":
        sync_legacy_forbidden(db, item)
    elif was_forbidden:
        remove_legacy_forbidden(db, item.id)
    db.commit()
    db.refresh(item)
    return item


@router.delete("/{item_id}")
def delete_item(item_id: int, db: Session = Depends(get_db)):
    item = db.get(KnowledgeItem, item_id)
    if not item:
        raise HTTPException(404, "条目不存在")
    if item.kind == "forbidden":
        from ..core.compliance_rules import remove_legacy_forbidden
        remove_legacy_forbidden(db, item.id)
    db.delete(item)
    db.commit()
    return {"deleted": True}


@router.post("/import")
async def import_excel(file: UploadFile, db: Session = Depends(get_db)):
    """Excel 列：kind | name | aliases | attrs(JSON) | note（首行为表头）。"""
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise HTTPException(500, "未安装 openpyxl")

    data = await file.read()
    wb = load_workbook(io.BytesIO(data), read_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return {"imported": 0}

    header = [str(c).strip().lower() if c else "" for c in rows[0]]
    idx = {name: header.index(name) for name in header if name}
    count = 0
    for r in rows[1:]:
        def cell(col: str) -> str:
            i = idx.get(col)
            return str(r[i]).strip() if (i is not None and i < len(r) and r[i] is not None) else ""

        kind = cell("kind")
        name = cell("name")
        if not kind or not name:
            continue
        # 导入同一份清单时按 kind + name 更新，避免重复条目让
        # ASR 热词和型号匹配结果随导入次数膨胀。
        item = db.execute(
            select(KnowledgeItem).where(
                KnowledgeItem.kind == kind,
                KnowledgeItem.name == name,
            )
        ).scalar_one_or_none()
        if item is None:
            item = KnowledgeItem(kind=kind, name=name)
            db.add(item)
        item.aliases = cell("aliases")
        item.attrs = cell("attrs")
        item.note = cell("note")
        db.flush()
        if kind == "forbidden":
            from ..core.compliance_rules import sync_legacy_forbidden
            sync_legacy_forbidden(db, item)
        count += 1
    db.commit()
    return {"imported": count}


# ========== RAG 文档：文本/文件录入 + 切片 + 向量化 ==========

def _chunk_text(text: str, size: int = 400) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    chunks, buf = [], ""
    for para in re.split(r"\n{1,}", text):
        para = para.strip()
        if not para:
            continue
        if len(buf) + len(para) + 1 > size and buf:
            chunks.append(buf)
            buf = para
        else:
            buf = f"{buf}\n{para}" if buf else para
        while len(buf) > size * 1.5:        # 超长段硬切
            chunks.append(buf[:size])
            buf = buf[size:]
    if buf:
        chunks.append(buf)
    return chunks


async def _store_chunks(db: Session, source: str, chunks: list[str],
                        kind: str = "doc", category: str = "",
                        scene: str = "", auto_scene: bool = False) -> int:
    """auto_scene=True 时按每片文本关键词识别场景（原文切片用）；否则用传入的 scene。"""
    chunks = [c for c in chunks if c and c.strip()]
    if not chunks:
        return 0
    vecs = await embed_texts(chunks)   # 失败返回 []，则只存文本（仍可关键词检索）
    for i, ck in enumerate(chunks):
        emb = json.dumps(vecs[i]) if i < len(vecs) else ""
        sc = detect_scene(ck)[0] if auto_scene else scene
        db.add(KnowledgeChunk(source=source, text=ck, embedding=emb,
                              kind=kind, category=category, scene=sc))
    db.commit()
    return len(chunks)


async def _ingest_text(db: Session, source: str, text: str) -> dict:
    """统一录入：先 LLM 提炼复盘要点（优先检索），再存原文切片（回溯）。"""
    distilled = 0
    points = await course.distill_points(text)
    if points:
        # 每条要点单独成片，带 category（内容类型）+ scene（话术场景）便于分库检索/展示
        for p in points:
            await _store_chunks(db, f"[提炼] {source}", [p["point"]], kind="distilled",
                                category=p.get("category", ""), scene=p.get("scene", ""))
        distilled = len(points)
    raw = await _store_chunks(db, source, _chunk_text(text), kind="doc", auto_scene=True)
    return {"source": source, "distilled": distilled, "raw": raw}


def _ingest_media_bg(filename: str, tmp_path: str) -> None:
    """音视频后台任务：转写 → 提炼 → 入库（独立事件循环 + 会话）。"""
    async def _run():
        try:
            with SessionLocal() as _kbdb:      # with 确保连接释放，不泄漏 DB session
                hotwords = load_kb(_kbdb).hotwords()
            text = await course.transcribe_media(Path(tmp_path), hotwords)
            with SessionLocal() as db:
                if text.strip():
                    await _ingest_text(db, f"[课程] {filename}", text)
                else:
                    await _store_chunks(
                        db, f"[课程] {filename}",
                        ["（音视频未能转写：未配置 ASR key 或无可识别语音）"], kind="doc")
        except Exception as e:  # noqa: BLE001
            print(
                "[knowledge] media_ingest status=failed; "
                f"code={safe_exception_code(e, prefix='KNOWLEDGE_MEDIA')}; "
                f"error={safe_error_text(e)}"
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    asyncio.run(_run())


@router.post("/text")
async def add_text(source: str = Body("手动录入"), text: str = Body(...),
                   db: Session = Depends(get_db)):
    if not (text or "").strip():
        raise HTTPException(400, "文本为空")
    return await _ingest_text(db, source or "手动录入", text)


@router.post("/upload")
async def upload_doc(file: UploadFile, db: Session = Depends(get_db)):
    name = (file.filename or "").lower()
    is_media = course.is_media(name)
    if not is_media and not name.endswith(_DOC_EXT):
        raise HTTPException(400, f"不支持的文件类型：{file.filename}")
    cap = _MEDIA_MAX if is_media else _DOC_MAX
    # 流式读取并累计校验，超限立即中止，避免 500MB 大文件整包读进内存造成尖峰
    buf = bytearray()
    while True:
        chunk = await file.read(1 << 20)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > cap:
            raise HTTPException(413, f"文件过大（上限 {cap // 1024 // 1024}MB）")
    data = bytes(buf)
    if not data:
        raise HTTPException(400, "空文件")

    if is_media:
        # 音视频转写慢，存临时文件后台处理，立即返回（前端轮询 /chunks）
        suffix = Path(name).suffix
        fd, tmp = tempfile.mkstemp(suffix=suffix, prefix="kb_media_")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        asyncio.create_task(asyncio.to_thread(_ingest_media_bg, file.filename, tmp))
        return {"source": file.filename, "processing": True,
                "message": "音视频已接收，正在转写+提炼，完成后出现在知识库切片里"}

    try:
        text = await asyncio.to_thread(extract_document_text, file.filename, data)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"文档解析失败：{str(e)[:160]}") from e
    if not text.strip():
        raise HTTPException(400, "未从文件中提取到文本")
    return await _ingest_text(db, file.filename, text)


@router.get("/chunks")
def list_chunks(scene: str | None = None, category: str | None = None,
                kind: str | None = None, db: Session = Depends(get_db)):
    stmt = select(KnowledgeChunk)
    if scene:
        stmt = stmt.where(KnowledgeChunk.scene == scene)
    if category:
        stmt = stmt.where(KnowledgeChunk.category == category)
    if kind:
        stmt = stmt.where(KnowledgeChunk.kind == kind)
    rows = db.execute(stmt.order_by(KnowledgeChunk.id.desc())).scalars().all()
    return [{"id": r.id, "source": r.source, "text": r.text[:120],
             "kind": getattr(r, "kind", "doc"), "category": getattr(r, "category", ""),
             "scene": getattr(r, "scene", ""),
             "scene_label": scene_label(r.scene) if getattr(r, "scene", "") else "",
             "indexed": bool(r.embedding)} for r in rows]


@router.get("/scenes")
def list_scenes(db: Session = Depends(get_db)):
    """知识库分库概览：各话术场景下的切片数（供前端分库浏览）。"""
    from ..core.scene import SCENES
    rows = db.execute(
        select(KnowledgeChunk.scene, func.count()).group_by(KnowledgeChunk.scene)
    ).all()
    counts = {s or "": n for s, n in rows}
    out = [{"key": k, "label": name, "count": counts.get(k, 0)} for k, name, _ in SCENES]
    out.append({"key": "general", "label": "通用", "count": counts.get("general", 0)})
    if counts.get(""):
        out.append({"key": "", "label": "未分场景", "count": counts[""]})
    return out


@router.get("/embed_status")
def embed_status(db: Session = Depends(get_db)):
    total = db.execute(select(func.count()).select_from(KnowledgeChunk)).scalar() or 0
    indexed = db.execute(
        select(func.count()).select_from(KnowledgeChunk)
        .where(KnowledgeChunk.embedding != "")).scalar() or 0
    h = embedding_health()
    return {"total": total, "indexed": indexed, "unindexed": total - indexed, **h}


@router.delete("/chunks/{chunk_id}")
def delete_chunk(chunk_id: int, db: Session = Depends(get_db)):
    r = db.get(KnowledgeChunk, chunk_id)
    if r:
        origin_type, origin_id = r.origin_type, r.origin_id
        if origin_id is not None and origin_type in {"rhythm_template", "experience_tip"}:
            # 结构化资产必须走各自的 version/CAS 审核状态机。若在这个通用入口里
            # 直接改 ORM 快照，会与并发采纳/驳回互相覆盖。
            db.rollback()
            if origin_type == "rhythm_template":
                try:
                    result = rhythm_core.reject_template(origin_id)
                except LookupError:
                    result = {"ok": False, "missing": True}
                except rhythm_core.TemplateStateError as exc:
                    raise HTTPException(409, str(exc)) from exc
            else:
                result = experience_core.reject_tip(origin_id)
                if not result.get("ok") and "不存在" not in result.get("error", ""):
                    raise HTTPException(409, result.get("error", "经验审核状态已变化"))
            if result.get("ok"):
                return {"deleted": True, "structured_status": "rejected"}
            # owner 已被另一事务删除时，下面只清理仍存在的孤儿切片。
            r = db.get(KnowledgeChunk, chunk_id)
            if r is None:
                return {"deleted": True}
        db.delete(r)
        db.commit()
    return {"deleted": True}
