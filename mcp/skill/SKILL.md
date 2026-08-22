---
name: fortress-stealth-browser
description: >-
  Use when a web fetch is blocked — Cloudflare, DataDome, PerimeterX, Akamai, a 403/429,
  a CAPTCHA/"Press & Hold", "Access denied", an empty JavaScript shell, or a page whose
  data only appears after client-side rendering. Drives a real recompiled-Chromium stealth
  engine (Fortress) via the Fortress MCP to fetch, extract, crawl, recon, search, and
  automate. Also use to get clean markdown/tables/JSON from any page, or a CDP endpoint to
  run existing Playwright/Puppeteer/browser-use code through the stealth browser.
---

# Fortress stealth-browser skill

## When to use this
Reach for the `fortress` MCP tools the moment a normal fetch fails or returns junk:
- HTTP **403 / 429 / 503**, a Cloudflare "Just a moment", DataDome/PerimeterX walls,
  "Access to this page has been denied", "Press & Hold to confirm you are human".
- The page is a **JavaScript SPA** and the plain HTML is an empty shell.
- You need **structured data** (markdown, tables, or a schema-shaped record), a whole-site
  **crawl**, or the site's **private JSON API**.
- You have Playwright/Puppeteer/browser-use code that keeps getting **detected** — get a
  stealth **CDP endpoint** and point your code at it.

## Setup
```bash
pip install "tilion[mcp]"
```
Add to the client MCP config: `{ "mcpServers": { "fortress": { "command": "tilion-mcp" } } }`

## Tool cheat-sheet (pick by intent)
- **Blocked / need the page** → `fetch_protected_page(url)` → `{status, http_status, blocked, waf, title, text}`.
- **Clean content** → `read_page(url)` (markdown) or `extract_page(url, schema?)` (record).
- **A file (PDF/DOCX/XLSX/CSV)** → `extract_document(source)`.
- **Whole site** → `crawl_site(url, depth, max_pages)`.
- **Find the data source** → `recon_site_apis(url)` → discovered XHR/JSON endpoints.
- **Search the web** → `search_web(query, count)` (real browser, no SERP key).
- **Interact** → `page_elements` → `click_button` / `fill_field` / `press_key` / `wait_for`
  / `evaluate_js`; check with `current_page`.
- **Multi-step flow** (login, paginate, infinite-scroll, checkout) →
  `list_browser_tasks()` then `run_browser_task(task, url, params?)`.
- **Identify the anti-bot** → `detect_waf(url)` → `{vendor, challenged, strategy}`.
- **Solve a captcha** → `solve_captcha(url)` (needs `CAPTCHA_API_KEY`).
- **Check egress** → `get_egress_info()` → the real public IP the target sees.
- **Auth reuse** → `save_profile(name)` / `load_profile(name)`.
- **Capture** → `screenshot_page` / `save_page(format=pdf|html|text|png)` / `download_file`.
- **Bring your own automation** → `get_stealth_cdp_endpoint()` → a CDP url for
  Playwright / Puppeteer / browser-use / Crawl4AI.

## Handling a block (the golden path)
`fetch_protected_page` returns a `waf` field — **read it, don't guess**:
- `status:"ok"` → you have the page; extract/read/act.
- `status:"empty"` → JS withheld content; retry or use `run_browser_task` to interact.
- `status:"blocked"` → check `waf.vendor`: **cloudflare/incapsula** usually auto-clears (retry
  once); **datadome/perimeterx** are behavioral (fetch already nudged — if still blocked you
  need residential egress); **akamai/kasada** are IP-gated → set `TILION_PROXY` and retry; a
  **captcha** present → `solve_captcha` (or fetch auto-solves with `CAPTCHA_API_KEY`).

## Configure the hard-target levers (env)
- `TILION_PROXY=http://user:pass@host:port` — residential/mobile egress (the #1 unblock lever;
  verify with `get_egress_info`). `TILION_REGION=us` aligns timezone/locale to the IP.
- `CAPTCHA_API_KEY=...` (+ `CAPTCHA_PROVIDER=2captcha|anticaptcha|capsolver`) — auto-solve.

## Good habits
- Prefer `extract_page`/`read_page` over dumping raw HTML — outputs are capped and clean.
- For an authenticated site, `load_profile` before fetching so cookies + solved challenges
  are reused.
- Reads are safe/auto-approved; `run_browser_task`, `save_page`, `download_file` write or
  act — confirm intent first.
- A great fingerprint on a datacenter IP can still be blocked by the hardest sites; on a
  residential IP the local engine clears most walls. Hosted residential egress is coming soon.

## Reference
- **[`mcp/USAGE.md`](../USAGE.md)** — the full 29-tool guide: per-tool detail, workflow
  recipes, result-reading, and config.
- **[`mcp/README.md`](../README.md)** — install, tool table, benchmarks.
