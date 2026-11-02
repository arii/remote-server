# Shared routines for the root Makefile: logging, error handling,
# environment validation, and prerequisite checks. Defined once here,
# reused by every target via `include mk/common.mk`.
#
# Test fakes (env overrides, no system changes):
#   FAKE_NO_GH=1         pretend `gh` is absent
#   FAKE_NO_DOCKER=1     pretend `docker compose` is absent
#   FAKE_NO_COMPOSE=1    pretend only the compose plugin is absent

SHELL := /usr/bin/env bash
.SHELLFLAGS := -euo pipefail -c

# --- logging / error-handler (defined once) ---
LOG  = printf '\033[1;34m==>\033[0m %s\n' "$(1)"
OK   = printf '\033[1;32mOK\033[0m %s\n' "$(1)"
WARN = printf '\033[1;33mWARN\033[0m %s\n' "$(1)"
DIE  = { printf '\033[1;31mERROR\033[0m %s\n' "$(1)" >&2; exit 1; }

# Resolve a tool path, honoring FAKE_NO_* overrides for tests.
# $(call resolve,gh,FAKE_NO_GH)
resolve = $(if $($(2)),__missing__,$(shell command -v $(1) 2>/dev/null || echo __missing__))

GH_BIN     := $(call resolve,gh,FAKE_NO_GH)
DOCKER_BIN := $(call resolve,docker,FAKE_NO_DOCKER)
COMPOSE_OK := $(if $(FAKE_NO_COMPOSE),no,$(if $(FAKE_NO_DOCKER),no,$(shell $(DOCKER_BIN) compose version >/dev/null 2>&1 && echo yes || echo no)))

.PHONY: check-gh check-docker check-env

check-gh:
	@if [ "$(GH_BIN)" = "__missing__" ]; then \
		printf '\033[1;31mERROR\033[0m %s\n' "prerequisite missing: 'gh' CLI not found in PATH. Install it: https://cli.github.com/ (FAKE_NO_GH override active: '$(FAKE_NO_GH)')" >&2; \
		exit 1; \
	fi
	@$(call LOG,gh found: $(GH_BIN))

check-docker:
	@if [ "$(DOCKER_BIN)" = "__missing__" ]; then \
		printf '\033[1;31mERROR\033[0m %s\n' "prerequisite missing: 'docker' not found in PATH. Install Docker Engine: https://docs.docker.com/engine/install/ (FAKE_NO_DOCKER override active: '$(FAKE_NO_DOCKER)')" >&2; \
		exit 1; \
	fi
	@if [ "$(COMPOSE_OK)" != "yes" ]; then \
		printf '\033[1;31mERROR\033[0m %s\n' "prerequisite missing: 'docker compose' plugin not working. Install the compose plugin: https://docs.docker.com/compose/install/ (FAKE_NO_COMPOSE override active: '$(FAKE_NO_COMPOSE)')" >&2; \
		exit 1; \
	fi
	@$(call LOG,docker compose found: $(DOCKER_BIN))

# Env validation shared by setup targets: warns (not fails) on missing token.
check-env: check-gh check-docker
	@if [ -z "$${GH_TOKEN:-$${GITHUB_TOKEN:-}}" ]; then \
		printf '\033[1;33mWARN\033[0m %s\n' "GH_TOKEN/GITHUB_TOKEN is not set; setup-gh-auth will fail until it is exported."; \
	else \
		printf '\033[1;32mOK\033[0m %s\n' "GH_TOKEN present."; \
	fi
