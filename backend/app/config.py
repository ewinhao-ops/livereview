"""集中配置。密钥只从环境变量或本地 .env 读取；演示与真实服务显式区分。"""

from __future__ import annotations

import os
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 独立部署使用 password；接入自有办公平台时才切换 studio。
    auth_mode: Literal["password", "studio"] = "password"

    # 存储
    database_url: str = "sqlite:///./data/replay.db"
    audio_dir: str = "./data/audio"
    log_dir: str = "./data/logs"
    ffmpeg_executable: str = "ffmpeg"

    # 统一登录（2026-10-01 起后台改认 workspace.example.test 平台登录态，见 sso.py）：
    # STUDIO_JWT_SECRET 与 PAIOS/LiveReview/画布同一个值，本地验 studio_token 签名；留空=无法核验，后台接口一律 503
    studio_jwt_secret: str = ""
    studio_me_url: str = ""   # studio 账号实时状态与权限（本机回环）
    platform_url: str = ""           # 平台登录 / 退出 / 申请开通页所在站点
    # 本系统对外地址（含 /fupan 前缀）：罗盘脚本上报地址等对外生成的链接都从这里拼，换域名只改这一项
    public_base_url: str = "http://localhost:8765"

    # 本机应急口令：只认服务器本机直连 8077 的请求（SSH 隧道），经 Caddy 来的公网请求一律拒绝。
    # 必须在 .env 设 ACCESS_PASSWORD，没有代码默认值；留空 = 应急入口关闭
    access_password: str = ""
    # 鉴权增强：cookie 签名密钥（留空自动生成并持久化）、HTTPS-only cookie、CORS 白名单（逗号分隔）
    secret_key: str = ""
    cookie_secure: bool = False
    cors_origins: str = ""

    # 真实直播唯一允许 DashScope Paraformer；mock 仅供无真实流的本地测试场景。
    asr_provider: str = "dashscope"
    ocr_provider: str = "mock"  # mock | dashscope | deepseek（DeepSeek 官方 vision）
    llm_provider: str = "mock"  # mock | doubao | hunyuan | qwen | deepseek | chatgpt
    # 快速档（逐句纠错/实时质检/分段点评/弹幕答疑等高频调用）单独指定 provider；留空=同 LLM_PROVIDER。
    # 2026-09-27 起：主力档方舟豆包，快速档 DeepSeek 官方（LLM_FAST_PROVIDER=deepseek）。
    llm_fast_provider: str = ""
    # 真实运行默认禁止模型失败后伪造 Mock 点评；仅单元测试可显式开启。
    llm_mock_fallback: bool = False

    # 成本控制：高频实时点评默认关闭，实时规则检测仍会执行。
    realtime_correction_enabled: bool = False
    realtime_review_enabled: bool = False
    # 段点评按 20 句或 segment_interval_sec 触发，先到者触发。
    segment_max_sentences: int = 20
    # 情绪与语境二次匹配默认关闭，避免和段点评重复请求。
    emotion_context_enabled: bool = False
    # 弹幕 AI 答疑默认关闭；打开后仍受会话次数和冷却限制。
    danmaku_answer_enabled: bool = False
    danmaku_answer_max_per_session: int = 20
    danmaku_answer_cooldown_sec: int = 30
    # 深度 v2 由用户按需触发，避免基础报告完成后自动产生多次深度请求。
    v2_auto_enabled: bool = False
    # 深度 v2 各块并行时，同时在跑的深度模型请求数上限（全进程共用，1~8）。调大更快，但更容易撞限流。
    v2_llm_concurrency: int = 4
    # 2026-09-29 上线（2026-09-27 用七月真实场次做过新旧对比）：深度档（下播总结/深度复盘/
    # 罗盘归因）单次请求等待上限，后台任务可放宽，避免长思考被截断。默认 180 与改造前一致。
    llm_deep_timeout_sec: int = 180
    # 豆包深度档思考强度：留空=模型默认（最长）；可选 minimal / low / medium / high。
    doubao_reasoning_effort: str = ""
    # 口播/画面价格与知识库官方价比对：直播间价格随活动变动，官网价不作依据，默认关闭
    # （2026-07-15 用户规则：价格不做质检，只报破价；2026-09-29 用户重申）。
    official_price_check_enabled: bool = False
    # 改进动作闭环（2026-10 第一期，app/core/review/focus.py）：下播出完整场总结后，
    # 用快速档核对这位主播在练的动作，再把本场能从逐字稿检查的问题都变成动作（不设条数上限）；
    # 关掉后下播不再处理、补漏循环也停（接口照常可读写）。
    focus_enabled: bool = False
    # 补漏循环只处理这一天（含）及以后开播的场次，格式 YYYY-MM-DD
    focus_since: str = ""
    # 价格按“每百万 token”填写；为 0 时仍记录 token，不计算金额。
    llm_input_price_per_million: float = 0.0
    llm_output_price_per_million: float = 0.0

    # 通用 OpenAI 兼容 LLM（豆包/Moonshine/任意兼容端点：填这三项 + LLM_PROVIDER=openai）
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""

    # 四个主力 LLM provider。四组配置可同时保留，切换时只需修改 LLM_PROVIDER。
    # base_url/model 留空时使用 app/core/llm.py 中的官方默认值。
    doubao_base_url: str = ""
    doubao_model: str = ""
    doubao_fast_model: str = ""
    hunyuan_api_key: str = ""
    hunyuan_base_url: str = ""
    hunyuan_model: str = ""
    qwen_api_key: str = ""
    qwen_base_url: str = ""
    qwen_model: str = ""
    openai_api_key: str = ""
    openai_base_url: str = ""
    openai_model: str = ""
    # DeepSeek 官方 API（密钥为下方 deepseek_api_key）：深/快两档模型分开配，留空回退 LLM_MODEL
    deepseek_base_url: str = ""
    deepseek_model: str = ""
    deepseek_fast_model: str = ""

    # DashScope ASR/OCR 模型
    asr_model: str = "paraformer-realtime-v2"
    ocr_model: str = "qwen-vl-max"
    # 收播后自动用 qwen3-asr-flash 精算段级情绪（需配 aliyun_dashscope_api_key）
    auto_emotion: bool = False

    # 采集 / 点评节奏
    frame_interval_sec: int = 3
    segment_interval_sec: int = 90

    # 无人值守：后台轮询 auto_record 直播间，检测到开播自动录制
    autopilot_enabled: bool = False
    autopilot_interval_sec: int = 60
    # 视频/音频保留天数（0=永久保留）；到期自动删 COS+本地并标记 deleted，控制存储成本
    media_retention_days: int = 0

    # 业绩统计系统对接：主播上/下播信号推送的鉴权 token（空=拒绝所有信号）
    signal_token: str = ""
    # 业绩系统读取复盘数据的只读 token（partner 接口用，与 signal_token 分离；空=拒绝）
    partner_token: str = ""
    # 信号行为开关（默认全关=只记审计不改行为；autostart/off_mode/grace 可经 SystemSetting 热切）
    # 全部班次录制：任何上播信号都登记开录意向（不看「我要复盘」），可被 system_settings 覆盖
    signal_record_all: bool = False
    signal_autostart_enabled: bool = False   # 上播信号驱动自动开录（阶段2 实装）
    signal_off_mode: str = "ignore"          # ignore=只记录 | close_turn=结班 | stop=结班+宽限停录
    signal_stop_grace_sec: int = 300         # stop 档宽限秒数（到期先探活，流还活着不停）
    signal_pending_ttl_sec: int = 7200       # 上播意向有效期（打卡后一直没开播则作废）
    signal_fast_poll_sec: int = 8            # 有意向时 autopilot 的加速轮询间隔
    signal_auto_create_streamer: bool = True  # 花名册没有该主播时自动建档（保归属不丢）
    integration_loopback_only: bool = True   # signal/partner 仅接受本机回环调用（挡公网）

    # 凭证（按所选 provider 填）
    aliyun_dashscope_api_key: str = ""
    volcano_access_key: str = ""
    volcano_secret_key: str = ""
    xunfei_app_id: str = ""
    xunfei_api_key: str = ""
    tencent_secret_id: str = ""
    tencent_secret_key: str = ""
    deepseek_api_key: str = ""
    # 第二个 DeepSeek 账户的密钥（2026-09-29）：现用密钥调用失败（如余额用完）时自动改用
    deepseek_api_key_backup: str = ""
    ark_api_key: str = ""       # 火山方舟（豆包）API Key，2026-07-14 起为 LLM 主力
    anthropic_api_key: str = ""

    # 抖音解析
    douyin_cookie: str = ""
    douyin_ua: str = ""   # 留空用内置 UA；反爬升级时可在 .env 覆盖
    playwright_chromium_executable_path: str = ""

    # 录像 + COS 媒体存储（视频/音频存对象存储，服务器盘不涨）
    record_video: bool = False           # True 录高清视频 mp4，否则只录音频 m4a
    cos_region: str = ""
    cos_bucket: str = ""
    cos_secret_id: str = ""
    cos_secret_key: str = ""

    @property
    def cos_enabled(self) -> bool:
        return bool(self.cos_bucket and self.cos_region
                    and self.cos_secret_id and self.cos_secret_key)

    @property
    def audio_path(self) -> Path:
        p = (BACKEND_ROOT / self.audio_dir).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def log_path(self) -> Path:
        p = (BACKEND_ROOT / self.log_dir).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    # 允许本地用绝对路径配置 winget/便携版 FFmpeg。只修改当前服务进程 PATH，
    # 因而所有既有 ffmpeg/ffprobe 子进程调用无需各自拼接私密内部路径。
    configured = (settings.ffmpeg_executable or "").strip()
    if configured:
        executable = Path(configured).expanduser()
        try:
            if executable.is_file():
                directory = str(executable.resolve().parent)
                path_parts = os.environ.get("PATH", "").split(os.pathsep)
                if directory.lower() not in {part.lower() for part in path_parts if part}:
                    os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")
        except OSError:
            # 就绪接口会报告不可执行；配置读取本身不能因此阻断整个应用启动。
            pass
    return settings


def public_url(path: str) -> str:
    """本系统对外地址 + 站内路径（PUBLIC_BASE_URL 已含 /fupan 前缀），如罗盘脚本的上报地址。"""
    return get_settings().public_base_url.strip().rstrip("/") + path


@lru_cache
def get_secret() -> str:
    """cookie 签名密钥：优先用 SECRET_KEY，否则在 data/.secret_key 生成并复用（重启后 cookie 不失效）。"""
    s = get_settings()
    if s.secret_key:
        return s.secret_key
    f = BACKEND_ROOT / "data" / ".secret_key"
    f.parent.mkdir(parents=True, exist_ok=True)
    if f.exists():
        txt = f.read_text().strip()
        if txt:
            return txt
    val = secrets.token_hex(32)
    f.write_text(val)
    return val
