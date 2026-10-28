# Web terminal image: ttyd (xterm.js over websocket) + zellij (phone-friendly
# multiplexer) + docker CLI (manage this stack from the phone terminal).
#
# Versions pinned via build args (defaults in .env.example as TTYD_VERSION /
# ZELLIJ_VERSION). Binaries are downloaded from GitHub releases and smoke-tested
# with --version during the build, so a renamed upstream asset fails LOUDLY at
# build time instead of silently shipping a broken image. If the build fails on
# a download step, check the asset names at:
#   https://github.com/tsl0922/ttyd/releases
#   https://github.com/zellij-org/zellij/releases

ARG DEBIAN_RELEASE=bookworm-slim
FROM debian:${DEBIAN_RELEASE}

ARG TTYD_VERSION=1.7.7
ARG ZELLIJ_VERSION=0.45.1
ARG NODE_VERSION=24.16.0
ARG PI_VERSION=1.4.0
ARG BOOMTICK_MCP_VERSION=0.6.2
ARG BOOMTICK_CLI_REF=cli-v0.14.2

ENV DEBIAN_FRONTEND=noninteractive \
    SHELL=/bin/bash \
    HOME=/root \
    TERM=xterm-256color \
    PATH="/root/.local/bin:/opt/venvs/td/bin:${PATH}"

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash ca-certificates curl gh git gnupg htop less nano openssh-client procps python3 python3-pip python3-venv xz-utils \
 && install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg \
      | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
 && chmod a+r /etc/apt/keyrings/docker.gpg \
 && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian bookworm stable" \
      > /etc/apt/sources.list.d/docker.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
 && curl -fsSL -o /usr/local/bin/ttyd \
      "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.x86_64" \
 && chmod +x /usr/local/bin/ttyd \
 && /usr/local/bin/ttyd --version \
 && curl -fsSL "https://github.com/zellij-org/zellij/releases/download/v${ZELLIJ_VERSION}/zellij-x86_64-unknown-linux-musl.tar.gz" \
      | tar -xz -C /usr/local/bin zellij \
 && chmod +x /usr/local/bin/zellij \
 && /usr/local/bin/zellij --version \
 && curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
      | tar -xJ -C /usr/local --strip-components=1 \
 && node --version \
 && npm install -g "@earendil-works/pi-coding-agent@${PI_VERSION}" \
 && pi --version \
 && gh --version \
 && npm install -g "pnpm@10.28.2" "@arii/boomtick-mcp@${BOOMTICK_MCP_VERSION}" \
 && pnpm --version \
 && test -x /usr/local/bin/boomtick-mcp \
 && python3 --version \
 && python3 -m venv /tmp/venv-smoke \
 && /tmp/venv-smoke/bin/python --version \
 && rm -rf /tmp/venv-smoke \
 && python3 -m venv /opt/venvs/td \
 && /opt/venvs/td/bin/pip install "git+https://github.com/arii/boomtick.git@${BOOMTICK_CLI_REF}#subdirectory=cli" \
 && /opt/venvs/td/bin/td --help > /dev/null \
 && ln -sf /root/.local/bin/agy /usr/local/bin/agy \
 && mkdir -p /home/ari && ln -sfn /srv/remote-server /home/ari/remote-server \
 && apt-get purge -y gnupg xz-utils \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

# System gitconfig: safe.directory = * for bind-mounted host checkouts (/srv/remote-server, /srv/repos/*).
COPY gitconfig /etc/gitconfig

# zellij config (single source of truth: terminal/config.kdl).
# Rebuild after editing: docker compose build terminal && docker compose up -d terminal
COPY config.kdl /root/.config/zellij/config.kdl

# Pi config (Ollama provider, see docs/pi-setup.md). A named volume
# (pi-data, see docker-compose.yml) persists sessions; the image content
# below seeds that volume on first use. To pick up config changes later,
# edit inside the volume or recreate it (recreating loses sessions).
COPY pi-config/models.json /root/.pi/agent/models.json
COPY pi-config/settings.json /root/.pi/agent/settings.json
# BoomTick MCP server entry (see docs/pi-setup.md § BoomTick MCP tools).
# Same volume-seeding caveat as above: an existing pi-data volume does
# NOT pick this up — copy it in with docker cp (see the doc).
COPY pi-config/mcp.json /root/.pi/agent/mcp.json
# Agent brief: standing instructions for Pi (see docs/pi-setup.md § Agent brief).
# Same volume-seeding caveat: copy it into an existing pi-data volume.
COPY pi-config/agent-brief.md /root/.pi/agent/agent-brief.md

WORKDIR /srv/remote-server
EXPOSE 7681
# CMD comes from docker-compose.yml: ttyd <flags> zellij attach --create main
