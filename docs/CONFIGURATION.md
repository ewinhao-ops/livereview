# 模型与功能配置

所有服务端配置集中在 `backend/.env`。初始模板是根目录 `.env.example`。配置文件不会被 Git 提交，也不会进入 Docker 镜像；不要把真实值写进 README、截图、Issue 或前端。

修改环境配置后重启服务。部分开关也可由后台运行时设置覆盖，请同时检查界面中的设置。

## 先理解两种运行状态

**界面与演示：** 默认 `LLM_PROVIDER=mock`，允许本地查看虚构记录，但不会伪装成真实直播处理成功。真实开播会检查配置，缺少真实 ASR 或 LLM 时阻止开播。

**真实直播：** 当前实时转写使用 DashScope `paraformer-realtime-v2`，需要自己的转写 key；报告还需要配置一个可用的 LLM。转写与报告服务可以来自不同厂商。

## 最小真实配置

以下仅展示变量关系。把空的密钥填入本机 `.env`，并替换模型占位值；不要提交实际密钥。

```dotenv
ASR_PROVIDER=dashscope
ASR_MODEL=paraformer-realtime-v2
ALIYUN_DASHSCOPE_API_KEY=

# 一个兼容 OpenAI chat/completions 的端点
LLM_PROVIDER=openai
LLM_BASE_URL=https://your-provider.example/v1
LLM_MODEL=your-model-id
LLM_API_KEY=

LLM_MOCK_FALLBACK=false
```

`LLM_PROVIDER=openai` 映射到系统中的 `chatgpt` 兼容客户端。端点需支持此客户端调用的接口与所需输出格式，不能仅根据“兼容”两个字判断所有能力都可用。模型 ID、账户权限和费用以服务商当前文档为准。

## 内置厂商适配

| `LLM_PROVIDER` | 主要密钥 | 可指定的模型 |
| --- | --- | --- |
| `deepseek` | `DEEPSEEK_API_KEY` | `DEEPSEEK_MODEL`、`DEEPSEEK_FAST_MODEL` |
| `doubao` | `ARK_API_KEY` | `DOUBAO_MODEL`、`DOUBAO_FAST_MODEL` |
| `qwen` | `QWEN_API_KEY` | `QWEN_MODEL` |
| `hunyuan` | `HUNYUAN_API_KEY` | `HUNYUAN_MODEL` |
| `chatgpt` / `openai` | `OPENAI_API_KEY` 或 `LLM_API_KEY` | `OPENAI_MODEL` 或 `LLM_MODEL` |

`LLM_FAST_PROVIDER` 可给高频任务选择另一适配器，留空时沿用主模型。仅配置了 key 不代表模型可用，请做小规模实际验证。不要依赖代码里的默认模型名称永久有效。

画面识别通过 `OCR_PROVIDER` 选择 `mock`、`dashscope` 或 `deepseek`；真实画面能力取决于对应模型和账户权限。`mock` 的结果仅供开发演示。

## 费用相关开关

| 设置 | 发布版默认 | 用途 |
| --- | --- | --- |
| `AUTOPILOT_ENABLED` | `false` | 无人值守轮询并录制开播直播间 |
| `REALTIME_CORRECTION_ENABLED` | `false` | 高频逐句纠错 |
| `REALTIME_REVIEW_ENABLED` | `false` | 高频实时点评 |
| `EMOTION_CONTEXT_ENABLED` | `false` | 情绪和语境二次分析 |
| `AUTO_EMOTION` | `false` | 收播后的额外音频情绪分析 |
| `DANMAKU_ANSWER_ENABLED` | `false` | 弹幕 AI 回答 |
| `V2_AUTO_ENABLED` | `false` | 自动生成深度报告；仍可手动按需触发 |
| `FOCUS_ENABLED` | `false` | 自动跨场动作核对与补处理 |

这些开关关闭不代表真实直播完全免费：基础转写、分段点评与整场总结仍会使用所配置服务。模型失败不会自动用虚构结果替代真实报告。

`LLM_INPUT_PRICE_PER_MILLION` 和 `LLM_OUTPUT_PRICE_PER_MILLION` 用于按自己填入的单价估算 token 成本；默认 0 表示不估算金额，不代表供应商免费。ASR、OCR、存储、网络等费用需要在对应控制台另查。

启用动作追踪前，可设置 `FOCUS_SINCE=YYYY-MM-DD` 限定自动处理范围；不要在不清楚历史量时一次开启大量补处理。

## 录制与存储

- `DOUYIN_COOKIE`：仅当自己有权使用且平台解析需要时填写，按账号凭据保管。
- `RECORD_VIDEO=false`：默认只保留音频；改为 `true` 会增加存储和带宽使用。
- `MEDIA_RETENTION_DAYS=0`：不按天数自动删除；非 0 会启用到期清理，设置前先理解保留需求。
- `FFMPEG_EXECUTABLE`：默认 `ffmpeg`，也可写完整可执行文件路径。
- `COS_REGION`、`COS_BUCKET`、`COS_SECRET_ID`、`COS_SECRET_KEY`：全部配置后可使用自有腾讯云 COS；留空时媒体保存在本地。

默认 SQLite 数据保存在 `backend/data/replay.db`，媒体在 `backend/data/audio`。不要把此目录放到公开静态站点或 Git 仓库。

## 登录与可选集成

| 设置 | 说明 |
| --- | --- |
| `AUTH_MODE=password` | 默认独立运行；管理员使用自己设置的密码 |
| `ACCESS_PASSWORD` | 至少 12 位，无默认公共密码；修改后旧管理员 Cookie 失效 |
| `SECRET_KEY` | 签名密钥，由安装脚本随机生成 |
| `PUBLIC_BASE_URL` | 用户访问应用的地址；独立模式建议使用域名根路径 |
| `COOKIE_SECURE=true` | 公网 HTTPS 部署时启用；本地 HTTP 保持 `false` |
| `AUTH_MODE=studio` | 高级模式，需要自己的兼容 Studio 身份平台 |
| `STUDIO_JWT_SECRET`、`STUDIO_ME_URL`、`PLATFORM_URL` | 自有 SSO 平台配置，默认全部为空 |
| `SIGNAL_TOKEN` | 上下播信号接口使用的独立 token；空值拒绝 |
| `PARTNER_TOKEN` | 合作系统接口 token；空值拒绝，含管理接口，需谨慎授权 |
| `INTEGRATION_LOOPBACK_ONLY=true` | 默认把机器间集成限制在服务器本机回环 |

Studio 适配器预期 HS256 签名、`studio_token` Cookie，以及 `/api/me` 返回的账户状态和权限。它不是通用 OAuth 登录。未接入自己的平台时，请保持 `password` 模式。
