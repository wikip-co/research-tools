# Deploy helpers (research-tools)

## FlareSolverr

Local Cloudflare / anti-bot challenge proxy used by `web-scraper` when HTML retrieval is blocked or weak.

| | |
|--|--|
| **Compose** | `deploy/docker-compose.flaresolverr.yml` |
| **Container** | `research-flaresolverr` |
| **API** | `http://127.0.0.1:8191/v1` |
| **Image** | `ghcr.io/flaresolverr/flaresolverr:latest` (`pull_policy: always`) |

### Start / update

```bash
cd /home/anthony/Research/research-tools

# Prefer this: always re-pull latest before recreate
docker compose -f deploy/docker-compose.flaresolverr.yml up -d --pull always

# Or pull then up
docker compose -f deploy/docker-compose.flaresolverr.yml pull
docker compose -f deploy/docker-compose.flaresolverr.yml up -d
```

### Health

```bash
docker ps --filter name=research-flaresolverr
curl -sS -X POST http://127.0.0.1:8191/v1 \
  -H 'Content-Type: application/json' \
  -d '{"cmd":"request.get","url":"https://www.google.com/","maxTimeout":60000}' \
  | jq '{status, message, version}'
```

### Use from web-scraper

```bash
cd web-scraper
uv run web-scraper "https://example.org/article" --flaresolverr-mode auto
```

Defaults:

- `FLARESOLVERR_URL=http://127.0.0.1:8191/v1`
- `WEB_SCRAPER_FLARESOLVERR_MODE=auto`

**Quality bar:** full text when available; **abstract-only is acceptable** for paywalled pages. FlareSolverr does not bypass publisher paywalls — it only solves bot/CF interstitial pages so the real HTML (or abstract landing page) can load. The returned page is still passed through fatal-page and title/DOI identity gates.

### Maintenance note

Cloudflare challenge implementations change. Keeping `latest` + `pull_policy: always` (and periodic `up -d --pull always`) is intentional so iconium tracks upstream FlareSolverr releases. If upstream goes stale and challenges fail again, evaluate maintained forks and pin a working tag temporarily.
