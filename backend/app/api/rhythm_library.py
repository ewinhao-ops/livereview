"""直播技巧/运营经验入库与“优秀节奏库”API。"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field, field_validator

from ..core.document_text import TEXT_DOCUMENT_EXTENSIONS, extract_document_text
from ..core import rhythm_library as library
from ..core.llm import llm_status, test_llm_connection


router = APIRouter(prefix="/api/rhythm-library", tags=["rhythm-library"])
_DOC_MAX = 30 * 1024 * 1024


@router.get("/llm/status")
def get_llm_status():
    """检查当前选择和配置是否完整；响应中不会包含 API Key。"""
    return llm_status()


@router.post("/llm/test")
async def test_current_llm():
    """真实调用一次当前模型，防止 mock 回退掩盖错误密钥或错误地址。"""
    try:
        return await test_llm_connection()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            502, f"大模型连接失败：{type(exc).__name__}: {str(exc)[:300]}",
        ) from exc


class CorpusTextIn(BaseModel):
    source: str = Field(default="手动录入", max_length=255)
    corpus_type: Literal["live_skill", "operations"] = "live_skill"
    text: str = Field(min_length=20, max_length=library.MAX_CORPUS_CHARS)

    @field_validator("source", "text")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


def _bad_request(exc: ValueError) -> HTTPException:
    return HTTPException(400, str(exc))


@router.post("/corpora/text", status_code=201)
async def add_text(payload: CorpusTextIn):
    try:
        return await library.ingest_corpus(payload.source, payload.corpus_type, payload.text)
    except ValueError as exc:
        raise _bad_request(exc) from exc
    except library.CorpusProcessingStateError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/corpora/upload", status_code=201)
async def upload_text_document(
    file: UploadFile = File(...),
    corpus_type: Literal["live_skill", "operations"] = Form("live_skill"),
    source: str = Form("", max_length=255),
):
    """只接收文字文档；按产品约定明确拒绝音视频及服务端转写。"""
    filename = file.filename or ""
    if not filename.lower().endswith(TEXT_DOCUMENT_EXTENSIONS):
        raise HTTPException(
            400,
            "优秀经验入口只接收已转成文字的文档（txt/md/docx/pdf/xlsx/csv/json），不接收音视频",
        )
    data = bytearray()
    while True:
        block = await file.read(1 << 20)
        if not block:
            break
        data.extend(block)
        if len(data) > _DOC_MAX:
            raise HTTPException(413, "文件过大（上限 30MB）")
    if not data:
        raise HTTPException(400, "空文件")
    try:
        text = (await asyncio.to_thread(
            extract_document_text, filename, bytes(data), library.MAX_CORPUS_CHARS,
        )).strip()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"文档解析失败：{str(exc)[:160]}") from exc
    if not text:
        raise HTTPException(400, "未从文件中提取到文本")
    try:
        return await library.ingest_corpus(source.strip() or filename, corpus_type, text)
    except ValueError as exc:
        raise _bad_request(exc) from exc
    except library.CorpusProcessingStateError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/corpora")
def list_corpora(
    corpus_type: Literal["live_skill", "operations"] | None = None,
    status: Literal["processing", "ready", "failed"] | None = None,
):
    return {"corpora": library.list_corpora(corpus_type=corpus_type, status=status)}


@router.get("/corpora/{corpus_id}")
def get_corpus(corpus_id: int):
    corpus = library.get_corpus(corpus_id)
    if corpus is None:
        raise HTTPException(404, "语料不存在")
    return {"corpus": corpus, "templates": library.list_templates(corpus_id=corpus_id)}


@router.post("/corpora/{corpus_id}/retry")
async def retry_corpus(corpus_id: int):
    try:
        return await library.retry_corpus(corpus_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except library.CorpusProcessingStateError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.delete("/corpora/{corpus_id}")
def delete_corpus(corpus_id: int):
    if not library.delete_corpus(corpus_id):
        raise HTTPException(404, "语料不存在")
    return {"deleted": True}


@router.get("/templates")
def list_templates(
    status: Literal["pending", "adopted", "rejected"] | None = None,
    scene: str | None = Query(default=None, max_length=24),
    corpus_type: Literal["live_skill", "operations"] | None = None,
    q: str | None = Query(default=None, max_length=80),
):
    return {"templates": library.list_templates(
        status=status, scene=scene, corpus_type=corpus_type, query=q,
    )}


@router.get("/templates/{template_id}")
def get_template(template_id: int):
    template = library.get_template(template_id)
    if template is None:
        raise HTTPException(404, "节奏模板不存在")
    return {"template": template}


@router.post("/templates/{template_id}/adopt")
async def adopt_template(template_id: int):
    try:
        return await library.adopt_template(template_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except library.TemplateStateError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/templates/{template_id}/reject")
def reject_template(template_id: int):
    try:
        return library.reject_template(template_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except library.TemplateStateError as exc:
        raise HTTPException(409, str(exc)) from exc
