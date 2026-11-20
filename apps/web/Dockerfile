# syntax=docker/dockerfile:1

# Build stage. Dev dependencies are needed here because `next build` runs the
# TypeScript compiler, and a type error must fail the image rather than ship.
FROM node:22-alpine AS builder

WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1

# Copied separately so a source-only change reuses the cached install layer.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .
RUN npm run build

# ---------------------------------------------------------------- runtime --
# `output: 'standalone'` emits a server.js with only the modules it actually
# imports, so the runtime image carries no node_modules tree and no npm.
FROM node:22-alpine AS runner

WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# Non-root. The image writes nothing at runtime; Next's cache lives in memory
# for a dynamic-only app.
RUN addgroup -g 1001 -S nodejs \
 && adduser -S -u 1001 -G nodejs nextjs

COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs

EXPOSE 3000

# Container Apps ingress is pinned to HTTP/1.1 (transport = "http") because
# this server does not speak h2c; see infra/apps.tf.
CMD ["node", "server.js"]
