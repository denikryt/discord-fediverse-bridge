# syntax=docker/dockerfile:1
FROM python:3.12-slim AS runtime

ARG APP_VERSION=dev
LABEL org.opencontainers.image.title="discord-fediverse-bridge" \
      org.opencontainers.image.version="$APP_VERSION"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/vendor/discordops

WORKDIR /app

COPY pyproject.toml VERSION LICENSE ./
COPY vendor/discordops ./vendor/discordops
COPY src ./src
COPY web ./web

RUN python -m pip install --no-cache-dir . ./vendor/discordops \
    && groupadd --gid 10001 bridge \
    && useradd --uid 10001 --gid bridge --home-dir /app --create-home bridge \
    && mkdir -p /data \
    && chown -R bridge:bridge /app /data

USER bridge
VOLUME ["/data"]
EXPOSE 8080
CMD ["python", "-m", "src.app"]
