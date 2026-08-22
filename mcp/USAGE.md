# Fortress MCP — usage guide (all 29 tools)

The **Fortress MCP** (`io.github.tiliondev/fortress`, config key `fortress`, command `tilion-mcp`)
gives an AI agent a real recompiled-Chromium stealth browser. This is the complete guide to
driving it well: the mental model, every tool, and copy-paste workflows.

> **Name recap** — Registry: `io.github.tiliondev/fortress` · Display: "Fortress Stealth
> Browser" · Client config key: `fortress` · Command: `tilion-mcp` / `npx tilion-mcp`.

---

## 1. Setup

```bash
pip install "tilion[mcp]"      # then command: tilion-mcp
#   —or, zero-install—
npx -y tilion-mcp
```

**Claude Desktop / Cursor / Cline / Windsurf** (`claude_desktop_config.json`, `~/.cursor/mcp.json`, …):
```json
{ "mcpServers": { "fortress": { "command": "tilion-mcp" } } }
```
**Claude Code (CLI):** `claude mcp add fortress -- tilion-mcp`

Optional env (see §6) enables **residential proxy** and **captcha solving**.

---

## 2. The mental model — when to reach for this

Use Fortress the moment a normal fetch **fails or returns junk**:
- HTTP **403 / 429 / 503**, a Cloudflare "Just a moment", a DataDome/PerimeterX wall,
  "Access denied", "Press & Hold", or a CAPTCHA.
- The page is a **JavaScript SPA** and plain HTML is an empty shell.
- You need **structured data**, a whole-site **crawl**, or a site's **private JSON API**.
- Your Playwright/Puppeteer/browser-use code keeps getting **detected**.

**The golden path when a target is hard:**
```
fetch_protected_page(url)
  ├─ status "ok"      → you have the page; extract / read / act
  ├─ status "empty"   → JS withheld content; retry, or use run_browser_task for interaction
  └─ status "blocked" → read the `waf` field it returns:
        vendor cloudflare/incapsula → it usually auto-clears; retry once
        vendor datadome/perimeterx  → behavioral; fetch already nudged — if still blocked,
                                       you likely need residential egress (see get_egress_info)
        vendor akamai               → IP-gated → set TILION_PROXY (residential) and retry
        vendor kasada               → hardest (PoW); needs residential + time
        a captcha present           → solve_captcha (needs CAPTCHA_API_KEY), or fetch auto-solves
```
Every `fetch_protected_page` result includes `{status, http_status, blocked, waf, title, text}`.
Don't guess why you're blocked — **read `waf`** and act on the vendor.

---

## 3. Tools by job (with when/how/returns)

### Get a blocked page
| Tool | When | Returns |
|---|---|---|
| **`fetch_protected_page(url)`** | first move on any protected/JS page | `{status, http_status, blocked, waf, title, text}` |
| **`read_page(url?)`** | you want clean reader-mode **markdown** of the page | `{url, title, markdown, tables}` |
| **`get_page_html(url?)`** | you need the raw rendered HTML | `{url, html}` |
| **`detect_waf(url?)`** | decide the approach *before* committing | `{vendor, challenged, confidence, strategy, signals}` |

### Pull structured data
| Tool | When | Returns |
|---|---|---|
| **`extract_page(url, schema?)`** | page → markdown + tables + metadata, or a **schema-shaped record** (pass `{"name":null,"price":null}` and it fills from JSON-LD/OpenGraph) | `{markdown, tables, metadata, structured}` |
| **`extract_document(source)`** | a PDF/DOCX/XLSX/CSV/HTML **file** (path or URL) | `{markdown, tables, …}` |

### Whole sites / hidden APIs
| Tool | When | Returns |
|---|---|---|
| **`crawl_site(url, depth, max_pages)`** | map/collect a whole site (auto-SPA) | `{pages_crawled, documents, sitemap}` |
| **`recon_site_apis(url, duration)`** | reverse-engineer the site's private XHR/JSON API (it scrolls to trigger lazy calls) | `{total_calls, api_endpoints, auth_signals}` |

### Drive a page (interactive session)
Fetch/read set a **current working page**; these act on it (or on `url=`):
| Tool | When |
|---|---|
| **`page_elements(url?)`** | list buttons / links / fields / headings — the map before you act |
| **`click_button(text, url?)`** | click by visible **text** *or* a CSS selector |
| **`fill_field(field, value, submit?)`** | type into a field (human cadence); `submit=true` presses Enter |
| **`press_key(key)`** | Enter / Tab / Escape / arrows on the working page |
| **`wait_for(selector, timeout_ms)`** | wait for an element before reading |
| **`evaluate_js(expr, url?)`** | run JS and return the result (both `document.title` and `(el||{}).x` idioms work) |
| **`current_page()`** | where did I land? `{url, title}` |

### Multi-step flows
| Tool | When |
|---|---|
| **`run_browser_task(task, url, params?)`** | one of 20 flows: `login`, `paginate_collect`, `infinite_scroll_collect`, `extract_table`, `multi_step_checkout`, `download_file`, … |
| **`list_browser_tasks()`** | the flow names `run_browser_task` accepts |

### Capture / files / auth
| Tool | When |
|---|---|
| **`screenshot_page(url?, path?)`** | PNG of the working (or navigated) page |
| **`save_page(url, path, format)`** | export `pdf` \| `html` \| `text` \| `png` |
| **`download_file(url, filename)`** | download a file with the session's cookies |
| **`get_cookies(url?)`** | the current page's cookies |
| **`save_profile(name)` / `load_profile(name)`** | persist/restore an authenticated session (cookies + storage) — log in once, reuse |

### Anti-bot helpers & escape hatch
| Tool | When |
|---|---|
| **`solve_captcha(url?)`** | detect + solve reCAPTCHA/hCaptcha/Turnstile (needs `CAPTCHA_API_KEY`) |
| **`get_egress_info()`** | confirm proxy/region + the **real public IP** the target sees |
| **`list_tabs()` / `close_tab(index)`** | manage open tabs |
| **`get_stealth_cdp_endpoint()`** | a CDP url to drive the stealth browser from your OWN Playwright/Puppeteer/browser-use |
| **`search_web(query, count)`** | real-browser web search (no SERP key) |

---

## 4. Workflow recipes

**A. Scrape a protected product page → JSON**
```
detect_waf(url)                     # know your enemy (optional)
fetch_protected_page(url)           # get the page (auto WAF strategy + captcha)
extract_page(url, {"name":null,"price":null,"sku":null})   # schema-shaped record
```

**B. Log in once, reuse the session**
```
fetch_protected_page("https://site/login")
page_elements()                     # find the field selectors
fill_field("#user","me")  ·  fill_field("#pass","secret", submit=true)
save_profile("mysite")              # later: load_profile("mysite") before fetching
```

**C. Reverse-engineer a site's API (skip the HTML)**
```
recon_site_apis("https://site/listing", duration=20)   # discovers /api/... endpoints
# then hit the JSON endpoint directly with fetch_protected_page
```

**D. Whole-site crawl**
```
crawl_site("https://docs.site", depth=2, max_pages=100)  # → documents + sitemap
```

**E. Bring your own automation, just add stealth**
```
get_stealth_cdp_endpoint()   → cdp_url
# point Playwright/Puppeteer/browser-use connect_over_cdp at it — your code, undetected browser
```

**F. Hard target still blocked?**
```
get_egress_info()            # if public_ip is a datacenter IP, that's why
# set TILION_PROXY=residential (see §6), restart, retry — IP is the #1 unblock lever
```

---

## 5. Reading results well
- **`status`**: `ok` (real content) · `empty` (loaded nothing real — retry/interact) ·
  `blocked` (bot-wall — read `waf`) · `error` (4xx/5xx — see `http_status`).
- **`waf.vendor` + `waf.strategy`** tell you *how* to approach: behavioral, wait, or proxy.
- **`http_status` + `headers`** are real now — use them for retry/error logic.
- Outputs are **capped** (text ~20k, html ~80k, crawl 200 docs) with `*_truncated` flags so
  your context window never blows up; raise via env if you need more.

---

## 6. Configuration (env)

| Env var | Default | Effect |
|---|---|---|
| `TILION_PROXY` | — | egress proxy `http://user:pass@host:port` (residential/mobile) — **the #1 unblock lever** |
| `TILION_REGION` | — | region hint (e.g. `us`) — aligns timezone/locale to the IP |
| `CAPTCHA_API_KEY` | — | solver key; `fetch` auto-solves + `solve_captcha` works |
| `CAPTCHA_PROVIDER` | `2captcha` | `2captcha` \| `anticaptcha` \| `capsolver` |
| `TILION_MCP_PREWARM` | `1` | boot the browser at startup (first call ~100 ms); `0` = lazy |
| `TILION_MCP_HEADLESS` | `1` | `0` to show a visible window |
| `TILION_MCP_TOOL_TIMEOUT` | `120` | per-tool wall-clock cap (seconds) — raise for big crawls |
| `TILION_ALLOW_PRIVATE_EGRESS` | `0` | `1` to allow localhost/private IPs (SSRF guard off) |

> **Verify egress:** after setting `TILION_PROXY`, call `get_egress_info` — if `public_ip`
> didn't change to the proxy's IP, the engine build isn't honoring the proxy yet (tracked
> separately); don't assume residential egress is active.

---

## 7. Best practices
- **Reuse the working page:** fetch/read set the current page; interactive tools act on it —
  no need to re-navigate for each step.
- **Prefer `extract_page`/`read_page` over `get_page_html`** — clean, capped, agent-friendly.
- **`save_profile` after login** so you authenticate once, not every run.
- **Reads auto-approve; writes gate** — `run_browser_task`, `save_page`, `download_file`,
  `solve_captcha` act or write, so confirm intent.
- **Honest limit:** a perfect fingerprint on a datacenter IP still gets blocked by the
  biggest sites. For those, residential egress (`TILION_PROXY`) is the fix, not a smarter
  prompt. `get_egress_info` tells you which side of that line you're on.
