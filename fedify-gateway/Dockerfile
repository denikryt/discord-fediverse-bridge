# syntax=docker/dockerfile:1
FROM node:22-bookworm-slim AS dependencies
WORKDIR /app/fedify-gateway
COPY fedify-gateway/package.json fedify-gateway/package-lock.json ./
RUN npm ci
COPY fedify-gateway/tsconfig.json ./tsconfig.json
COPY fedify-gateway/src ./src
RUN npm run check

FROM node:22-bookworm-slim AS runtime
ARG APP_VERSION=dev
LABEL org.opencontainers.image.title="discord-fediverse-bridge-fedify-gateway" \
      org.opencontainers.image.version="$APP_VERSION"

ENV NODE_ENV=production
WORKDIR /app
COPY VERSION ./VERSION
COPY --from=dependencies /app/fedify-gateway/node_modules ./fedify-gateway/node_modules
COPY fedify-gateway/package.json fedify-gateway/package-lock.json ./fedify-gateway/
COPY fedify-gateway/src ./fedify-gateway/src
RUN groupadd --gid 10001 bridge \
    && useradd --uid 10001 --gid bridge --home-dir /app --create-home bridge \
    && mkdir -p /data \
    && chown -R bridge:bridge /app /data

USER bridge
WORKDIR /app/fedify-gateway
VOLUME ["/data"]
EXPOSE 3000
CMD ["node", "./node_modules/tsx/dist/cli.mjs", "src/server.ts"]
