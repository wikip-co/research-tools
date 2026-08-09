# Release notes & operator handoff

Living notes for whoever is operating this stack (especially a Hermes agent on **iconium**).  
Read this before changing production paths, dotenv behavior, backups, or FlareSolverr.

**Primary host:** `iconium` (`10.32.25.177` / `iconium.lan`)  
**Repo on host:** `~/Research/research-tools`  
**Content repo:** `~/Research/content`  
**Triage UI:** http://iconium.lan:8765 (systemd user unit `research-triage-ui`)  
**GitHub:** `wikip-co/research-tools` · branch `main`

---

## Local publisher implementation (2026-08-09, working tree)

The production path now includes:

- packet rejection for bot/CAPTCHA/login/error responses regardless of length;
- Scrapling → FlareSolverr (original URL) → agent-browser fallback with
  validation after every retrieval;
- Crossref title/DOI identity checks and hybrid content matching;
- local llama.cpp structured draft and critic passes;
- exact-source-quote, near-verbatim Natural Healing, study-design, render, and
  Git-scope gates;
- isolated content worktrees based on `origin/main` and optional draft PRs;
- durable `publication_jobs` and `publication_job_events` queues with atomic
  leases, retries, terminal outcomes, and deduplication; and
- Scholar-sync and local-publisher systemd templates.

The Natural Healing guide is unchanged and remains authoritative. The new
timers are tracked but were not installed or enabled on iconium as of the
observation date. The active dependencies were the Qwen3.6 35B A3B Q8_0
llama.cpp service on port 8080, `research-flaresolverr` on port 8191, the
triage UI on port 8765, and the enabled nightly database backup timer.

See `docs/local-research-publisher.md` and the workspace-level
`../docs/research-production-operations.md` before enabling automation.

## Current committed baseline (as of 2026-08-03)

| Commit | Summary |
|--------|---------|
| `9d794a8` | Improve scholar-alerts SQLite backup for live DB on iconium |
| `b9fd730` | Fix managed-clone tests ignoring host `CONTENT_REPO_ROOT` from `.env` |
| `4f2f721` | Rewrite publisher PDF gate URLs to HTML before scrape |
| `9dd2fb4` | Add FlareSolverr Cloudflare fallback for web-scraper |

The commits below are the baseline beneath the 2026-08-09 implementation.
Do not assume the working tree is clean while that implementation is under
review. Ser9 is not the production runtime.

---

## Production layout (iconium)

```
~/Research/research-tools/          # this repo (tools + UI + agent-workflow)
~/Research/research-tools/.env      # host-local secrets/paths (never commit)
~/Research/research-tools/gmail-reader/data/scholar-alerts.db
~/Research/content/                 # markdown content corpus (CONTENT_REPO_ROOT)

FlareSolverr:  http://127.0.0.1:8191/v1
  (env often FLARESOLVERR_URL=http://127.0.0.1:8191/v1)
  podman name historically: research-flaresolverr
  compose reference: deploy/docker-compose.flaresolverr.yml

NAS (NFS):
  /mnt/naspi5  → backups etc.
  /mnt/data1   → additional data

DB backups:
  script: gmail-reader/backup-db.sh
  timer:  ~/.config/systemd/user/research-db-backup.timer  (~03:30 local)
  dest:   /mnt/naspi5/content-agent-backups/gmail-reader/
  keeps:  BACKUP_KEEP_COUNT (default 14) timestamped snapshots + *-latest.db
```

### systemd user units (typical)

| Unit | Role |
|------|------|
| `research-triage-ui.service` | LAN triage web UI |
| `research-db-backup.timer` / `.service` | Nightly SQLite backup to NAS |
| `container-research-flaresolverr.service` | FlareSolverr container (see footnote) |
| `hermes-gateway.service` | Iconium Hermes messaging gateway |
| `qwen-moe-server-q8.service` | Active local llama.cpp OpenAI-compatible API |
| `research-scholar-sync.timer` | Tracked template; not installed as observed 2026-08-09 |
| `research-local-publisher.timer` | Tracked template; not installed as observed 2026-08-09 |

Check: `systemctl --user status research-triage-ui research-db-backup.timer`

---

## Recent changes operators must know

### 1. `agent-workflow` dotenv / managed-clone fix (`b9fd730`)

**Symptom (iconium-only):** `tests.test_agent_workflow` failed on production while ser9 passed.

**Root cause:** iconium `.env` sets `CONTENT_REPO_ROOT` to the real content tree. Older `load_dotenv` treated **empty** env vars as missing (`-z`) and overwrote test isolation. `ensure_content_repo_ready` then skipped the managed clone and searched the live corpus (no test fixtures) → empty matches.

**Fix:**
- `load_dotenv` only fills **unset** variables (`! -v`), not empty ones
- `AGENT_WORKFLOW_SKIP_DOTENV=1` for tests
- Content-repo defaults bind **after** dotenv
- Tests force `CONTENT_REPO_ROOT=""` and drop host content-repo keys

**Verify:**
```bash
cd ~/Research/research-tools
python3 -m unittest tests.test_agent_workflow -v
# expect 2 tests OK, including managed-clone path
```

**Do not:** “fix” tests by unsetting production `CONTENT_REPO_ROOT` in `.env`. Production doctor/UI should keep resolving `~/Research/content`.

### 2. Safer live DB backup (`9d794a8`)

`gmail-reader/backup-db.sh` no longer plain-`cp`s a live SQLite file.

- Default DB path is **script-relative** (`…/gmail-reader/data/scholar-alerts.db`)
- Prefers `sqlite3 … '.backup'` (safe while UI holds the DB)
- Writes a `.partial` file, then renames; copies to `*-latest.db`
- `PRAGMA integrity_check` + article/message counts in the log line
- Fails if NAS parent path missing
- Prunes old timestamped snapshots (`BACKUP_KEEP_COUNT`, default 14)

**Manual run:**
```bash
~/Research/research-tools/gmail-reader/backup-db.sh \
  ~/Research/research-tools/gmail-reader/data/scholar-alerts.db \
  /mnt/naspi5/content-agent-backups/gmail-reader
```

Timer unit already passes explicit source + dest paths.

### 3. Scrape stack (earlier on `main`)

- FlareSolverr Cloudflare fallback for `web-scraper` (`9dd2fb4`)
- Publisher PDF gate URLs rewritten to HTML before scrape (`4f2f721`)
- Research triage web UI + Codex job log streaming (see README / `gmail-reader`)

---

## Known footnote (non-blocking as of 2026-08-03)

`systemctl --user is-active container-research-flaresolverr` may report **inactive** while  
`http://127.0.0.1:8191/v1` still answers (FlareSolverr 3.5.x).  

Something is keeping the listener up (manual podman, other unit, or stale process).  
**Follow-up:** make reboot-persistent ownership obvious — either enable the user unit properly or document the real supervisor — so CF-protected scrapes don’t silently lose Flare after reboot.

Quick health check:
```bash
curl -sS -m 3 http://127.0.0.1:8191/v1 \
  -H 'Content-Type: application/json' \
  -d '{"cmd":"sessions.list"}'
# expect JSON status ok
```

---

## Git hygiene note (resolved)

On 2026-08-02 the dotenv fix was **scp’d** onto iconium before a clean pull, leaving a dirty tree (`agent-workflow`, tests, local `backup-db.sh`, `*.bak-pre-dotenv-fix`, `tmp-fix/`).  

That was cleaned up on 2026-08-03:
1. Improved `backup-db.sh` committed from ser9 as `9d794a8` and pushed
2. Iconium junk removed; `git pull --ff-only` → clean at `9d794a8`
3. Tests re-run OK on iconium

**Prefer:** commit + push from a clean checkout, then `git pull --ff-only` on iconium. Avoid long-lived scp overlays on production.

---

## Suggested first actions for a new operator / Hermes

1. `cd ~/Research/research-tools && git status -sb && git pull --ff-only`
2. Read this file + `README.md` + `docs/WORKFLOW.md`
3. Confirm services/timers and Flare health (commands above)
4. Confirm NAS mounts: `mountpoint /mnt/naspi5 /mnt/data1`
5. Confirm DB size/integrity if touching backups:
   `sqlite3 gmail-reader/data/scholar-alerts.db 'PRAGMA integrity_check;'`
6. Only then take product work (triage UI, scrape packets, Scholar intake, etc.)

### Out of scope unless asked
- ser9 was decommissioned as the **primary** research runtime (cutover ~Aug 2026); don’t revive ser9 research services without explicit direction
- Vault paths and OAuth live outside git — see README “Required Vault Fields” and host `.env`

---

## Changelog (concise)

### 2026-08-03 — handoff snapshot
- Documented production layout, recent fixes, Flare footnote, clean git state
- `main` at `9d794a8` (backup script) atop `b9fd730` (dotenv/managed-clone)

### 2026-08-02–03 — reliability
- Managed-clone tests stable under production `.env`
- Online-safe SQLite backup with integrity check + retention

### 2026-07 / prior (see `git log`)
- FlareSolverr integration, PDF→HTML gate rewrite, triage UI, Codex job logs

---

*Update this file when you land operator-visible behavior changes, host cutovers, or break/fix notes another agent will need.*
