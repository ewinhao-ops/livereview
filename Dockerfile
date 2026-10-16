FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PLAYWRIGHT_BROWSERS_PATH=/opt/browsers
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/livereview/backend
COPY backend/requirements.lock ./
COPY backend/app ./app
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
    && python -m playwright install --with-deps chromium
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./
COPY frontend /opt/livereview/frontend
RUN useradd --create-home --uid 10001 livereview \
    && mkdir -p data && chown -R livereview:livereview /opt/livereview /opt/browsers
USER livereview
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=3)"
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765", "--no-proxy-headers"]
