# Build and run the Ateles task dashboard.
#
# Two stages: the first installs dev dependencies and builds the SPA plus the
# server bundle; the second carries only production dependencies and the built
# output, so the runtime image has no toolchain in it.

# ── build ────────────────────────────────────────────────────────────────────
FROM node:22-slim AS build
WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# The commit being deployed, surfaced at /healthz so a deploy can be PROVEN to
# have shipped this code. Declared here rather than baked into the source
# because it changes on every build and must not invalidate the npm ci layer.
ARG DASHBOARD_GIT_SHA=""
ENV DASHBOARD_GIT_SHA=$DASHBOARD_GIT_SHA

RUN npm run build:deploy

# ── runtime ──────────────────────────────────────────────────────────────────
FROM node:22-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production

COPY package.json package-lock.json ./
# `jose` is the one runtime dependency the server needs (Google id_token
# verification); everything else it uses is in the Node standard library.
RUN npm ci --omit=dev && npm cache clean --force

COPY --from=build /app/dist ./dist
COPY --from=build /app/server-dist ./server-dist

ARG DASHBOARD_GIT_SHA=""
ENV DASHBOARD_GIT_SHA=$DASHBOARD_GIT_SHA
ENV PORT=8080
EXPOSE 8080

CMD ["node", "server-dist/serve.js"]
