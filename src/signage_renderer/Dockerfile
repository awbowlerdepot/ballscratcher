# Signage renderer (runbook 6cn): Chromium (Playwright) + ffmpeg in one
# Lambda container image. Playwright's official image ships Chromium and its
# system dependencies; arm64 so it builds natively on Apple Silicon and runs
# on Graviton.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /var/task
COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt
COPY app.py .

# Lambda's filesystem is read-only except /tmp; Chromium and fontconfig need
# a writable home.
ENV HOME=/tmp \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["python3", "-m", "awslambdaric"]
CMD ["app.handler"]
