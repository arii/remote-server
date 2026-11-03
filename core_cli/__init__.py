"""Unified repo tooling CLI (stdlib only, no third-party deps).

Single entrypoint: ``python3 -m core_cli <subcommand> [args]`` from the
repo root. Each module keeps its own flags and stays directly executable
(``python3 core_cli/<module>.py ...``) for the container-exec call sites
that run tools by absolute path.
"""
