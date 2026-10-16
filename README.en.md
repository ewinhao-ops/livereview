# LiveReview

![LiveReview](docs/assets/banner.svg)

A self-hosted, Chinese-language workspace for livestream review. Connect transcripts, comments, recorded media and structured feedback, then turn the review into practical actions for the next session.

[中文介绍](README.md) · [Installation](docs/INSTALL.md) · [Configuration](docs/CONFIGURATION.md) · [Security](SECURITY.md)

## Features

- Livestream capture, transcription and timestamped evidence.
- Per-sentence checks, segment reviews and session summaries.
- Multi-host attribution and an individual host workspace.
- Product knowledge, document import, reusable experience and rhythm templates.
- Optional action tracking, model usage accounting and Tencent COS storage.
- Independent password login; an optional Studio SSO adapter for compatible deployments.

Some rules originated in headphone product presentations. Adapt the knowledge base and prompts before using other categories. AI output requires human review.

## Run locally

Prerequisites: Git, Python 3, [uv](https://docs.astral.sh/uv/getting-started/installation/), and FFmpeg for capture. The backend uses Python 3.12, which uv can install.

```bash
git clone https://github.com/ewinhao-ops/livereview.git
cd livereview
python3 scripts/setup.py
cd backend
uv sync --frozen --no-dev
uv run --no-sync python -m playwright install chromium
uv run --no-sync uvicorn app.main:app --host 127.0.0.1 --port 8765 --no-proxy-headers
```

Open http://localhost:8765 and use the password you just chose. No API key is required to install or browse fictional demo records. To insert those records into an empty database, run `uv run --no-sync python ../scripts/seed_demo.py` from `backend/` before starting the server.

For real processing, configure your own transcription and LLM credentials in `backend/.env`. Paid API calls are billed to your provider accounts. `/api/ready` intentionally returns 503 until live-processing prerequisites are configured; it is not a paid end-to-end test.

Docker alternative, from the repository root:

```bash
python3 scripts/setup.py
docker compose up -d --build
```

The repository includes no working credentials, production database or production Git history. Application data stays in your installation, with relevant input sent to providers when cloud AI features are enabled. See [Security](SECURITY.md).

## License

Original code is **source-available for noncommercial use** under [PolyForm Noncommercial 1.0.0](LICENSE). Commercial use requires a separate written license. Third-party software keeps its own license. Contact [the maintainer](https://github.com/ewinhao-ops) for commercial licensing.
