# gdocs-access — isolated Google Doc read/write spike

Works in this folder only, so nothing else in the checkout is touched.
Target doc: [workout doc](https://docs.google.com/document/d/1aXd0kxDP_PpxgE3HOwzxHOfP_pP88aA5TFruc3kY_Sk/edit)
(`GOOGLE_DOCUMENT_ID=1aXd0kxDP_PpxgE3HOwzxHOfP_pP88aA5TFruc3kY_Sk`).

Credentials are reused from
`/srv/repos/previous_workouts` (OAuth client-secret file copied here;
`token.json` is created fresh by you via browser consent). Secrets
(`.env`, `token.json`, `client_secret*.json`) are gitignored here and at
the repo root (`.env*`).

## Setup (project-local env, per python-env)

```bash
cd /srv/remote-server/gdocs-access
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## One-time consent (required for write)

The old `previous_workouts` token only has `drive.readonly` scope, and
the new Doc needs `documents` (read/write) scope, so authorize once:

```bash
source .venv/bin/activate
python gdocs.py auth-url     # open URL, approve
python gdocs.py auth-code <code-from-localhost-redirect>
```

## Read today (no consent needed)

Plain-text read via the Drive API works right now with the existing
`previous_workouts` token (read-only use, nothing is written back there):

```bash
source .venv/bin/activate
python gdocs.py export --token-file /srv/repos/previous_workouts/exercise_data/token.json
```

## Read / write via Docs API (two one-time steps left)

`read` and `append` need the Docs API, which is currently disabled on the
`gen-lang-client-0299253826` project (verified: `docs.get` returns 403
"API has not been used in project 237690545533"):

1. Enable it: [Google Docs API overview](https://console.developers.google.com/apis/api/docs.googleapis.com/overview?project=237690545533)
   (Drive API is already enabled).
2. Run the consent flow above (`auth-url` / `auth-code`) to mint a token
   with `documents` + `drive.readonly` scopes.

Then:

```bash
source .venv/bin/activate
python gdocs.py read
python gdocs.py append "Hello from the spike
"
```

## Notes on the sample you pasted

- Scopes were truncated (`https://googleapis.com`); fixed to
  `https://www.googleapis.com/auth/documents` +
  `https://www.googleapis.com/auth/drive.readonly`.
- `insertText` at `index: 1` writes at the *start*; `append` here reads
  the doc first and inserts at `endIndex - 1` (before trailing newline).
- Auth here is OAuth user consent (proven in `previous_workouts`),
  not a service account — no `.json` key to share the doc with,
  no Drive sharing step needed for docs you own.
