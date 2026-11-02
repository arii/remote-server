# Root task runner — consolidates scripts/ setup wrappers behind one interface.
# scripts/* are treated as READ-ONLY: targets wrap them, never modify them.
# Shared env-validation, error-handler, and logging live in mk/common.mk.
#
# Prerequisite chaining:
#   setup-mcp  -> setup-gh-auth (gh login persisted first), then MCP pieces
#   setup-all  -> setup-mcp + setup-hooks
#
# Usage: make help | make setup-all | make setup-mcp | make bootstrap

include mk/common.mk

SCRIPTS := scripts

.PHONY: help check-env setup-gh-auth setup-boomtick-mcp setup-browser-mcp \
	setup-tunnel setup-hooks setup-mcp setup-all bootstrap lint

help:
	@echo "Targets:"
	@echo "  check-env            validate gh + docker compose + token presence"
	@echo "  setup-gh-auth        persist gh CLI login from GH_TOKEN env"
	@echo "  setup-boomtick-mcp   install Boomtick MCP server + schemas (needs gh-auth)"
	@echo "  setup-browser-mcp    install Browser MCP launcher + schemas"
	@echo "  setup-tunnel         install BrowserMCP SSH tunnel wrapper + unit"
	@echo "  setup-hooks          install tracked git hooks into checkouts"
	@echo "  setup-mcp            gh-auth + all MCP pieces (boomtick, browser, tunnel)"
	@echo "  setup-all            setup-mcp + setup-hooks (full workstation setup)"
	@echo "  bootstrap            first-boot hardening (must run as root)"
	@echo "  lint                 bash -n + shellcheck over scripts/"

setup-gh-auth: check-gh
	@$(call LOG,setup-gh-auth)
	@$(SCRIPTS)/setup-gh-auth.sh
	@$(call OK,setup-gh-auth done)

setup-boomtick-mcp: setup-gh-auth
	@$(call LOG,setup-boomtick-mcp)
	@$(SCRIPTS)/setup-boomtick-mcp.sh
	@$(call OK,setup-boomtick-mcp done)

setup-browser-mcp:
	@$(call LOG,setup-browser-mcp)
	@$(SCRIPTS)/setup-browser-mcp.sh
	@$(call OK,setup-browser-mcp done)

setup-tunnel:
	@$(call LOG,setup-tunnel)
	@$(SCRIPTS)/setup-browsermcp-tunnel.sh
	@$(call OK,setup-tunnel done)

setup-hooks:
	@$(call LOG,setup-hooks)
	@$(SCRIPTS)/setup-hooks.sh
	@$(call OK,setup-hooks done)

# gh-auth first, then all MCP pieces.
setup-mcp: setup-gh-auth setup-boomtick-mcp setup-browser-mcp setup-tunnel
	@$(call OK,setup-mcp done)

# Full workstation setup.
setup-all: setup-mcp setup-hooks
	@$(call OK,setup-all done)

bootstrap:
	@$(call LOG,bootstrap — requires root)
	@$(SCRIPTS)/bootstrap.sh
	@$(call OK,bootstrap done)

lint:
	@bash -n $(SCRIPTS)/*.sh && (command -v shellcheck >/dev/null 2>&1 && shellcheck -S warning $(SCRIPTS)/*.sh || true)
