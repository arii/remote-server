---
name: gemini-repo-ask
description: Drive the Gemini web app via BrowserMCP to import a GitHub repo and ask structured advanced questions.
---

# Gemini Repo Ask

Use when the user wants repo-grounded answers from `https://gemini.google.com/app` driven through the paired browser (BrowserMCP): import repo code, then ask advanced questions.

## Repo selection

- Default repo is `arii/remote-server` (`https://github.com/arii/remote-server`).
- Accept an explicit override: `arii/boomtick`, `arii/hrm`, or `arii/tech-dancer`. When in `/srv/repos/<name>`, default to `arii/<name>` unless the user names another.
- Always use the full `https://github.com/<owner>/<repo>` URL for the import dialog.
- Keep the repo chip attached for the whole chat and name the repo explicitly in every prompt (for example: "Based on the attached `arii/remote-server` code ..."). This is what keeps follow-ups and change requests grounded when code changes are discussed.

## Canonical import flow (verified)

1. `tab list`, then `tab select` the existing Gemini tab or `tab new https://gemini.google.com/app`.
2. `snapshot -i`. Click `Upload & tools` (+), then `More uploads`, then `Import code`.
3. In the `Import code` dialog, fill `GitHub repository or branch URL` with the full repo URL and click `Import`.
4. Verify success: a chip with a `close <owner>/<repo>` button appears near the prompt box.
5. Re-snapshot after any navigation; snapshot refs expire. Batch predictable steps in one call. `TrustedHTML assignment blocked` console errors during clicks/fills are benign.

## Ask questions

- Fill `Enter a prompt for Gemini`, click `Send message`, then `wait` and `get text`. Long answers stream; wait again and re-read until the response is complete.
- Structure every question: state the attached repo, demand exact Markdown sections, demand file paths, demand conciseness.
- Architecture starter: "This repo `<owner>/<repo>` is attached. Summarize its architecture: main services, how `<subsystem>` works, and key config/test files."
- Issues/tech-debt audit: "Based on the attached `<owner>/<repo>` code, audit for persisting issues and technical debt. Respond in Markdown with exactly these sections: `## Top Issues` (Severity | Area | File/Path | Issue | Suggested fix), `## Technical Debt`, `## Quick Wins (<30 min)`, `## Risks`. Be specific with file paths, keep it concise."
- For change requests, keep the same chat and chip, restate the repo, and ask for unified diffs or per-file edits with paths.

## Screenshot UX feedback (verified)

No clipboard or host-shared mount needed: the BrowserMCP daemon runs in this same container, so `screenshot --path` files are directly usable by `upload`.

1. On the app tab: `screenshot --path /tmp/<name>.jpg` (JPEG, longest side ≤1568px). Confirm the file exists via shell at the same path.
2. Back on the Gemini tab: click `Upload & tools`, then `upload <Upload-files-menuitem-ref> /tmp/<name>.jpg` (the menuitem opens the file chooser; `upload` fills it).
3. Verify: a thumbnail chip appears above the prompt box. It carries no filename text in the DOM, so confirm visually with `screenshot`.
4. Ask with this template: "This screenshot shows `<page>` of `<owner>/<repo>`. Give UX feedback in Markdown with exactly these sections: `## Verdict` (one line plus score out of 10), `## What works` (max 3 bullets), `## Issues` (Severity | Element | Problem | Fix), `## Accessibility`, `## Top 3 changes`. Be specific to visible elements, keep it concise."
5. Multiple states: repeat steps 1-2 per view (`upload` accepts several paths in one call), then ask for a comparative review across them.
