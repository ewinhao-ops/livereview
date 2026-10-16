"""v2 深度复盘的生成任务登记（进程内，2026-09-30）。

- 同一（场次, 主播）同一时刻只跑一份：后来的请求直接等这一份的结果，不再重复调模型花钱。
- 任务不挂在发起它的请求上：页面关掉、换设备再点，等到的都是同一份。
- sessions.deep_status 是按场次记的：同场有多位主播在生成时，等最后一个收尾才改成 done/failed，
  先跑完的不会把还在跑的盖成 done。
- 服务是单进程（uvicorn 单 worker），登记放内存即可；重启后登记清空，
  启动时用 reset_stale_deep_status() 把库里残留的 running 复位。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import update

from ...db import SessionLocal
from ...models import ReplaySession
from ..safe_error import safe_error_text, safe_exception_code
from ..usage import llm_context
from .deep_analysis import V2Outcome, generate_v2


@dataclass
class V2Job:
    session_id: int
    streamer_id: int | None
    only_failed: bool
    started_at: datetime
    task: asyncio.Task | None = None      # 结果是 V2Outcome；任务自己兜住异常，不会抛给等它的人


_jobs: dict[tuple[int, int | None], V2Job] = {}
_failed_sessions: set[int] = set()        # 这一轮（该场次从没人在跑到全部跑完）里有没有没成的


def _set_deep_status(session_id: int, status: str) -> None:
    try:
        with SessionLocal() as db:
            db.execute(update(ReplaySession).where(ReplaySession.id == session_id)
                       .values(deep_status=status))
            db.commit()
    except Exception as exc:  # noqa: BLE001  状态只是展示用，写不上不能连累生成
        print(
            f"[v2_jobs] 状态没写上 session={session_id} status={status}; "
            f"code={safe_exception_code(exc, prefix='V2_STATUS')}; error={safe_error_text(exc)}"
        )


def _finish(job: V2Job, ok: bool) -> None:
    key = (job.session_id, job.streamer_id)
    if _jobs.get(key) is job:
        _jobs.pop(key)
    if not ok:
        _failed_sessions.add(job.session_id)
    if any(session_id == job.session_id for session_id, _ in _jobs):
        return                            # 同场还有别的主播在生成：整场保持 running，等最后一个收尾
    failed = job.session_id in _failed_sessions
    _failed_sessions.discard(job.session_id)
    _set_deep_status(job.session_id, "failed" if failed else "done")


async def _run(job: V2Job) -> V2Outcome:
    ok = False
    try:
        with llm_context(job.session_id, "v2"):
            outcome = await generate_v2(
                job.session_id, job.streamer_id, only_failed=job.only_failed,
            )
        ok = outcome.ok
        took = int((datetime.now() - job.started_at).total_seconds())
        print(
            f"[v2_jobs] 生成{'完成' if ok else '未成'} session={job.session_id} "
            f"sid={job.streamer_id}; took={took}s; blocks={','.join(outcome.reran) or '-'}; "
            f"failed={','.join(outcome.failed) or '-'}; reason={outcome.error or '-'}"
        )
        return outcome
    except Exception as exc:  # noqa: BLE001
        print(
            f"[v2_jobs] 生成出错 session={job.session_id} sid={job.streamer_id}; "
            f"code={safe_exception_code(exc, prefix='V2_JOB')}; error={safe_error_text(exc)}"
        )
        return V2Outcome(error="深度复盘生成出错，请稍后重试")
    finally:
        _finish(job, ok)                  # 被取消（服务关闭）也走这里，状态落成 failed


def start_job(session_id: int, streamer_id: int | None, *,
              only_failed: bool = False) -> tuple[V2Job, bool]:
    """开始生成；同一（场次, 主播）已经在跑就直接返回那一份。返回 (任务, 是不是这次新开的)。"""
    key = (session_id, streamer_id or None)
    job = _jobs.get(key)
    if job is not None and job.task is not None and not job.task.done():
        return job, False
    job = V2Job(session_id, key[1], only_failed, datetime.now())
    job.task = asyncio.create_task(_run(job))
    _jobs[key] = job
    _set_deep_status(session_id, "running")
    return job, True


async def wait_job(job: V2Job) -> V2Outcome:
    """等任务出结果。等的人中途走了（请求断开）不影响任务继续跑。"""
    return await asyncio.shield(job.task)


def running_jobs(session_id: int) -> dict[int | None, dict]:
    """该场次正在生成的主播 -> 给页面看的状态。会被线程池里的同步接口调用，所以先取快照再遍历。"""
    now = datetime.now()
    return {
        job.streamer_id: {
            "status": "running",
            "started_at": job.started_at.isoformat(timespec="seconds"),
            "elapsed_sec": max(0, int((now - job.started_at).total_seconds())),
            "only_failed": job.only_failed,
        }
        for (sid, _), job in list(_jobs.items())
        if sid == session_id and job.task is not None and not job.task.done()
    }


def reset_stale_deep_status() -> int:
    """服务刚启动时调用：上个进程没跑完的生成不会再有人接着跑，把残留的 running 改成 failed
    （页面上可以重新生成）。返回复位的场次数。"""
    with SessionLocal() as db:
        changed = db.execute(
            update(ReplaySession)
            .where(ReplaySession.deep_status.in_(("running", "queued")))
            .values(deep_status="failed")
        ).rowcount
        db.commit()
    return int(changed or 0)


async def cancel_all_jobs() -> None:
    """服务关闭时调用：停掉还在跑的生成。"""
    tasks = [job.task for job in _jobs.values() if job.task is not None and not job.task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    for job in list(_jobs.values()):      # 还没来得及开跑就被取消的任务不会自己收尾，这里补上
        if job.task is not None and job.task.done():
            _finish(job, False)
