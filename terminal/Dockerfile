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

ENV DEBIAN_FRONTEND=noninteractive \
    SHELL=/bin/bash \
    HOME=/root \
    TERM=xterm-256color

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash ca-certificates curl git gnupg htop less nano openssh-client procps xz-utils \
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
 && apt-get purge -y gnupg xz-utils \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

# zellij config (single source of truth: terminal/config.kdl).
# Rebuild after editing: docker compose build terminal && docker compose up -d terminal
COPY config.kdl /root/.config/zellij/config.kdl

WORKDIR /srv/remote-server
EXPOSE 7681
# CMD comes from docker-compose.yml: ttyd <flags> zellij attach --create main
