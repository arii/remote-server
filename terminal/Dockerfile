# Web terminal image: ttyd (xterm.js over websocket) + zellij (phone-friendly
# multiplexer) + docker CLI (manage this stack from the phone terminal).
#
# Supply chain (#523): every build-time fetch is integrity-checked and the
# build fails LOUDLY on mismatch. Release binaries + node tarball are
# verified against embedded SHA256 (taken from vendor-published checksums);
# npm packages install by immutable version; the boomtick CLI installs by
# release tag; the muse installer is vendored in this directory; the ttyd
# html.h is verified by content hash. To bump a version, update the ARG and
# its hash together — a lone version bump fails the build by design.
# Bump pointers:
#   https://github.com/tsl0922/ttyd/releases (SHA256SUMS asset)
#   https://github.com/zellij-org/zellij/releases (*.sha256sum covers the inner binary)
#   https://nodejs.org/dist/ (SHASUMS256.txt)
#   npm: `npm view <pkg> version` (pin the running version, not latest)
#   boomtick CLI tags: `git ls-remote --tags https://github.com/arii/boomtick.git`
#   muse installer: curl -fsSL https://dev.meta.ai/install.sh, review the diff,
#     then replace terminal/muse-install.sh
# TTYD_VERSION / ZELLIJ_VERSION also flow in from .env via compose build
# args; overriding them without updating the hashes below fails loud.

ARG DEBIAN_RELEASE=bookworm-slim
FROM debian:${DEBIAN_RELEASE}

ARG TTYD_VERSION=1.7.7
ARG ZELLIJ_VERSION=0.45.1
ARG NODE_VERSION=24.16.0
ARG PI_VERSION=1.4.0
# Git SHA pinned to boomtick origin/main at pin time (no release tag
# exists yet containing the td JSON gates + MCP tool pruning; a full SHA
# is immutable and therefore as deterministic as a tag).
# Bump: take the newest cli-vX.Y.Z tag (or newer main SHA) and smoke td
# + boomtick-mcp in the built image.
ARG BOOMTICK_CLI_REF=3e9c5c2f8b604e38d8d0d76944d5c03e9d5c656e

ENV DEBIAN_FRONTEND=noninteractive \
    SHELL=/bin/bash \
    HOME=/root \
    TERM=xterm-256color \
    PATH="/root/.local/bin:/opt/venvs/td/bin:${PATH}"

# Vendored muse installer (#523): no pipe-to-bash at build time. The script
# self-verifies the launcher it fetches (x-content-sha256 when advertised).
COPY muse-install.sh /tmp/muse-install.sh
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash ca-certificates curl gh git gnupg htop less nano openssh-client procps python3 python3-pip python3-venv tmux vim xz-utils \
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
 && echo "8a217c968aba172e0dbf3f34447218dc015bc4d5e59bf51db2f2cd12b7be4f55  /usr/local/bin/ttyd" | sha256sum -c - \
 && chmod +x /usr/local/bin/ttyd \
 && /usr/local/bin/ttyd --version \
 && curl -fsSL -o /tmp/zellij.tar.gz "https://github.com/zellij-org/zellij/releases/download/v${ZELLIJ_VERSION}/zellij-x86_64-unknown-linux-musl.tar.gz" \
 && echo "40bcc2e03f5d5ae8e054e39f676081fe12ab70871506996ba595834c3718eefc  /tmp/zellij.tar.gz" | sha256sum -c - \
 && tar -xz -f /tmp/zellij.tar.gz -C /usr/local/bin zellij \
 && rm -f /tmp/zellij.tar.gz \
 && chmod +x /usr/local/bin/zellij \
 && /usr/local/bin/zellij --version \
 && curl -fsSL -o /tmp/node.tar.xz "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
 && echo "d804845d34eddc21dc1092b519d643ef40b1f58ec5dec5c22b1f4bd8fabde6c9  /tmp/node.tar.xz" | sha256sum -c - \
 && tar -xJ -f /tmp/node.tar.xz -C /usr/local --strip-components=1 \
 && rm -f /tmp/node.tar.xz \
 && node --version \
 && npm install -g "@earendil-works/pi-coding-agent@${PI_VERSION}" \
 && pi --version \
 && gh --version \
 && npm install -g "pnpm@10.28.2" \
 && pnpm --version \
 && python3 --version \
 && python3 -m venv /opt/venvs/td \
 && /opt/venvs/td/bin/pip install "git+https://github.com/arii/boomtick.git@${BOOMTICK_CLI_REF}#subdirectory=cli" \
 && /opt/venvs/td/bin/td --help > /dev/null \
 && git clone https://github.com/arii/boomtick.git /tmp/boomtick \
 && git -C /tmp/boomtick checkout "${BOOMTICK_CLI_REF}" \
 && test "$(git -C /tmp/boomtick rev-parse HEAD)" = "${BOOMTICK_CLI_REF}" \
 && (cd /tmp/boomtick && pnpm install --frozen-lockfile --filter @arii/boomtick-mcp...) \
 && (cd /tmp/boomtick/mcp && PATH="/opt/venvs/td/bin:${PATH}" pnpm run build) \
 && test -f /tmp/boomtick/mcp/dist/mcp/server.js \
 && test "$(find /tmp/boomtick/mcp/.mcp/schemas -name '*.json' | wc -l)" -ge 27 \
 && npm install -g /tmp/boomtick/mcp \
 && rm -rf /tmp/boomtick \
 && test -x /usr/local/bin/boomtick-mcp \
 && command -v boomtick-mcp > /dev/null \
 && MUSE_NO_MODIFY_PATH=1 bash /tmp/muse-install.sh \
 && rm -f /tmp/muse-install.sh \
 && test -x /root/.local/bin/muse \
 && /root/.local/bin/muse --version \
 && python3 --version \
 && python3 -m venv /tmp/venv-smoke \
 && /tmp/venv-smoke/bin/python --version \
 && rm -rf /tmp/venv-smoke \
 && /opt/venvs/td/bin/td audit-impact --help > /dev/null \
 && mkdir -p /home/ari && ln -sfn /srv/remote-server /home/ari/remote-server \
 && apt-get purge -y gnupg xz-utils \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /root/.local/share/vim /root/.local/cache

# No /usr/local/bin/agy symlink (#523): agy is the Antigravity CLI,
# installed at runtime onto the local-data volume (/root/.local) — a
# build-time symlink would ship dead and no build-time check could
# pass. /root/.local/bin is first on PATH, so agy resolves without it.

# System gitconfig: safe.directory = * for bind-mounted host checkouts (/srv/remote-server, /srv/repos/*).
COPY gitconfig /etc/gitconfig

# System vimrc: viminfo (jumps/registers/history) on the local-data volume.
COPY vimrc.local /etc/vim/vimrc.local
RUN (grep -q "vimrc.local" /etc/vim/vimrc || echo "source /etc/vim/vimrc.local" >> /etc/vim/vimrc) && test -f /etc/vim/vimrc.local

# Custom ttyd index.html with takeover button: extract base index.html from ttyd source and inject takeover widget
COPY takeover.html /tmp/takeover.html
RUN mkdir -p /etc/ttyd \
 && python3 -c "import urllib.request, gzip, re, hashlib, sys; \
    data = urllib.request.urlopen('https://raw.githubusercontent.com/tsl0922/ttyd/${TTYD_VERSION}/src/html.h').read(); \
    hashlib.sha256(data).hexdigest() == '6f3716ebd951e101df883bb14922f067c023f11561aa088ef487814183727cf3' or sys.exit('html.h sha256 mismatch'); \
    raw = bytes(int(b, 16) for b in re.findall(r'0x([0-9a-fA-F]{2})', data.decode('utf-8'))); \
    html = gzip.decompress(raw).decode('utf-8'); \
    widget = open('/tmp/takeover.html').read(); \
    open('/etc/ttyd/index.html', 'w').write(html.replace('</body>', widget + '</body>'))" \
 && rm -f /tmp/takeover.html

# Task-scoped Pi profile scripts (pi-code, pi-review, pi-triage)
COPY bin/ /usr/local/bin/
RUN chmod +x /usr/local/bin/pi-*

# Muse resume hint on interactive shell start (#377): one line naming the
# latest session. /root/.bashrc is image content (not volume-mounted), so
# this applies on rebuild+recreate. Smoke test runs the no-store path.
RUN touch /root/.bashrc \
 && (grep -q "muse-resume-hint" /root/.bashrc || printf '%s\n' \
      '# Muse resume hint (one line, interactive shells only).' \
      'if [ -n "$PS1" ] && command -v muse-resume-hint >/dev/null 2>&1; then muse-resume-hint; fi' \
      >> /root/.bashrc) \
 && muse-resume-hint > /dev/null

# Muse auto-resume offer on interactive shell start (#408): offers to
# resume the session interrupted by a restart (timeout resumes, N declines
# until next restart, MUSE_NO_AUTORESUME=1 skips). Same rebuild+recreate
# note. Smoke test runs the no-store path plus a bash syntax check.
RUN touch /root/.bashrc \
 && (grep -q "muse-resume-ask" /root/.bashrc || printf '%s\n' \
      '# Muse auto-resume offer (interactive shells only).' \
      'if [ -n "$PS1" ] && command -v muse-resume-ask >/dev/null 2>&1; then muse-resume-ask; fi' \
      >> /root/.bashrc) \
 && muse-resume-offer > /dev/null \
 && bash -n /usr/local/bin/muse-resume-ask

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
COPY pi-config/APPEND_SYSTEM.md /root/.pi/agent/APPEND_SYSTEM.md
COPY pi-config/AGENTS.md /root/.pi/agent/AGENTS.md
COPY pi-config/skills /root/.pi/agent/skills

WORKDIR /srv/remote-server
EXPOSE 7681
# CMD comes from docker-compose.yml: ttyd <flags> zellij attach --create main
