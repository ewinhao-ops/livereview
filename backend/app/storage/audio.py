"""音频文件管理：唯一持久化的媒体。提供下载定位与"彻底删除"。

彻底删除 = 删磁盘文件 + 置状态 deleted + 记删除时间。不可逆。
逐字稿/点评等文本默认保留（复盘价值在文本，不占空间）。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import AudioFile, AudioStatus


def get_audio(db: Session, session_id: int) -> AudioFile | None:
    return (
        db.query(AudioFile).filter(AudioFile.session_id == session_id).one_or_none()
    )


def hard_delete_audio(db: Session, session_id: int) -> tuple[bool, str]:
    """返回 (是否真的删了文件, 提示)。幂等：已删除再次调用安全。"""
    af = get_audio(db, session_id)
    if af is None:
        return False, "该场次没有音频记录"
    if af.status == AudioStatus.deleted:
        return False, "音频已是删除状态"

    removed = False
    p = Path(af.path)
    if p.exists():
        p.unlink()
        removed = True

    # 同步删 COS 对象并清 cos_key，否则"彻底删除"后云端仍残留、继续计费、隐私未清
    if af.cos_key:
        from ..core import media_store
        if af.cos_key.endswith(".m3u8"):
            media_store.delete_prefix(f"replay/session_{session_id}/")
            media_store.delete(f"replay/session_{session_id}.m4a")
        else:
            media_store.delete(af.cos_key)
        af.cos_key = ""
        removed = True

    af.status = AudioStatus.deleted
    af.deleted_at = datetime.now()
    af.size_bytes = 0
    db.commit()
    return removed, ("已从服务器彻底删除音频文件" if removed else "标记删除（磁盘文件本就不存在）")


def find_part_file(path: str):
    """录制中回退：成品 session_N.mp4 未合并时找最新分段 session_N.part*.mp4。"""
    from pathlib import Path
    p = Path(path or "")
    if not p.name:
        return None
    parts = sorted(p.parent.glob(p.stem + ".part*" + p.suffix))
    return parts[-1] if parts else None
