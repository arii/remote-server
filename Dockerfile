# buttons — S5 control console (FastAPI backend + Vite SPA, single image).
# Built on the S5 at deploy time (docker compose up -d --build buttons);
# no GHCR push. Runs as root: it needs the Docker socket (root-equivalent,
# same trust level as the terminal container). Never publish its port —
# ingress is via oauth2-proxy -> caddy only.

# ---- SPA build (pnpm workspace: single root lockfile) ----
FROM node:22-alpine AS spa
WORKDIR /build
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY frontend/package.json ./frontend/
RUN npm install -g "pnpm@10.28.2" \
 && pnpm install --frozen-lockfile
COPY frontend/ ./frontend/
RUN VITE_BASE=/buttons/ pnpm --filter frontend build
# Repair syntax gate (issue #488): the worker validates .ts/.tsx via
# the repo's own pinned TypeScript. Stage the compiler lib for the
# runtime image (-L: pnpm installs are symlinked).
RUN mkdir -p /tscheck && cp -rL /build/frontend/node_modules/typescript/lib /tscheck/lib

# ---- Runtime ----
FROM python:3.11-slim
ARG BOOMTICK_CLI_REF=main
ARG DOCKER_VERSION=27.4.1
ARG COMPOSE_VERSION=v2.32.4
RUN apt-get update \
 && apt-get install -y --no-install-recommends git gh curl ca-certificates openssh-client nodejs \
 && node --version \
 && echo "node available for repair syntax gate (issue #488)" \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /home/ari \
 && ln -sfn /root/.ssh /home/ari/.ssh \
 && git config --system --add safe.directory '*' \
 && echo "mounted checkouts trusted (single-user box)" \
 && mkdir -p /var/lib/buttons/deep-agent \
 && echo "deep-agent state dir ready (persisted volume)" \
 && curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_VERSION}.tgz" \
  | tar -xz -C /usr/local/bin --strip-components=1 docker/docker \
 && mkdir -p /usr/local/libexec/docker/cli-plugins \
 && curl -fsSL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-x86_64" \
  -o /usr/local/libexec/docker/cli-plugins/docker-compose \
 && chmod +x /usr/local/libexec/docker/cli-plugins/docker-compose \
 && HOME=/nonexistent docker compose version > /dev/null \
 && echo "compose plugin resolves HOME-independently" \
 && python3 -m venv /opt/venvs/td \
 && /opt/venvs/td/bin/pip install --no-cache-dir \
  "git+https://github.com/arii/boomtick.git@${BOOMTICK_CLI_REF}#subdirectory=cli" \
 && /opt/venvs/td/bin/td --help > /dev/null
ENV PATH="/opt/venvs/td/bin:${PATH}"
WORKDIR /app
COPY backend/requirements.txt ./backend-requirements.txt
COPY deep-agent/requirements.txt ./deep-agent-requirements.txt
# Backend runs ON the td venv (single python): boomtick's agent stack and
# our server share it. Absolute pip path: never depend on PATH order.
RUN /opt/venvs/td/bin/pip install --no-cache-dir -r backend-requirements.txt -r deep-agent-requirements.txt
COPY backend/ ./backend/
COPY deep-agent/ ./deep-agent/
COPY core_cli/ ./core_cli/
COPY --from=spa /build/frontend/dist ./frontend/dist/
COPY --from=spa /tscheck/lib /opt/tscheck/lib
ENV TS_LIB_DIR=/opt/tscheck/lib
# Build-time smoke: CLI intact after our pins, server + agent imports resolve.
RUN git config --system --get-all safe.directory | grep -qF '*' \
 && /opt/venvs/td/bin/td --help > /dev/null \
 && /opt/venvs/td/bin/python -c "import fastapi, uvicorn, httpx, langchain; print('deps ok')" \
 && /opt/venvs/td/bin/python -c "import backend.server; print('server imports ok')" \
 && /opt/venvs/td/bin/python /app/core_cli/briefing_summary.py 2>&1 | grep -q "usage:" \
 && echo "briefing script imports ok"
# Syntax-gate smoke (issue #488): bad .ts must be INVALID (brackets
# alone would pass it, so INVALID proves the real toolchain ran).
RUN printf 'const x: = 3;\n' > /tmp/gate-bad.ts \
 && printf 'const x: number = 3;\n' > /tmp/gate-good.ts \
 && /opt/venvs/td/bin/python /app/core_cli/ast_validator.py /tmp/gate-bad.ts 2>&1 | grep -q "^INVALID:" \
 && /opt/venvs/td/bin/python /app/core_cli/ast_validator.py /tmp/gate-good.ts | grep -q "^VALID:" \
 && rm /tmp/gate-bad.ts /tmp/gate-good.ts \
 && echo "ts syntax gate live"
# Agent runtime = same venv (chat spawn inherits the langchain stack).
# Repo state (git status, compose file, .env) resolves to the mounted
# live checkout, not the baked code.
ENV DEEP_AGENT_VENV=/opt/venvs/td/bin/python \
    BUTTONS_STACK=/home/ari/remote-server \
    DEEP_AGENT_STATE_DIR=/var/lib/buttons/deep-agent
EXPOSE 8000
CMD ["/opt/venvs/td/bin/python", "-m", "uvicorn", "backend.server:app", "--host", "0.0.0.0", "--port", "8000"]
