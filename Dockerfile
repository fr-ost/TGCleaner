# TG Cleaner service, hosted mode (Railway or any Docker host).
# Configuration comes from environment variables; see README "Deploy on Railway".
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TGC_DATA_DIR=/data

WORKDIR /app
COPY service/requirements.txt ./
RUN pip install -r requirements.txt
COPY service/tgcleaner ./tgcleaner

# The Telegram session and scan cache live here: mount a persistent volume at /data.
RUN mkdir -p /data && chmod 700 /data

CMD ["python", "-m", "tgcleaner", "serve", "--cloud"]
