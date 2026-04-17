FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

WORKDIR /app

# System deps for scipy/sklearn wheels (most wheels have what they need, but
# libgomp is pulled in by sklearn on some base images).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

# model_state.pkl ships in the repo; _prime_state() on import will rebuild
# grids anyway. Just make the data volume target exists.
RUN mkdir -p /data && mkdir -p "/data/other datasets"

# Host sets $PORT (Fly: 8080, HF Spaces: 7860, Render: 10000, Railway: random).
# Default to 8080 for `docker run` without -e PORT.
ENV PORT=8080
EXPOSE 8080

# Shell form so $PORT is expanded at runtime. 1 worker — STATE is process-local
# module globals. 120 s timeout covers a worst-case 7k-row refit; `--preload`
# means RECORDS + STATE are built once and copied-on-write into the worker.
CMD gunicorn app:app \
    --bind "0.0.0.0:${PORT}" \
    --workers 1 \
    --threads 4 \
    --timeout 120 \
    --preload
