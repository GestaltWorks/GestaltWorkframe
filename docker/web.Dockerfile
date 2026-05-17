# Gestalt Workframe — Next.js static export served by nginx.
# Build context is the repository root: `docker build -f docker/web.Dockerfile .`

FROM node:22-alpine AS builder

WORKDIR /build

RUN corepack enable

COPY web/package.json web/pnpm-lock.yaml* web/pnpm-workspace.yaml* ./
# Static export with `images.unoptimized: true` needs no native postinstalls
# (sharp, unrs-resolver). Skipping lifecycle scripts keeps the image lean and
# sidesteps pnpm 10's ERR_PNPM_IGNORED_BUILDS gate.
RUN if [ -f pnpm-lock.yaml ]; then \
        pnpm install --frozen-lockfile --ignore-scripts; \
    else \
        pnpm install --ignore-scripts; \
    fi

COPY web/ ./
RUN pnpm build


FROM nginx:1.27-alpine AS runtime

RUN rm -f /etc/nginx/conf.d/default.conf
COPY docker/web-nginx.conf /etc/nginx/conf.d/web.conf
COPY --from=builder /build/out /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -qO- http://127.0.0.1/ >/dev/null 2>&1 || exit 1
