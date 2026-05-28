# Stage 1: install dependencies and build the Next.js application
FROM node:22-alpine AS builder

RUN corepack enable && corepack prepare pnpm@latest --activate

WORKDIR /build

# Copy manifests first so the layer is cached as long as dependencies don't change
COPY src/frontend/package.json src/frontend/pnpm-lock.yaml src/frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

# Copy application source and build
COPY src/frontend/ ./
RUN pnpm build

# Stage 2: minimal runtime image
FROM node:22-alpine AS runner

ENV NODE_ENV=production \
    PORT=3000 \
    HOSTNAME=0.0.0.0

WORKDIR /app

# The node user (uid 1000) ships with the node alpine image
RUN chown node:node /app
USER node

# Standalone server and its bundled node_modules
COPY --from=builder --chown=node:node /build/.next/standalone ./
# Static assets (/_next/static/...)
COPY --from=builder --chown=node:node /build/.next/static ./.next/static
# Public assets (/favicon.ico, etc.) — copy only if the directory exists
COPY --from=builder --chown=node:node /build/public ./public

EXPOSE 3000

CMD ["node", "server.js"]
