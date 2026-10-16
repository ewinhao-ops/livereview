# 安装和部署

LiveReview 可以独立运行，不依赖维护者的办公平台、服务器或账号。首次安装请先完成本地登录与演示，再配置自己的模型服务。

## 选择安装方式

| 方式 | 适合谁 | 需要准备 |
| --- | --- | --- |
| 源码运行 | 本地试用、开发、修改 | Git、Python 3、uv、FFmpeg |
| Docker Compose | Linux 服务器、希望隔离依赖 | Git、Python 3（配置脚本）、Docker 和 Compose |

建议使用 Python 3.12；uv 可自动下载后端使用的 Python。前端由后端直接提供，不需要单独构建。少量示例数据占用很小，真实录制的空间和并发需求取决于视频清晰度、直播时长和保留策略。

## macOS 和 Linux 源码安装

1. 按 [uv 官方说明](https://docs.astral.sh/uv/getting-started/installation/) 安装 uv。已有 Python 时，也可运行 `python3 -m pip install uv`。
2. 安装 FFmpeg。macOS 的 Homebrew 用户可执行 `brew install ffmpeg`；Debian / Ubuntu 可执行 `sudo apt-get update && sudo apt-get install -y ffmpeg`。
3. 下载和配置：

```bash
git clone https://github.com/ewinhao-ops/livereview.git
cd livereview
python3 scripts/setup.py
cd backend
uv sync --frozen --no-dev
```

4. 安装 Chromium。macOS：

```bash
uv run --no-sync python -m playwright install chromium
```

Linux 还需要浏览器系统依赖，使用：

```bash
uv run --no-sync python -m playwright install --with-deps chromium
```

5. 在 `backend` 目录启动：

```bash
uv run --no-sync uvicorn app.main:app --host 127.0.0.1 --port 8765 --no-proxy-headers
```

浏览器打开 [http://localhost:8765](http://localhost:8765)，输入自己设置的管理员密码。终端保持运行；按 Ctrl+C 停止服务。

## Windows 源码安装

安装 Git for Windows、Python 和 uv，并从 [FFmpeg 官网链接](https://ffmpeg.org/download.html) 获取 Windows 发行版。确保新开的 PowerShell 能运行 `git --version`、`uv --version` 和 `ffmpeg -version`。

```powershell
git clone https://github.com/ewinhao-ops/livereview.git
cd livereview
py scripts/setup.py
cd backend
uv sync --frozen --no-dev
uv run --no-sync python -m playwright install chromium
uv run --no-sync uvicorn app.main:app --host 127.0.0.1 --port 8765 --no-proxy-headers
```

如果 FFmpeg 没有加入 PATH，在 `backend/.env` 中设置 `FFMPEG_EXECUTABLE=C:/tools/ffmpeg/bin/ffmpeg.exe`，并确保同目录包含 `ffprobe.exe`。端口被占用时可改为 8766，同时修改 `PUBLIC_BASE_URL`。

Windows 文档提供对应命令；发布前的交互验收在 macOS 完成，Windows 原生环境仍需自行验证。

## 看演示数据

停止当前服务，在 `backend` 目录执行：

```bash
uv run --no-sync python ../scripts/seed_demo.py
```

重新启动后可看到两位虚构主播、四场复盘、逐字稿与报告。该命令不会访问直播平台或模型。数据库已经存在主播、知识条目或场次时，它会拒绝写入，避免污染业务记录。

演示账号仅为主播花名册，不含可登录的主播密码。管理员可在主播管理中自行开通主播账号。演示记录没有真实音视频，因此显示“录音不可用”是正常的。

## Docker Compose

本版本已实际验证 macOS 源码安装；Docker 文件已提供，但本次未执行镜像构建验收。服务器部署前请在自己的环境先运行下面的检查。

在仓库根目录运行：

```bash
python3 scripts/setup.py
docker compose up -d --build
docker compose ps
python3 scripts/smoke_test.py
```

打开 [http://localhost:8765](http://localhost:8765)。镜像安装锁定的 Python 依赖、FFmpeg 和 Chromium，应用以非 root 用户运行。首次下载浏览器可能较慢。

- 本机端口：`127.0.0.1:8765`。
- 数据：`livereview-data` 命名卷，包含数据库、音频和运行日志。
- 配置：宿主机 `backend/.env`，不会打包进镜像。
- 看日志：`docker compose logs --tail=100 -f`。
- 停止：`docker compose stop`；恢复：`docker compose start`。
- 改配置后生效：`docker compose up -d --force-recreate`。

不要运行 `docker compose down -v` 来做日常停止；`-v` 会删除数据卷。

## 第一次接真实直播

1. 在主播管理里建立主播档案。
2. 用自己的商品事实替换内置虚构示例，检查讲解规则。
3. 在 `backend/.env` 配置转写和大模型，参考 [配置说明](CONFIGURATION.md)，然后重启。
4. 打开“实时复盘”，查看依赖检查，填写自己有权录制的直播链接。
5. 先做一次短录制，核对真实转写、报告、音视频和主播归属，再决定是否启用自动录制。

## 放到公网

仓库公开不等于把运行中的后台开放给所有人。建议用 HTTPS 反向代理访问后台，保留应用密码保护，后端端口继续只监听本机。

使用自有域名，设置 DNS 指向自己的服务器。例如采用 Caddy：

```caddyfile
review.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

修改 `backend/.env`：

```dotenv
PUBLIC_BASE_URL=https://review.example.com
COOKIE_SECURE=true
```

重新启动服务。独立密码模式建议部署在域名根路径；可选 Studio 集成是单独的高级配置。Caddy 和证书的完整安装见 [Caddy 官方文档](https://caddyserver.com/docs/quick-starts/reverse-proxy)。

默认不开跨域。不要为了排错把 `CORS_ORIGINS` 设成任意来源，也不要把 Cookie 或 API key 放进前端代码。多人需要不同管理权限时，应接入自己的身份系统；独立模式的管理员密码是共享管理员身份。

## 备份与升级

源码安装请先停止服务，再完整备份 `backend/data`。SQLite 的数据库及 WAL/SHM 文件应作为同一份数据保存，避免只复制主文件遗漏尚未合并的内容。`backend/.env` 另行安全保存，备份目录不要公开上传。

Docker 可在停止后复制数据：

```bash
docker compose stop
mkdir -p backups
docker compose cp livereview:/opt/livereview/backend/data ./backups/data-copy
docker compose start
```

先备份再升级：

```bash
git pull --ff-only
# 源码安装：停止服务，在 backend/ 运行
uv sync --frozen --no-dev
# 然后用原命令启动
```

Docker 升级使用 `docker compose up -d --build`。本地 `.env` 和数据卷会保留。数据库初始化与兼容字段迁移在启动时执行；升级前先阅读 [更新记录](../CHANGELOG.md)，不要直接套用到其他私有系统的数据库。

## 判断是否安装成功

| 检查 | 含义 |
| --- | --- |
| 首页可打开 | 静态界面和后端可访问 |
| 正确密码登录、退出后再次受保护 | 独立认证可用 |
| 演示记录和逐字稿可读取 | 数据库和复盘读取路径可用 |
| `/api/ready` 通过 | 本地依赖和所需配置齐全，未验证账户权限、余额或实际效果 |
| 真实短录制得到准确转写与报告 | 才算自己的直播和模型链路得到实际验证 |

安装过程和演示模式不会产生模型费用。调用真实转写、图像理解、点评和文档向量化可能产生费用，请在自己的服务商控制台检查计费。
