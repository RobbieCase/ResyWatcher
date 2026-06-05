FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# Railway will inject env vars (DISCORD_TOKEN, etc.). DATA_PATH defaults to
# /data/store.json — mount a persistent volume to /data on Railway.
RUN mkdir -p /data
ENV DATA_PATH=/data/store.json

CMD ["python", "-m", "app.main"]
