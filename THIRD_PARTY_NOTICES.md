# 第三方组件

本项目对原创代码的非商业许可证不改变第三方组件的许可证。

## 随仓库分发的浏览器库

| 组件 | 版本 | 许可证与来源 |
| --- | --- | --- |
| hls.js | 1.5.20 | [Apache-2.0](frontend/vendor/licenses/hls.js-LICENSE) · [上游](https://github.com/video-dev/hls.js/tree/v1.5.20) |
| mpegts.js | 1.7.3 | [Apache-2.0](frontend/vendor/licenses/mpegts.js-LICENSE) · [上游](https://github.com/xqq/mpegts.js/tree/v1.7.3) |

两者保留各自的版权和第三方依赖声明。hls.js 许可证文件还列有其包含的代码的声明；mpegts.js 分发文件保留了 es6-promise 的声明。

## 安装时获取的组件

Python 依赖通过 `backend/uv.lock` 和 `backend/requirements.lock` 锁定，包括 FastAPI、SQLAlchemy、Pydantic、Playwright、DashScope SDK、python-docx、pypdf 等。各组件及其传递依赖继续受各自许可证约束，完整元数据随安装包提供。

FFmpeg 和 Chromium 由安装流程取得，适用各自的许可证及分发条件。部署 Docker 镜像、重新分发二进制或修改这些组件时，请核对相应发行包的许可证。

品牌标记、横幅、流程图和界面主题为本项目制作；示例数据为虚构内容。
