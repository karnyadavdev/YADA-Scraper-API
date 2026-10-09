# YADA Scraper API

[![CI](https://github.com/karnyadavdev/YADA-Scraper-API/actions/workflows/ci.yml/badge.svg)](https://github.com/karnyadavdev/YADA-Scraper-API/actions/workflows/ci.yml)

Self-hosted web scraping API. Send one URL or a list of URLs, get clean
page content back. Built to get through common bot walls (Cloudflare,
DataDome, rate limits, JS-rendered pages) without per-site hacks.

## Tech stack

- Python 3.12 + FastAPI + uvicorn
- curl_cffi for Chrome TLS impersonation
- BeautifulSoup + lxml for parsing
- Playwright for browser fallback
- pytest, Docker, GitHub Actions

## What it can do

- Capture a single URL or a batch of up to 100 (`/scrape/bulk`, grouped by site)
- Named extractors for Reddit, LinkedIn, YouTube, Walmart, PubMed; generic fallback for any other site
- Capture modes: `auto`, `mobile`, `structured`, `source`
- Remember cookies and hints per domain, so retries skip challenges already solved
- Return honest capture status: `captured`, `captured_with_warnings`, `no_capture`
- Compare every strategy on one URL (`/scrape/compare`)

## Technical details

Auto mode walks a strategy ladder and stops at the first clean capture:

1. Named source route
2. Raw HTTP with Chrome TLS impersonation
3. Mobile HTTP with Android Chrome fingerprint
4. Crawler personas (Googlebot, Bingbot, DuckDuckBot)
5. TLS impersonation fallback
6. Playwright browser capture (last; disable with `allow_browser=false`)

- Every attempt has its own timeout
- Auth via `X-API-Key` header; Docker image refuses to start without a key (`YADA_REQUIRE_API_KEY=1`)
- Only public HTTP(S) URLs on ports 80/443; loopback and private ranges are rejected before any request
- Credentials are stripped on cross-origin redirects
- Caps: URL 4096 chars, response 8 MiB
- Test suite: 246 offline tests, no network needed; CI runs on every push

## Usage

Local:

```bash
git clone https://github.com/karnyadavdev/YADA-Scraper-API.git
cd YADA-Scraper-API
pip install -r requirements.txt
playwright install chromium
python yada_scraper.py
```

```bash
export YADA_API_KEY="$(openssl rand -hex 32)"
curl -H "X-API-Key: $YADA_API_KEY" "http://localhost:8000/scrape?url=example.com"
```

Docker:

```bash
export YADA_API_KEY="$(openssl rand -hex 32)"
docker compose up -d --build
```

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/scrape?url=...` | capture one URL |
| POST | `/scrape` | same, JSON body |
| GET | `/scrape/compare?url=...` | run every strategy on one URL |
| POST | `/scrape/bulk` | up to 100 URLs |
| GET | `/health` | status and config flags |
| GET | `/memory` | per-domain memory |
| DELETE | `/memory/clear` | reset memory |

Swagger UI: `http://localhost:8000/docs`

| Variable | Meaning | Default |
| --- | --- | --- |
| `YADA_API_KEY` | client key (`X-API-Key`) | none |
| `YADA_REQUIRE_API_KEY` | refuse to start without a key | `0` (Docker sets `1`) |
| `YADA_MAX_URL_LENGTH` | URL length cap | `4096` |
| `YADA_MAX_CONTENT_BYTES` | response size cap | 8 MiB |
| `YADA_BROWSER_SOURCE_CONCURRENCY` | parallel browser captures | `2` |
| `YADA_PROXY` / `YADA_PROXY_OFF` | outbound proxy | off |

## Hosting

Ubuntu server:

```bash
bash deploy/bootstrap.sh <public-ip> <email>
```

Installs Docker and nginx, gets a Let's Encrypt certificate, starts the
API behind HTTPS. Pushes to `main` redeploy through GitHub Actions.

## Use cases

- Feeding clean web content into LLM / RAG pipelines
- Price and availability monitoring across retailers
- Research and content aggregation that needs current page data
- Internal tools that need reliable page text without maintaining scrapers per site
