"""The Fortress stealth-browser MCP server.

Tool descriptions are deliberately intent-loaded: they name the failure the agent
is experiencing (blocked, 403, Cloudflare, DataDome, PerimeterX, bot detection) so
the model selects this tool the moment a fetch/automation gets blocked.

One shared Tilion instance backs every tool (lazy-started on first use, closed at
process exit). Local mode by default; TILION_BASE_URL/TILION_API_KEY switch to the
hosted fleet.
"""
from __future__ import annotations

import asyncio
import functools
import os
import sys
from contextlib import asynccontextmanager

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from tilion.facade import Tilion

# Tool safety hints (clients auto-approve reads, gate writes/destructive). Everything
# touches the live internet → openWorldHint; list_browser_tasks is pure-local.
_READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
_LOCAL = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
_WRITE = ToolAnnotations(readOnlyHint=False, openWorldHint=True)
_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)


def _env(name: str, default: str | None = None) -> str | None:
    """Read a config var tolerant of BOTH brand prefixes during the migration:
    the new one-L `TILION_*` first, then the legacy two-L `TILLION_*` the server
    core still uses — so an operator isn't silently unconfigured across the
    facade/server boundary. (The arham->tilion rename unifies these.)"""
    return os.environ.get(f"TILION_{name}", os.environ.get(f"TILLION_{name}", default))


# Per-tool wall-clock cap: a hung nav/crawl must NOT wedge the shared browser (it
# holds a per-origin lock). On timeout the coroutine is cancelled (unwinding the
# lock) and a structured error is returned. Tune via env.
_TOOL_TIMEOUT = float(_env("MCP_TOOL_TIMEOUT", "120"))


def _safe(fn):
    """Error boundary for a tool: enforce a wall-clock timeout AND on any failure
    return a STRUCTURED error the agent can reason about ({status:'error', error})
    instead of an opaque protocol error, so a bad/slow call never crashes or wedges
    the server. Preserves the signature so FastMCP introspects it."""
    @functools.wraps(fn)
    async def wrap(*args, **kwargs):
        try:
            return await asyncio.wait_for(fn(*args, **kwargs), timeout=_TOOL_TIMEOUT)
        except asyncio.TimeoutError:
            return {"status": "error",
                    "error": f"tool timed out after {_TOOL_TIMEOUT:.0f}s "
                             f"(raise TILION_MCP_TOOL_TIMEOUT for slow crawls)"}
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all at the tool boundary
            return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    return wrap


def _ip_blocked(ip_str: str) -> bool:
    import ipaddress
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    # normalise IPv4-mapped IPv6 (::ffff:169.254.169.254) to the v4 checks
    if getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


async def _check_url(url: str) -> None:
    """SSRF guard for the local MCP: refuse localhost / private / cloud-metadata
    targets unless the operator opts in with TILION_ALLOW_PRIVATE_EGRESS=1.

    DNS resolution goes through the event loop's ASYNC resolver — a blocking
    socket.getaddrinfo here would stall every concurrent tool call and could not
    be cancelled by the per-tool timeout."""
    if _env("ALLOW_PRIVATE_EGRESS", "0") == "1":
        return
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").strip("[]")
    if not host:
        raise ValueError(f"invalid url: {url!r}")
    if host.lower() in ("localhost", "metadata.google.internal"):
        raise ValueError(f"refused private/metadata host {host!r} "
                         "(set TILION_ALLOW_PRIVATE_EGRESS=1 to allow)")
    if _ip_blocked(host):                     # host is already a literal IP
        raise ValueError(f"refused private/internal address {host!r} "
                         "(set TILION_ALLOW_PRIVATE_EGRESS=1 to allow)")
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, None)
    except Exception:
        return  # unresolvable → let the browser surface a clean nav error
    for info in infos:
        if _ip_blocked(info[4][0]):
            raise ValueError(f"refused private/internal address for {host!r} "
                             "(set TILION_ALLOW_PRIVATE_EGRESS=1 to allow)")


def _write_root() -> str:
    """The directory tool-written files are confined to. Override with
    TILION_MCP_WRITE_DIR; defaults to <tempdir>/tilion-mcp."""
    import tempfile
    root = _env("MCP_WRITE_DIR") or os.path.join(tempfile.gettempdir(), "tilion-mcp")
    os.makedirs(root, exist_ok=True)
    return os.path.abspath(root)


def _confine_path(path: str | None, suffix: str) -> str:
    """Confine a tool-supplied write path to the sandbox root — blocks path
    traversal / overwriting arbitrary files (e.g. ~/.ssh/authorized_keys). Only the
    basename is used. Set TILION_MCP_ALLOW_ANY_PATH=1 to write anywhere (trusted
    local use). A None path yields a fresh temp name in the root."""
    import tempfile
    root = _write_root()
    if _env("MCP_ALLOW_ANY_PATH", "0") == "1" and path:
        return path
    if not path:
        fd, p = tempfile.mkstemp(suffix=suffix, prefix="tilion_", dir=root)
        os.close(fd)
        return p
    name = os.path.basename(path) or ("out" + suffix)
    if not os.path.splitext(name)[1]:
        name += suffix
    return os.path.join(root, name)


async def _log(ctx, msg: str) -> None:
    """Best-effort server→client log. Never breaks a tool if the client has no
    logging capability (or there's no active session, e.g. in tests)."""
    if ctx is not None:
        try:
            await ctx.info(msg)
        except Exception:
            pass


async def _check_params_urls(params: dict | None) -> None:
    """Defense-in-depth: SSRF-check any URL-looking values a flow might navigate to."""
    for v in (params or {}).values():
        if isinstance(v, str) and v[:8].lower().startswith(("http://", "https:/")):
            await _check_url(v)


@asynccontextmanager
async def _lifespan(_server):
    """Pre-warm the stealth browser at server startup so the agent's FIRST tool
    call is a ~150ms warm hit, not a ~2.2s cold launch. Disable with
    TILION_MCP_PREWARM=0. Shuts the browser down cleanly on exit."""
    if _env("MCP_PREWARM", "1") != "0":
        try:
            await _t()
        except Exception:
            pass  # never let a pre-warm failure block the server
    try:
        yield
    finally:
        global _tilion
        if _tilion is not None:
            try:
                await _tilion.close()
            except Exception:
                pass
            _tilion = None


mcp = FastMCP("Fortress Stealth Browser", lifespan=_lifespan)

# ── one shared browser for the whole server ─────────────────────────────────
# NOTE: an ASYNCIO lock, not a threading lock — holding a threading.Lock across an
# `await` deadlocks the event loop when two tool calls arrive concurrently. In
# 3.10+ asyncio.Lock() binds to the running loop lazily, so a module-level instance
# is safe.
_tilion: Tilion | None = None
_lock: asyncio.Lock | None = None


async def _t() -> Tilion:
    """Return the shared Tilion instance, starting it on first use and REBUILDING
    it if the browser has died (crash / dropped CDP). Concurrency-safe."""
    global _tilion, _lock
    if _lock is None:            # bind lazily to the running loop (atomic: no await)
        _lock = asyncio.Lock()
    async with _lock:
        if _tilion is not None and not _tilion.is_healthy():
            try:
                await _tilion.close()
            except Exception:
                pass
            _tilion = None
        if _tilion is None:
            base_url = _env("BASE_URL")
            api_key = _env("API_KEY")
            headless = (_env("MCP_HEADLESS", "1") != "0")
            # Egress + captcha come from env so an agent host can configure a residential
            # proxy and a captcha key without changing the tool surface. TILION_PROXY /
            # TILION_REGION / CAPTCHA_API_KEY are read by the facade itself; pass any
            # explicit ones through here too.
            inst = Tilion(
                headless=headless, base_url=base_url, api_key=api_key,
                proxy=_env("PROXY"),                 # TILION_PROXY
                region=_env("REGION"),               # TILION_REGION
                captcha_api_key=os.environ.get("CAPTCHA_API_KEY"),
                captcha_provider=os.environ.get("CAPTCHA_PROVIDER", "2captcha"),
            )
            await inst.start()
            _tilion = inst
    return _tilion


# ── tools ───────────────────────────────────────────────────────────────────

def _cap(d: dict, limits: dict[str, int]) -> dict:
    """Trim oversized string fields so a tool response never floods the agent's
    context. Drops the raw-HTML dump by default (agents want text/markdown), and
    flags anything truncated."""
    out = dict(d)
    for field, n in limits.items():
        v = out.get(field)
        if isinstance(v, str) and len(v) > n:
            out[field] = v[:n]
            out[f"{field}_truncated"] = True
    return out


@mcp.tool(annotations=_READ)
@_safe
async def fetch_protected_page(url: str) -> dict:
    """Fetch a web page that normal HTTP clients / headless browsers CAN'T get —
    pages behind Cloudflare, DataDome, PerimeterX, Akamai, or that return 403 /
    'Access Denied' / 'Are you a robot' / CAPTCHA challenges.

    Use this the moment a requests/httpx/fetch call is blocked, rate-limited, or
    returns a bot-detection interstitial instead of the real content. It drives a
    real recompiled-Chromium stealth browser (Fortress) that passes bot checks,
    waits out the JS challenge, and returns the resolved HTML + text.

    Returns {url, status ('ok'|'blocked'), title, text, blocked}. (Raw HTML is
    dropped from the response to save context — use extract_page for structured
    content.)
    """
    await _check_url(url)
    t = await _t()
    r = await t.fetch(url)
    r.pop("html", None)                       # agents want text, not a raw-HTML dump
    return _cap(r, {"text": 20000})


@mcp.tool(annotations=_DESTRUCTIVE)
@_safe
async def run_browser_task(task: str, url: str, params: dict | None = None) -> dict:
    """Run a multi-step browser automation on a stealth browser that won't get
    flagged as a bot — logging in, paginating, infinite-scroll collection, table
    extraction, form fill, file download, multi-step checkout, SPA navigation.

    Use when a task needs real interaction (not just a fetch) on a site that
    detects/blocks automation. `task` is a flow name from `list_browser_tasks`;
    `params` carries flow arguments (selectors, credentials, etc).
    """
    valid = set(Tilion.list_tasks())
    if task not in valid:
        return {"status": "error",
                "error": f"unknown task {task!r}; call list_browser_tasks for valid names"}
    if url:
        await _check_url(url)
    await _check_params_urls(params)          # SSRF-check URL-bearing flow params too
    t = await _t()
    return await t.agent(task, url=url, **(params or {}))


@mcp.tool(annotations=_LOCAL)
@_safe
async def list_browser_tasks() -> list[str]:
    """List the browser-automation flow names accepted by run_browser_task
    (login, paginate_collect, infinite_scroll_collect, extract_table,
    multi_step_checkout, download_file, navigate_spa, ...)."""
    return Tilion.list_tasks()


@mcp.tool(annotations=_READ)
@_safe
async def search_web(query: str, count: int = 10) -> dict:
    """Run a web search through a real stealth browser (not a paid SERP API) and
    return organic results. Useful when you need fresh search results and SERP
    APIs are unavailable or the search engine blocks scripted queries.

    Returns {engine, query, results:[{title, url, snippet}...]}.
    """
    count = max(1, min(int(count), 50))
    t = await _t()
    return await t.search(query, count=count)


@mcp.tool(annotations=_READ)
@_safe
async def extract_page(url: str, schema: dict | None = None) -> dict:
    """Extract clean, LLM-ready content from a page that may be behind bot
    detection — returns markdown + tables + metadata (or a schema-shaped record if
    `schema` is given). Use instead of raw HTML scraping when you want structured,
    readable content and the site blocks normal scrapers.
    """
    await _check_url(url)
    t = await _t()
    r = await t.extract(url, schema=schema)
    r.pop("html", None)
    return _cap(r, {"markdown": 40000, "text": 20000})


@mcp.tool(annotations=_READ)
@_safe
async def crawl_site(url: str, depth: int = 2, max_pages: int = 100,
                     ctx: Context | None = None) -> dict:
    """Crawl a whole website (BFS, auto-handles SPA/JS + lazy-load) through the
    stealth browser and return every discovered page/document + a sitemap. Use to
    map or harvest a site that blocks conventional crawlers.
    """
    await _check_url(url)
    depth = max(1, min(int(depth), 5))            # clamp: unbounded depth can run away
    max_pages = max(1, min(int(max_pages), 500))
    if ctx:
        await _log(ctx, f"crawling {url} (depth {depth}, up to {max_pages} pages)…")
    t = await _t()
    r = await t.crawl(url, depth=depth, max_pages=max_pages)
    if ctx:
        await _log(ctx, f"crawled {r.get('pages_crawled', 0)} pages, {r.get('document_count', 0)} documents")
    docs = r.get("documents")                     # cap the doc list so a big crawl can't flood context
    if isinstance(docs, list) and len(docs) > 200:
        r["documents"] = docs[:200]
        r["documents_truncated"] = True
    return r


@mcp.tool(annotations=_READ)
@_safe
async def recon_site_apis(url: str, duration: float = 20.0,
                          ctx: Context | None = None) -> dict:
    """Reverse-engineer a site's PRIVATE API: drive the page in a stealth browser
    and capture the XHR/fetch endpoints, auth flow, and JSON traffic behind it
    (secret-scrubbed). Use when you want to hit a site's real backend API directly
    instead of scraping HTML.
    """
    await _check_url(url)
    duration = max(1.0, min(float(duration), 60.0))   # clamp: capture window bounded
    if ctx:
        await _log(ctx, f"capturing {url} network for {duration:.0f}s…")
    t = await _t()
    r = await t.recon(url, duration=duration)
    eps = r.get("api_endpoints")                       # cap endpoint list for context safety
    if isinstance(eps, list) and len(eps) > 100:
        r["api_endpoints"] = eps[:100]
        r["api_endpoints_truncated"] = True
    return r


@mcp.tool(annotations=_WRITE)
@_safe
async def screenshot_page(url: str, path: str | None = None) -> dict:
    """Screenshot a page (that may be behind bot detection) as PNG. Writes to
    `path` if given, else returns base64 (large images are saved to a temp file
    instead, to avoid flooding context). Use for visual verification/QA."""
    await _check_url(url)
    if path is not None:
        path = _confine_path(path, ".png")       # sandbox the write path
    t = await _t()
    r = await t.screenshot(url, path=path)
    b64 = r.get("png_base64")
    if b64 and len(b64) > 200_000:               # ~150KB image → too big to inline
        import base64
        tmp = _confine_path(None, ".png")
        with open(tmp, "wb") as fh:
            fh.write(base64.b64decode(b64))
        r.pop("png_base64", None)
        r["path"] = tmp
        r["note"] = "image too large to inline; saved to a file (pass path= to name it)"
    return r


@mcp.tool(annotations=_WRITE)
@_safe
async def save_page(url: str, path: str, format: str = "pdf") -> dict:
    """Save a page (behind bot detection or not) as pdf | html | text | screenshot
    to `path`. Use to archive/export a protected page as a PDF."""
    await _check_url(url)
    allowed = {"pdf", "html", "text", "screenshot", "png"}
    if format not in allowed:
        return {"status": "error", "error": f"format must be one of {sorted(allowed)}"}
    ext = {"text": ".txt", "screenshot": ".png"}.get(format, "." + format)
    path = _confine_path(path, ext)              # sandbox the write path
    t = await _t()
    return await t.save(url, path, format=format)


@mcp.tool(annotations=_READ)
@_safe
async def read_page(url: str) -> dict:
    """Get clean, reader-mode MARKDOWN of ANY web page (protected or not) — the
    readable article/content as markdown plus any tables. Use to read a page's
    content without the HTML noise. Returns {url, title, markdown, tables}.
    """
    await _check_url(url)
    t = await _t()
    r = await t.read(url)
    return _cap(r, {"markdown": 40000})


@mcp.tool(annotations=_READ)
@_safe
async def page_elements(url: str) -> dict:
    """Map a page's interactive STRUCTURE — its buttons, links, form fields, and
    headings (the layout/design overview). Use to discover what you can click or
    fill before calling click_button / fill_field. Returns
    {url, buttons[], links[], fields[], headings[]}.
    """
    await _check_url(url)
    t = await _t()
    return await t.elements(url)


@mcp.tool(annotations=_WRITE)
@_safe
async def click_button(text: str, url: str | None = None) -> dict:
    """Click a BUTTON or link by its visible text on the current page (navigates to
    `url` first if given). Use to press buttons, submit, open menus, follow links.
    Returns the resulting {clicked, url, title}.
    """
    if url:
        await _check_url(url)
    t = await _t()
    return await t.click(text, url=url)


@mcp.tool(annotations=_WRITE)
@_safe
async def fill_field(field: str, value: str, url: str | None = None,
                     submit: bool = False) -> dict:
    """Type `value` into a form FIELD (CSS selector, e.g. 'input[name=q]') on the
    current page (navigates to `url` first if given). Set submit=True to press Enter
    and submit — the reliable way to submit a search box. Pair with page_elements to
    find field selectors.
    """
    if url:
        await _check_url(url)
    t = await _t()
    return await t.fill(field, value, url=url, submit=submit)


@mcp.tool(annotations=_WRITE)
@_safe
async def press_key(key: str) -> dict:
    """Press a key on the current page: Enter (submit a focused field), Tab, Escape
    (dismiss a dialog), ArrowDown, PageDown, etc. Returns the resulting {url}.
    """
    t = await _t()
    return await t.press(key)


@mcp.tool(annotations=_READ)
@_safe
async def current_page() -> dict:
    """Get the current working page's URL and title WITHOUT navigating — use to
    check where you landed after click_button / fill_field(submit=True). Returns
    {open, url, title}.
    """
    t = await _t()
    return await t.current()


@mcp.tool(annotations=_READ)
@_safe
async def extract_document(source: str) -> dict:
    """Extract clean text/markdown + tables from a DOCUMENT — a PDF, DOCX, XLSX, CSV,
    or HTML file, given a local path OR a URL (even one behind bot detection). Use for
    reports, datasheets, filings, or any file the agent must read.
    """
    t = await _t()
    is_url = source[:4].lower() == "http"
    if is_url:
        await _check_url(source)
    r = await (t.extract_url(source) if is_url else t.extract_file(source))
    r.pop("html", None)
    return _cap(r, {"markdown": 40000, "text": 20000})


@mcp.tool(annotations=_READ)
@_safe
async def get_page_html(url: str) -> dict:
    """Get the RAW HTML of a page (behind bot detection or not). Use when you need the
    exact markup rather than cleaned markdown. Returns {url, html} (capped)."""
    await _check_url(url)
    t = await _t()
    return _cap(await t.get_html(url), {"html": 80000})


@mcp.tool(annotations=_READ)
@_safe
async def evaluate_js(expression: str, url: str | None = None) -> dict:
    """Run a JavaScript expression on the current (or a navigated) page and return its
    result. Use for custom scraping the high-level tools don't cover, e.g.
    `Array.from(document.querySelectorAll('.price')).map(e=>e.innerText)`.
    """
    if url:
        await _check_url(url)
    t = await _t()
    return await t.evaluate(expression, url=url)


@mcp.tool(annotations=_READ)
@_safe
async def wait_for(selector: str, url: str | None = None, timeout_ms: int = 10000) -> dict:
    """Wait for a CSS selector to appear (navigates to `url` first if given). Use
    before reading a page whose content loads late via JavaScript."""
    if url:
        await _check_url(url)
    timeout_ms = max(500, min(int(timeout_ms), 60000))
    t = await _t()
    return await t.wait_for(selector, url=url, timeout_ms=timeout_ms)


@mcp.tool(annotations=_WRITE)
@_safe
async def download_file(url: str, filename: str) -> dict:
    """Download a file (preserving the stealth browser's cookies/session) to a
    sandboxed path. Use for PDFs/CSVs/exports behind auth or bot detection.
    Returns {url, path, bytes}."""
    await _check_url(url)
    ext = os.path.splitext(filename)[1] or ".bin"
    dest = _confine_path(filename, ext)
    t = await _t()
    return await t.download(url, dest)


@mcp.tool(annotations=_READ)
@_safe
async def get_cookies(url: str | None = None) -> dict:
    """Get the cookies on the current (or a navigated) page — inspect or export an
    authenticated session. Returns {cookies:[...]}."""
    if url:
        await _check_url(url)
    t = await _t()
    return {"cookies": await t.get_cookies(url=url)}


@mcp.tool(annotations=_WRITE)
@_safe
async def save_profile(name: str = "session") -> dict:
    """Save the current authenticated session (cookies + localStorage) to a named
    profile so you can restore the login later with load_profile — no need to log in
    again. Do this AFTER a successful login (e.g. via run_browser_task)."""
    t = await _t()
    r = await t.save_profile(_confine_path(name, ".json"))
    r["profile"] = name
    return r


@mcp.tool(annotations=_WRITE)
@_safe
async def load_profile(name: str = "session") -> dict:
    """Restore a saved session (cookies + localStorage) from a named profile — reuse a
    login without re-authenticating. Navigate to the site afterward."""
    p = _confine_path(name, ".json")
    if not os.path.exists(p):
        return {"status": "error", "error": f"no profile {name!r} — call save_profile first"}
    t = await _t()
    r = await t.load_profile(p)
    r["profile"] = name
    return r


@mcp.tool(annotations=_READ)
@_safe
async def list_tabs() -> dict:
    """List the open browser tabs (index + url + which is current)."""
    t = await _t()
    return {"tabs": await t.list_tabs()}


@mcp.tool(annotations=_WRITE)
@_safe
async def close_tab(index: int) -> dict:
    """Close a tab by index (from list_tabs)."""
    t = await _t()
    return await t.close_tab(int(index))


@mcp.tool(annotations=_READ)
@_safe
async def detect_waf(url: str | None = None) -> dict:
    """Identify the anti-bot / WAF vendor protecting a page (Cloudflare, DataDome,
    PerimeterX, Akamai, Kasada, Incapsula) on the current page or a freshly-navigated `url`.

    Returns {vendor, challenged, confidence, strategy, signals}. Use it to decide the
    approach before hitting a hard target: `ip_and_wait`/Akamai and `behavior_*`/DataDome+
    PerimeterX want residential egress + human interaction; `wait_challenge`/Cloudflare and
    `pow_wait`/Kasada auto-clear or need time. `fetch_protected_page` already applies the
    right strategy automatically and returns the same `waf` info.
    """
    if url:
        await _check_url(url)
    t = await _t()
    return await t.detect_waf(url)


@mcp.tool(annotations=_WRITE)
@_safe
async def solve_captcha(url: str | None = None) -> dict:
    """Detect and solve a CAPTCHA on the current page (or a freshly-navigated `url`).

    Use when a page is gated by reCAPTCHA/hCaptcha/Turnstile. Requires a solver key in the
    server env (`CAPTCHA_API_KEY`, provider `CAPTCHA_PROVIDER` = 2captcha|anticaptcha|
    capsolver). Returns {detected, kind, solvable, solved}. `fetch_protected_page` already
    auto-solves when a key is set; use this for an explicit retry on an interactive page.
    """
    if url:
        await _check_url(url)
    t = await _t()
    return await t.solve_captcha(url)


@mcp.tool(annotations=_READ)
@_safe
async def get_egress_info() -> dict:
    """Report the current egress config: whether a proxy/region is set and the public IP
    the target actually sees. Use to confirm residential egress is active before hitting a
    hard target (a datacenter IP gets pre-classified as a bot regardless of fingerprint).
    Configure via env: TILION_PROXY=http://user:pass@host:port, TILION_REGION=us.
    """
    t = await _t()
    info = {"proxy_configured": t._proxy is not None,
            "proxy_type": getattr(t._proxy, "type", None),
            "region": t._region,
            "captcha_solver": bool(t._captcha_api_key)}
    try:
        r = await t.fetch("https://api.ipify.org?format=json", timeout_ms=20000)
        import json as _json
        info["public_ip"] = _json.loads(r.get("text", "{}") or "{}").get("ip")
    except Exception:
        info["public_ip"] = None
    return info


@mcp.tool(annotations=_READ)
@_safe
async def get_stealth_cdp_endpoint() -> dict:
    """Return a Chrome DevTools Protocol (CDP) websocket URL for a running stealth
    browser you can connect your OWN automation stack to — browser-use, Playwright,
    Puppeteer, Crawl4AI, Stagehand all accept a `cdp_url`.

    Use this when you already have browser-automation code but keep getting
    detected/blocked: point your existing `connect_over_cdp` / `cdp_url` at this
    endpoint to run the exact same code through the Fortress stealth engine.

    Returns {cdp_url}.
    """
    t = await _t()
    return {"cdp_url": t.cdp_url}


def main() -> None:
    """Console entry point (``tilion-mcp`` / ``python -m tilion.mcp``)."""
    # Windows consoles default to cp1252, so any non-latin1 glyph in a log line raises
    # UnicodeEncodeError and can kill the server. Force UTF-8 on the streams (JSON-RPC is
    # UTF-8 anyway) so no banner/log character can ever crash it. The banner itself is
    # kept ASCII as belt-and-suspenders.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass
    # Banner to STDERR only — stdout is the MCP stdio transport and must stay clean.
    print(
        "Tilion Fortress Stealth-Browser MCP  [Beta]\n"
        "  29 tools | local & free | hosted cloud (residential egress) coming soon\n"
        "  docs: DOCUMENTATION.md - more coming; benchmarks in the README\n"
        "  listening on stdio...",
        file=sys.stderr, flush=True,
    )
    mcp.run()


if __name__ == "__main__":
    main()
