"""COS 对象存储封装：把录像/录音上传到腾讯云 COS，播放时给临时签名 URL。

私有桶 + 临时签名 URL：既安全又不占服务器盘。未配置 COS 时全部为空操作，
调用方回退到本地文件（服务器盘）。SDK 懒加载，未装也不影响其他功能。
"""

from __future__ import annotations

from pathlib import Path

from ..config import get_settings
from .safe_error import safe_error_text, safe_exception_code


def _log_failure(operation: str, exc: BaseException) -> None:
    print(
        f"[cos] operation={operation}; "
        f"code={safe_exception_code(exc, prefix='COS')}; "
        f"error={safe_error_text(exc)}"
    )


def enabled() -> bool:
    return get_settings().cos_enabled


def _client():
    from qcloud_cos import CosConfig, CosS3Client
    s = get_settings()
    return CosS3Client(CosConfig(
        Region=s.cos_region, SecretId=s.cos_secret_id,
        SecretKey=s.cos_secret_key, Scheme="https",
    ))


def upload_file(local_path: str | Path, key: str) -> bool:
    """把本地文件传到 COS 的 key。成功返回 True。"""
    if not enabled():
        return False
    try:
        _client().upload_file(Bucket=get_settings().cos_bucket, Key=key,
                              LocalFilePath=str(local_path))
        return True
    except Exception as e:  # noqa: BLE001
        _log_failure("upload", e)
        return False


def presigned_url(key: str, expires: int = 3600, filename: str | None = None) -> str | None:
    """生成临时可播放/下载的签名 URL（私有桶也能访问）。

    传 filename 时附加 response-content-disposition，让浏览器按可读名下载（下载用）；
    不传则内联，可直接播放（工作台回放用）。
    """
    if not enabled() or not key:
        return None
    params = None
    if filename:
        from urllib.parse import quote
        params = {"response-content-disposition": f"attachment; filename*=UTF-8''{quote(filename)}"}
    try:
        return _client().get_presigned_url(
            Method="GET", Bucket=get_settings().cos_bucket, Key=key,
            Expired=expires, Params=params)
    except Exception as e:  # noqa: BLE001
        _log_failure("presign", e)
        return None


def delete(key: str) -> None:
    if not enabled() or not key:
        return
    try:
        _client().delete_object(Bucket=get_settings().cos_bucket, Key=key)
    except Exception as e:  # noqa: BLE001
        _log_failure("delete", e)


def get_text(key: str) -> str | None:
    """读取 COS 上的小文本对象（如 HLS 索引 m3u8）。"""
    if not enabled() or not key:
        return None
    try:
        r = _client().get_object(Bucket=get_settings().cos_bucket, Key=key)
        return r["Body"].get_raw_stream().read().decode("utf-8")
    except Exception as e:  # noqa: BLE001
        _log_failure("read", e)
        return None


def delete_prefix(prefix: str) -> int:
    """删除某前缀下全部对象（HLS 场次目录清理），返回删除数。"""
    if not enabled() or not prefix:
        return 0
    n = 0
    try:
        c = _client()
        bucket = get_settings().cos_bucket
        marker = ""
        while True:
            resp = c.list_objects(Bucket=bucket, Prefix=prefix, Marker=marker, MaxKeys=500)
            items = resp.get("Contents") or []
            if items:
                c.delete_objects(Bucket=bucket, Delete={
                    "Object": [{"Key": it["Key"]} for it in items], "Quiet": "true"})
                n += len(items)
            if resp.get("IsTruncated") == "true":
                marker = resp.get("NextMarker", "")
            else:
                break
    except Exception as e:  # noqa: BLE001
        _log_failure("delete_prefix", e)
    return n


def rewrite_hls_playlist(m3u8_key: str, expires: int = 14400,
                         per_segment_discontinuity: bool = False) -> str | None:
    """把 COS 上的 m3u8 重写为逐切片签名 URL 的可播放文本（私有桶方案）。
    签名为本地 HMAC 计算，千级切片毫秒完成。

    per_segment_discontinuity：存量场次补救——2026-07-07 前录制的切片用了
    reset_timestamps（每片时间戳归零），必须每片前插 #EXT-X-DISCONTINUITY
    声明时间轴重置，否则播放器音画错乱（声音拖慢/卡死/无声）。新录制勿开。"""
    text = get_text(m3u8_key)
    if text is None:
        return None
    # 时间戳已修复的场次(refit_ts 重排后带此标记)无需再插 DISCONTINUITY
    if "#X-TS-CONTINUOUS" in text:
        per_segment_discontinuity = False
    prefix = m3u8_key.rsplit("/", 1)[0]
    out = []
    first_seg = True
    for ln in text.splitlines():
        s = ln.strip()
        if s and not s.startswith("#"):
            if per_segment_discontinuity and not first_seg:
                # 已有的轮次 DISCONTINUITY 不重复插
                if out and out[-1].strip() != "#EXT-X-DISCONTINUITY" and not (
                        len(out) >= 2 and out[-2].strip() == "#EXT-X-DISCONTINUITY"):
                    # EXTINF 行在切片行上一行，DISCONTINUITY 要插在 EXTINF 之前
                    if out and out[-1].startswith("#EXTINF"):
                        out.insert(len(out) - 1, "#EXT-X-DISCONTINUITY")
                    else:
                        out.append("#EXT-X-DISCONTINUITY")
            first_seg = False
            url = presigned_url(f"{prefix}/{s}", expires=expires)
            out.append(url or s)
        else:
            out.append(ln)
    return "\n".join(out)


def exists(key: str) -> bool:
    if not enabled() or not key:
        return False
    try:
        return bool(_client().object_exists(Bucket=get_settings().cos_bucket, Key=key))
    except Exception:  # noqa: BLE001
        return False


def count_prefix(prefix: str) -> int:
    """数某前缀下 COS 实际对象数（收尾核对切片是否真落云用）。未启用/异常返回 -1。"""
    if not enabled() or not prefix:
        return -1
    try:
        c = _client()
        bucket = get_settings().cos_bucket
        n = 0
        marker = ""
        while True:
            resp = c.list_objects(Bucket=bucket, Prefix=prefix, Marker=marker, MaxKeys=1000)
            n += len(resp.get("Contents") or [])
            if resp.get("IsTruncated") == "true":
                marker = resp.get("NextMarker", "")
            else:
                break
        return n
    except Exception as e:  # noqa: BLE001
        _log_failure("count_prefix", e)
        return -1


def audio_track_key(af, session_id: int) -> str | None:
    """媒体源为 HLS（cos_key=.m3u8）时，伴随音频文件的约定 key；单文件模式返回 cos_key 本身。"""
    key = (getattr(af, "cos_key", "") or "")
    if not key:
        return None
    if key.endswith(".m3u8"):
        ak = f"replay/session_{session_id}.m4a"
        return ak if exists(ak) else None
    return key
