FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SLEEVE_DATA_DIR=/data \
    HF_HOME=/data/cache/huggingface \
    XDG_CACHE_HOME=/data/cache

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

RUN groupadd --gid 10001 sleeve \
    && useradd --uid 10001 --gid sleeve --create-home --shell /usr/sbin/nologin sleeve \
    && mkdir -p /data /music \
    && chown sleeve:sleeve /data /music

COPY --chown=sleeve:sleeve *.py ./
COPY --chown=sleeve:sleeve static ./static
COPY --chown=sleeve:sleeve templates ./templates

USER sleeve
EXPOSE 5000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "5000"]
