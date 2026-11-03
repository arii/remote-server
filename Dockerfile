# buttons — S5 control console (FastAPI backend + Vite SPA, single image).
# Built on the S5 at deploy time (docker compose up -d --build buttons);
# no GHCR push. Runs as root: it needs the Docker socket (root-equivalent,
# same trust level as the terminal container). Never publish its port —
# ingress is via oauth2-proxy -> caddy only.

# ---- SPA build ----
FROM node:22-alpine AS spa
WORKDIR /build/frontend
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN npm install -g "pnpm@10.28.2" \
 && printf 'onlyBuiltDependencies:\n  - esbuild\n' > pnpm-workspace.yaml \
 && pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

# ---- Runtime ----
FROM python:3.11-slim
ARG BOOMTICK_CLI_REF=main
ARG DOCKER_VERSION=27.4.1
ARG COMPOSE_VERSION=v2.32.4
RUN apt-get update \
 && apt-get install -y --no-install-recommends git gh curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_VERSION}.tgz" \
  | tar -xz -C /usr/local/bin --strip-components=1 docker/docker \
 && mkdir -p /root/.docker/cli-plugins \
 && curl -fsSL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-x86_64" \
  -o /root/.docker/cli-plugins/docker-compose \
 && chmod +x /root/.docker/cli-plugins/docker-compose \
 && python3 -m venv /opt/venvs/td \
 && /opt/venvs/td/bin/pip install --no-cache-dir \
  "git+https://github.com/arii/boomtick.git@${BOOMTICK_CLI_REF}#subdirectory=cli" \
 && /opt/venvs/td/bin/td --help > /dev/null
ENV PATH="/opt/venvs/td/bin:${PATH}"
WORKDIR /app
COPY backend/requirements.txt ./backend-requirements.txt
COPY deep-agent/requirements.txt ./deep-agent-requirements.txt
RUN pip install --no-cache-dir -r backend-requirements.txt -r deep-agent-requirements.txt
COPY backend/ ./backend/
COPY deep-agent/ ./deep-agent/
COPY buttons/briefing_summary.py ./buttons/
COPY --from=spa /build/frontend/dist ./frontend/dist/
# Agent runtime runs under the system python (langchain stack above).
# Repo state (git status, compose file, .env) resolves to the mounted
# live checkout, not the baked code.
ENV DEEP_AGENT_VENV=/usr/local/bin/python \
    BUTTONS_STACK=/home/ari/remote-server
EXPOSE 8000
CMD ["uvicorn", "backend.server:app", "--host", "0.0.0.0", "--port", "8000"]
