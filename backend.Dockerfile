FROM python:3.13.12-slim-bookworm@sha256:a58daefb915e1e03ad48f3ca4df8832065412c5c35cacb9d39f4229184de12b6

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock
COPY pyproject.toml /tmp/pyproject.toml
COPY src /app/src
COPY alembic.ini /app/alembic.ini
COPY migrations /app/migrations
ENV PYTHONPATH=/app/src
EXPOSE 8000
CMD ["uvicorn", "videoclip.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
