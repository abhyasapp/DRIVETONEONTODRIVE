# GitHub Actions secrets - how to paste them

Where: **GitHub repo -> Settings -> Secrets and variables -> Actions -> New repository secret**
Enter the *Name* exactly as below (case-sensitive), paste the *Value*, click **Add secret**.
Secrets can't be viewed again after saving - to change one, create it again with the same name.

**Rules for every secret**
- Paste on **one line**, no quotes around it, no leading/trailing spaces (unless the table says "whole file").
- Never commit these values or the files they come from (`.gitignore` already blocks them).

| Secret name | Required | What to paste (example is FAKE) |
|---|---|---|
| `DATABASE_URL` | yes | `postgresql://neondb_owner:AbC123xyz@ep-cool-name-12345.ap-southeast-1.aws.neon.tech/neondb?sslmode=require` |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | yes | **Whole contents** of the service-account key file, starting with `{` and ending with `}` (multi-line is OK here) |
| `GDRIVE_OAUTH_TOKEN` | yes* | One-line JSON, see below |
| `ABHYAS_APPS_SCRIPT_URL` | yes | `https://script.google.com/macros/s/AKfycbx.../exec` |
| `ABHYAS_SYNC_KEY` | yes | `my-long-random-sync-key` (same value as in the Apps Script) |
| `GEMINI_API_KEY` | yes | `AIzaSyD-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` |
| `GEMINI_API_KEY_2` | no | a second Gemini key (more quota) |
| `CEREBRAS_API_KEY` | no | `csk-xxxxxxxxxxxxxxxxxxxx` |
| `OPENROUTER_API_KEY` | no | `sk-or-v1-xxxxxxxxxxxxxxxx` |
| `OAUTH_CREDENTIALS_JSON` | legacy | base64 text, only if you use the pickle fallback |
| `OAUTH_TOKEN_PICKLE` | legacy | base64 text, only if you do NOT set `GDRIVE_OAUTH_TOKEN` |

\* Set either `GDRIVE_OAUTH_TOKEN` (recommended) **or** `OAUTH_TOKEN_PICKLE`.

## Getting `GDRIVE_OAUTH_TOKEN` (recommended)

1. On your PC, run the uploader once so the browser login creates `oauth-token.pickle`:
   `python -m upload.upload_chapters_data --dry-run`
2. Print the token as JSON:
   `python -m utils.extract_token`
3. Copy the single output line. It looks like this (FAKE):
   ```
   {"refresh_token": "1//0gAbC...", "token_uri": "https://oauth2.googleapis.com/token", "client_id": "1234-abc.apps.googleusercontent.com", "client_secret": "GOCSPX-xxxx", "scopes": ["https://www.googleapis.com/auth/drive"]}
   ```
4. Paste it into the secret `GDRIVE_OAUTH_TOKEN`.

> If uploads start failing after about 7 days with `invalid_grant`, your Google OAuth consent screen is in
> **Testing** mode. Set it to **In production** (Google Cloud Console -> APIs & Services -> OAuth consent screen),
> then repeat steps 1-4.

## Legacy base64 secrets (only if you skip `GDRIVE_OAUTH_TOKEN`)

Encode on **one line** (`-w0` matters), then paste the output:

| OS | Command |
|---|---|
| Linux / Git Bash | `base64 -w0 oauth-credentials.json` |
| macOS | `base64 -i oauth-credentials.json` |
| Windows PowerShell | `[Convert]::ToBase64String([IO.File]::ReadAllBytes("oauth-credentials.json"))` |

Example output (FAKE, shortened): `eyJpbnN0YWxsZWQiOnsiY2xpZW50X2lkIjoiMTIzNC5hcHBzLi4u`
Do the same for `oauth-token.pickle` -> `OAUTH_TOKEN_PICKLE`.

## Quick checks after saving

- Actions -> **Daily Pipeline** -> **Run workflow**. Tip: type `export` in "only" to test one step fast.
- The first step "Check required secrets exist" prints `::error::Secret X is empty or not set` for anything missing.
- "Write .env" prints each key's length (e.g. `GEMINI_API_KEY: 39 chars`). `0 chars` = not set or empty.
- The job now turns **red** when any step fails, and `pipeline-logs-<run id>` is attached to every run.

## Common mistakes

| Symptom | Cause / fix |
|---|---|
| `Secret X is empty or not set` | Wrong name (typo/case) or saved under *Environment* or *Variables* instead of *Repository secrets* |
| `... is not valid base64` | You pasted the raw file instead of the base64 text |
| `not valid service-account JSON` | Pasted only part of the file; paste everything from `{` to `}` |
| `contains a line break` | Value was wrapped over two lines; paste it on one line |
| `DB connect failed after 4 tries` | Wrong `DATABASE_URL`, missing `?sslmode=require`, or the Neon project is paused/out of quota |
| `No valid OAuth credentials and stdin is not a terminal` | Token missing/expired/revoked; redo "Getting GDRIVE_OAUTH_TOKEN" |
