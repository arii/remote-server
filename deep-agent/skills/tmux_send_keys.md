---
name: tmux_send_keys
description: How to drive terminal panes with tmux send-keys today (no plugin needed): targeted keystrokes to a named pane plus capture-pane readback, for agent-to-agent CLI control. Use when a turn needs another pane's CLI to act.
trigger_nodes:
  - intent_explain
  - intent_fix
  - intent_implement
  - intent_review
---

# Skill: tmux Send-Keys Pane Control

`tmux` is already installed in the terminal image and in the dev
environment — use it directly. Do not add a zellij plugin for this
until a task proves tmux insufficient (see remote-server#1594).

## Primitives (all scoped to an explicit socket path + session)

Socket flag: `-S /path/to.sock` takes a full socket path;
`-L name` takes only a bare socket name (it lives under /tmp/tmux-$UID/).
Prefer `-S` with an explicit path so concurrent runs never share a server.
Proven live: `new-session`, `send-keys ... Enter`, `capture-pane -p`
round-trips a marker string back (verified 2026-11-23).

- `send-keys -l` sends text literally (no key-name interpretation) — use it when the text contains words like Enter or Tab.
- `capture-pane -p -S -` reads the full scrollback, not just the visible screen; plain `-p` gives readable text, `-e` keeps escape sequences.
- Healthcheck: `tmux -S <sock> has-session -t <name>` exits 0 alive / 1 gone — one command, no parsing.
- Blocking on a job (send-keys is fire-and-forget): have the pane signal done — `send-keys -t w 'long-job && tmux wait-for -S job-done' Enter`, then `tmux wait-for job-done` blocks until it fires.
- Audit trail: `tmux pipe-pane -t <name> -o 'cat >> ~/logs/<name>.log'` keeps a grep-able transcript of everything a driven pane printed.

## Pattern: one session per agent, named after it

Long-lived workers each get their own named session (`worker-<issue>`,
`coder-1`); the supervisor polls with `has-session` + `capture-pane -S - | tail`
and tears down with `kill-session`. Supervision is shell commands, no SDK:
spawn detached (`new-session -d -s <name> -c <dir>`), send the task,
poll the screen, kill or leave for human inspection. Same mechanics cross
machines over SSH since tmux does not care where the client runs.
(Credit: mager.co/blog/2026-08-23-orchestrating-agents-with-tmux.)

- List panes: `tmux -S <socket.sock> list-panes -s -F '#{session_name}:#{window_index}.#{pane_index} #{pane_current_command}'`
- Send text (no Enter): `tmux -S <socket.sock> send-keys -t <target> "text here"`
- Send Enter: `tmux -S <socket.sock> send-keys -t <target> Enter`
- One-liner command: `tmux -S <socket.sock> send-keys -t <target> "cmd --flags" Enter`
- Read back output: `tmux -S <socket.sock> capture-pane -t <target> -p -S -50`
- Wait for idle: poll `capture-pane` until the expected prompt or marker appears; bound the polls (max ~10, a few seconds apart) and report instead of looping forever.

## Rules (the #1389 lesson is binding)

1. Named panes only: always address `-t session:window.pane` explicitly. Never broadcast without a target (`send-keys` with no `-t` hits whatever is attached).
2. No spectator input: programmatic keystrokes are allowed only inside explicit operator-approved automation (a queued task, a named runbook step). Never leave a standing key-forward that types into whichever pane has focus.
3. Log every send: record target + text (never secrets) in the run log so a later turn can see what was driven.
4. Read before second send: `capture-pane` after each command; do not stack commands blindly.
5. Prefer the API/CLI first: if the other side has a real interface (HTTP endpoint, CLI flags, queue file), use it — pane-driving is the fallback for CLIs that only take keystrokes.
6. Clean up: kill scratch sessions/panes you created (`tmux -S <socket.sock> kill-session -t <name>`); never touch panes you did not create.
