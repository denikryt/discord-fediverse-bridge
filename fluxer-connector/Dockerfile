# syntax=docker/dockerfile:1
# A checked-in npm lockfile is required for a reproducible official SDK build.
# Missing or inconsistent locks fail the build before installing the SDK.
FROM node:22-bookworm-slim AS dependencies
WORKDIR /app/fluxer-connector
COPY fluxer-connector/package.json fluxer-connector/package-lock.json ./
COPY fluxer-connector/scripts ./scripts
RUN npm run check:lock && npm ci --ignore-scripts --no-audit --no-fund
COPY fluxer-connector/tsconfig.json ./
COPY fluxer-connector/src ./src
COPY fluxer-connector/tests ./tests
RUN npm run check && npm test && npm prune --omit=dev

FROM node:22-bookworm-slim AS runtime
ARG APP_VERSION=dev
LABEL org.opencontainers.image.title="discord-fediverse-bridge-fluxer" \
      org.opencontainers.image.version="$APP_VERSION"
ENV NODE_ENV=production
WORKDIR /app/fluxer-connector
COPY --from=dependencies /app/fluxer-connector/node_modules ./node_modules
COPY fluxer-connector/package.json ./package.json
COPY fluxer-connector/src ./src
# No bot token is baked into the image. The Compose secret is Node-only.
USER node
CMD ["node", "--experimental-strip-types", "src/main.ts"]
