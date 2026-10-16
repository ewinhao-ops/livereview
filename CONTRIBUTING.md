# 参与贡献

欢迎提交问题报告和改进建议。先阅读 [README](README.md) 的适用范围与许可证；提交原创修改即表示你愿意按本项目许可证提供该修改，不包括第三方代码的重新授权。

## 本地检查

```bash
cd backend
uv sync --frozen --extra dev
uv run --no-sync pytest -q
cd ..
node --test frontend/tests/*.test.js
```

真实付费集成测试默认跳过。普通贡献不需要自己的 API key，也不要在测试中调用生产服务。

修改用户界面后，启动本地应用验证实际操作和手机宽度；截图只使用虚构样例。修改登录、数据归属或迁移逻辑时，请增加能验证行为的测试。

## 提交问题

说明你想完成什么、实际发生什么，并提供系统、安装方式和去敏后的错误片段。不要附 `.env`、Cookie、数据库、录制或签名链接。

## Pull Request

说明变更解决的问题、用户能观察到的结果，以及运行过的检查。涉及界面时提供脱敏截图。保持改动范围清楚，不顺便提交生成数据、依赖目录或个人配置。

更新 Python 依赖后同时更新锁文件：

```bash
cd backend
uv lock
cd ..
uv export --project backend --locked --no-dev --no-emit-project --format requirements-txt --output-file backend/requirements.lock
```

`backend/uv.lock` 用于源码安装，`backend/requirements.lock` 用于 Docker 的带哈希依赖安装。

## GitHub 自动检查

[检查模板](docs/ci/checks.yml) 包含 Linux 测试和 Docker 安装检查。启用时将它复制到 `.github/workflows/checks.yml`，并使用有 workflow 写权限的 GitHub 授权提交。模板未启用前，不应把本地通过的测试当成 GitHub Actions 已通过。
