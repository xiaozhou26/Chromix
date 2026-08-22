// chromix (Node) — drive the Chromix stealth Chromium engine with a
// CloakBrowser-compatible API.
//
// Mirrors the `cloakbrowser` JS wrapper (github.com/CloakHQ/CloakBrowser) so
// existing CloakBrowser scripts run on Chromix by changing only the import:
//
//   - import { launch } from "cloakbrowser";
//   + import { launch } from "chromix";
//
// Same function names, same option names (camelCase, contextOptions/
// launchOptions nesting), same return types (Playwright Browser / BrowserContext
// via playwright-core — imported lazily so the SDK itself stays
// dependency-free). The --fingerprint-* flags emitted here are normalized to
// the engine's --uxr-* persona switches by patch 0036.
//
// Intentional differences: licenseKey is accepted and ignored (one open tier),
// geoip uses ip-api.com instead of a local GeoLite2 database, and there is no
// puppeteer subpath (use the playwright surface).
import { existsSync, rmSync, readdirSync } from "node:fs";
import { join } from "node:path";
import {
  CHANNELS, CACHE, hostFor, resolvePlatform, ensureNative,
} from "./_binary.js";

export const CHROMIUM_VERSION = "151";
export const DEFAULT_VIEWPORT = { width: 1920, height: 947 };

// ---------------------------------------------------------------------------
// Binary management (CLOAKBROWSER_* env aliases)
// ---------------------------------------------------------------------------

function chromeBinaryPath(plat, tag) {
  const name = plat === "win-x64" ? "chrome.exe" : "chrome";
  return join(CACHE, tag, plat, "chromix", name);
}

function channelFor(browserVersion, releaseChannel) {
  const ver = browserVersion || process.env.CLOAKBROWSER_VERSION;
  const ch = releaseChannel || process.env.CLOAKBROWSER_RELEASE_CHANNEL;
  if (CHANNELS[ch]) return ch;
  if (ver) {
    for (const [name, spec] of Object.entries(CHANNELS))
      if (spec.tag.replace(/^v/, "").startsWith(ver.split(".")[0])) return name;
  }
  return "stable";
}

export async function ensureBinary({ browserVersion, releaseChannel } = {}) {
  const explicit = process.env.CLOAKBROWSER_BINARY_PATH;
  if (explicit) {
    if (!existsSync(explicit)) throw new Error(`CLOAKBROWSER_BINARY_PATH does not exist: ${explicit}`);
    return explicit;
  }
  const plat = resolvePlatform();
  if (!plat) throw new Error("No native Chromix binary for this platform (Linux/Windows x64, macOS); or point CLOAKBROWSER_BINARY_PATH at a local build.");
  const ch = channelFor(browserVersion, releaseChannel);
  const tag = CHANNELS[ch].tag;
  const chrome = chromeBinaryPath(plat, tag);
  if (existsSync(chrome)) return chrome;
  await ensureNative(plat, hostFor(tag), tag);
  if (!existsSync(chrome)) throw new Error(`bundle extracted but chrome binary missing: ${chrome}`);
  return chrome;
}

export function binaryInfo({ browserVersion, releaseChannel } = {}) {
  const ch = channelFor(browserVersion, releaseChannel);
  const tag = CHANNELS[ch].tag;
  const plat = resolvePlatform() || "unknown";
  const path = chromeBinaryPath(plat, tag);
  return { tier: "open-source", version: tag.replace(/^v/, ""), channel: ch,
           platform: plat, path: existsSync(path) ? path : null,
           installed: existsSync(path), cacheDir: CACHE };
}

export function clearCache() { rmSync(CACHE, { recursive: true, force: true }); }

export async function checkForUpdate() {
  const current = CHANNELS.stable.tag;
  let latest = current;
  try {
    const r = await fetch("https://api.github.com/repos/xiaozhou26/Chromix/releases/latest",
      { headers: { Accept: "application/vnd.github+json" } });
    if (r.ok) latest = (await r.json()).tag_name || current;
  } catch { /* offline */ }
  return { currentVersion: current.replace(/^v/, ""), latestVersion: latest.replace(/^v/, ""),
           updateAvailable: latest !== current };
}

// ---------------------------------------------------------------------------
// Stealth args
// ---------------------------------------------------------------------------

export function getDefaultStealthArgs() {
  const seed = 10000 + Math.floor(Math.random() * 90000);
  const base = ["--no-sandbox", `--fingerprint=${seed}`];
  return process.platform === "darwin"
    ? [...base, "--fingerprint-platform=macos"]
    : [...base, "--fingerprint-platform=windows"];
}

export function buildArgs({ stealthArgs = true, extraArgs = [], timezone, locale,
                            headless = true, extensionPaths, startMaximized = false } = {}) {
  const seen = new Map();
  const put = (arg) => seen.set(arg.split("=", 1)[0], arg);
  if (stealthArgs) for (const a of getDefaultStealthArgs()) put(a);
  // Keep the GPU off the software-fallback path (SwiftShader is an instant tell).
  if (!headless || process.platform === "win32") put("--ignore-gpu-blocklist");
  for (const a of extraArgs || []) put(a);
  if (timezone) put(`--fingerprint-timezone=${timezone}`);
  if (locale) { put(`--lang=${locale}`); put(`--fingerprint-locale=${locale}`); }
  if (extensionPaths?.length) {
    const val = extensionPaths.map((p) => resolveAbs(p)).join(",");
    put(`--load-extension=${val}`);
    put(`--disable-extensions-except=${val}`);
  }
  const keys = [...seen.keys()];
  if (startMaximized && !["--start-maximized", "--window-size", "--window-position"].some((k) => keys.includes(k)))
    put("--start-maximized");
  return [...seen.values()];
}

function resolveAbs(p) {
  try { return new URL(`file://${p}`).pathname; } catch { return p; }
}

// ---------------------------------------------------------------------------
// GeoIP + WebRTC exit IP
// ---------------------------------------------------------------------------

function extractProxyUrl(proxy) {
  if (!proxy) return null;
  if (typeof proxy === "string") return proxy;
  let url = proxy.server;
  if (proxy.username && proxy.password) {
    const i = url.indexOf("://");
    const scheme = i > 0 ? url.slice(0, i) : "http";
    const rest = i > 0 ? url.slice(i + 3) : url;
    url = `${scheme}://${proxy.username}:${proxy.password}@${rest}`;
  }
  return url;
}

async function geoipHttp(proxyUrl) {
  const timeoutMs = Number(process.env.CLOAKBROWSER_GEOIP_TIMEOUT_SECONDS || 10) * 1000;
  try {
    const url = "http://ip-api.com/json/?fields=status,timezone,countryCode,query";
    const r = await fetch(url, { signal: AbortSignal.timeout(timeoutMs) });
    const d = await r.json();
    if (d.status === "success")
      return { timezone: d.timezone || null, locale: (d.countryCode || "").toLowerCase() || null, exitIp: d.query || null };
  } catch { /* fail open */ }
  return null;
}

export async function maybeResolveGeoip(geoip, proxy, timezone, locale, args) {
  if (!geoip) return { timezone, locale, exitIp: null };
  if (!timezone && args) {
    const f = args.find((a) => a.startsWith("--fingerprint-timezone="));
    if (f) timezone = f.split("=").slice(1).join("=");
  }
  if (!locale && args) {
    const f = args.find((a) => a.startsWith("--lang=") || a.startsWith("--fingerprint-locale="));
    if (f) locale = f.split("=").slice(1).join("=");
  }
  const geo = await geoipHttp(extractProxyUrl(proxy));
  if (!geo) {
    process.stderr.write("[chromix] geoip lookup failed; timezone/locale unchanged\n");
    return { timezone, locale, exitIp: null };
  }
  return { timezone: timezone ?? geo.timezone, locale: locale ?? geo.locale, exitIp: geo.exitIp };
}

async function resolveWebrtcArgs(args, proxy) {
  if (!args || !args.includes("--fingerprint-webrtc-ip=auto")) return args;
  args = [...args];
  const i = args.indexOf("--fingerprint-webrtc-ip=auto");
  const proxyUrl = extractProxyUrl(proxy);
  if (!proxyUrl) { args.splice(i, 1); return args; }
  const geo = await geoipHttp(proxyUrl);
  if (geo?.exitIp) args[i] = `--fingerprint-webrtc-ip=${geo.exitIp}`;
  else args.splice(i, 1);
  return args;
}

function appendWebrtcExitIp(args, exitIp) {
  if (exitIp && !(args || []).some((a) => a.startsWith("--fingerprint-webrtc-ip")))
    return [...(args || []), `--fingerprint-webrtc-ip=${exitIp}`];
  return args;
}

// ---------------------------------------------------------------------------
// Widevine CDM location — enabled automatically when a CDM is present (same
// policy as CloakBrowser); opt out with CLOAKBROWSER_WIDEVINE=0. The Linux deb
// fetch lives in the Python CLI (`python -m chromix widevine`).
// ---------------------------------------------------------------------------

const WIDEVINE_LAYOUT = {
  "win-x64": ["win_x64", "widevinecdm.dll"],
  "linux-x64": ["linux_x64", "libwidevinecdm.so"],
  "mac-arm64": ["mac_arm64", "libwidevinecdm.dylib"],
  "mac-x64": ["mac_x64", "libwidevinecdm.dylib"],
};

function validCdmDir(dir, plat) {
  const [subdir, lib] = WIDEVINE_LAYOUT[plat] || [];
  if (!subdir) return false;
  return existsSync(join(dir, "manifest.json")) &&
         existsSync(join(dir, "_platform_specific", subdir, lib));
}

export function findWidevineCdm() {
  const plat = resolvePlatform();
  if (!plat) return null;
  const p = process.env.CLOAKBROWSER_WIDEVINE_CDM;
  if (p) {
    if (validCdmDir(p, plat)) return p;
    console.warn(`[chromix] CLOAKBROWSER_WIDEVINE_CDM=${p} is not a valid CDM dir; ignoring`);
  }
  const roots = {
    "win-x64": ["C:\\Program Files\\Google\\Chrome\\Application",
                "C:\\Program Files (x86)\\Google\\Chrome\\Application"],
    "mac-arm64": ["/Applications/Google Chrome.app/Contents/Versions"],
    "mac-x64": ["/Applications/Google Chrome.app/Contents/Versions"],
  }[plat] || [];
  let best = null;
  for (const root of roots) {
    if (!existsSync(root)) continue;
    for (const entry of readdirSync(root)) {
      if (!/^\d/.test(entry)) continue;
      const cdm = join(root, entry, "WidevineCdm");
      if (validCdmDir(cdm, plat) && (!best || entry > best.ver)) best = { dir: cdm, ver: entry };
    }
  }
  return best?.dir || null;
}

// ---------------------------------------------------------------------------
// Launch options assembly
// ---------------------------------------------------------------------------

function splitProxy(proxy) {
  if (!proxy) return undefined;
  if (typeof proxy === "string") {
    const i = proxy.indexOf("://");
    const scheme = i > 0 ? proxy.slice(0, i) : "http";
    const rest = i > 0 ? proxy.slice(i + 3) : proxy;
    const at = rest.lastIndexOf("@");
    if (at > 0) {
      const [username, password] = rest.slice(0, at).split(":");
      const out = { server: `${scheme}://${rest.slice(at + 1)}`, username };
      if (password) out.password = password;
      return out;
    }
    return { server: proxy };
  }
  const out = { server: proxy.server };
  for (const k of ["bypass", "username", "password"]) if (proxy[k]) out[k] = proxy[k];
  return out;
}

function effectiveHeadless(options) {
  return options?.launchOptions?.headless ?? options?.headless ?? true;
}

export function buildContextOptions(options = {}) {
  const headless = effectiveHeadless(options);
  // Context-level locale/timezoneId would route through CDP emulation — strip
  // them and route through the binary flags instead (same policy as CloakBrowser).
  const { locale, timezoneId, ...ctx } = options.contextOptions || {};
  if (locale !== undefined || timezoneId !== undefined)
    console.warn("[chromix] contextOptions.locale/timezoneId ignored — use top-level locale/timezone (binary flag)");
  const viewport = options.viewport !== undefined
    ? options.viewport
    : headless ? DEFAULT_VIEWPORT : null;
  return {
    ...ctx,
    ...(options.userAgent ? { userAgent: options.userAgent } : {}),
    viewport,
    ...(options.colorScheme ? { colorScheme: options.colorScheme } : {}),
  };
}

export async function buildLaunchOptions(options = {}) {
  const headless = effectiveHeadless(options);
  const binary = await ensureBinary(options);
  const { timezone, locale, exitIp } = await maybeResolveGeoip(
    options.geoip, options.proxy, options.timezone ?? options.timezoneId, options.locale, options.args);
  let args = await resolveWebrtcArgs(options.args, options.proxy);
  args = appendWebrtcExitIp(args, exitIp);
  // Widevine / DRM: auto-enable when a CDM is present; CLOAKBROWSER_WIDEVINE=0 opts out.
  if (process.env.CLOAKBROWSER_WIDEVINE !== "0" && !(args || []).some((a) => a.startsWith("--uxr-widevine-cdm"))) {
    const cdm = findWidevineCdm();
    if (cdm) args = [...(args || []), `--uxr-widevine-cdm=${cdm}`];
  }
  const chromeArgs = buildArgs({
    stealthArgs: options.stealthArgs ?? true,
    extraArgs: args,
    timezone, locale, headless,
    extensionPaths: options.extensionPaths,
    startMaximized: options.startMaximized ?? true,
  });
  const proxy = splitProxy(options.proxy);
  return {
    executablePath: binary,
    headless,
    args: chromeArgs,
    ignoreDefaultArgs: ["--enable-automation"],
    ...(proxy ? { proxy } : {}),
    ...options.launchOptions,
  };
}

// ---------------------------------------------------------------------------
// Humanize (behavioral layer)
// ---------------------------------------------------------------------------

const HUMAN_PRESETS = {
  default: { typingDelay: 70, typingSpread: 40, pauseChance: 0.1, wobble: 1.5,
             overshoot: 0.35, minSteps: 8, stepsDivisor: 12, aimDelay: 80,
             hold: 100, mistype: 0.02, scrollPause: 300 },
  careful: { typingDelay: 130, typingSpread: 60, pauseChance: 0.15, wobble: 1.0,
             overshoot: 0.15, minSteps: 10, stepsDivisor: 8, aimDelay: 200,
             hold: 180, mistype: 0.04, scrollPause: 500 },
};

export function resolveHumanConfig(preset = "default", humanConfig) {
  return { ...HUMAN_PRESETS[preset] ?? HUMAN_PRESETS.default, ...(humanConfig || {}) };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const ease = (t) => t * t * (3 - 2 * t);

function bezierPath(p0, p3, steps, rand) {
  const pts = [];
  const dx = p3.x - p0.x, dy = p3.y - p0.y;
  const dist = Math.hypot(dx, dy) || 1;
  const nx = -dy / dist, ny = dx / dist;
  const bow = (0.15 + rand() * 0.35) * dist * (rand() < 0.5 ? -1 : 1);
  const c1 = { x: p0.x + dx * 0.3 + nx * bow * 0.5, y: p0.y + dy * 0.3 + ny * bow * 0.5 };
  const c2 = { x: p0.x + dx * 0.7 + nx * bow * 0.4, y: p0.y + dy * 0.7 + ny * bow * 0.4 };
  for (let i = 1; i <= steps; i++) {
    const t = ease(i / steps), u = 1 - t;
    pts.push({
      x: u * u * u * p0.x + 3 * u * u * t * c1.x + 3 * u * t * t * c2.x + t * t * t * p3.x,
      y: u * u * u * p0.y + 3 * u * u * t * c1.y + 3 * u * t * t * c2.y + t * t * t * p3.y,
    });
  }
  return pts;
}

export function humanizePage(page, cfg) {
  const rand = mulberry32(cfg.seed ?? Math.floor(Math.random() * 2 ** 31));
  const state = { x: 0, y: 0 };
  const mouse = page.mouse, keyboard = page.keyboard;
  const raw = { move: mouse.move.bind(mouse), wheel: mouse.wheel.bind(mouse),
                type: keyboard.type.bind(keyboard), press: keyboard.press.bind(keyboard) };

  async function humanMove(x, y) {
    const dist = Math.hypot(x - state.x, y - state.y);
    if (dist < 1) return;
    const steps = Math.max(cfg.minSteps, Math.floor(dist / cfg.stepsDivisor));
    const duration = 80 + Math.min(620, dist / 2.5) + rand() * 60; // ms
    const pts = bezierPath({ x: state.x, y: state.y }, { x, y }, steps, rand);
    const t0 = Date.now();
    for (let i = 0; i < pts.length; i++) {
      const w = rand() * cfg.wobble;
      await raw.move(pts[i].x + (rand() * 2 - 1) * w, pts[i].y + (rand() * 2 - 1) * w);
      const target = t0 + duration * Math.pow((i + 1) / pts.length, 1.15);
      const wait = target - Date.now();
      if (wait > 0) await sleep(wait);
    }
    if (dist > 60 && rand() < cfg.overshoot) {
      await raw.move(x + 2 + rand() * 4, y + 2 + rand() * 4);
      await sleep(20 + rand() * 30);
    }
    await raw.move(x, y);
    state.x = x; state.y = y;
  }

  mouse.move = async (x, y, ...rest) => { await humanMove(x, y); };
  mouse.click = async (x, y, opts = {}) => {
    await humanMove(x, y);
    await sleep(cfg.aimDelay * (0.5 + rand()));
    await mouse.down({ button: opts.button });
    await sleep(cfg.hold * (0.5 + rand()));
    await mouse.up({ button: opts.button });
  };
  mouse.dblclick = async (x, y, opts = {}) => {
    await humanMove(x, y);
    await sleep(cfg.aimDelay * (0.5 + rand()));
    for (let i = 0; i < 2; i++) {
      await mouse.down({ button: opts?.button });
      await sleep(cfg.hold * (0.5 + rand()));
      await mouse.up({ button: opts?.button });
      if (i === 0) await sleep(50 + rand() * 50);
    }
  };
  mouse.wheel = async (dx, dy) => {
    if (dy) {
      const steps = Math.max(4, Math.floor(Math.abs(dy) / 120));
      let done = 0;
      for (let i = 1; i <= steps; i++) {
        const target = Math.floor(dy * ease(i / steps));
        if (target - done) { await raw.wheel(0, target - done); done = target; }
        await sleep(rand() < 0.06 ? cfg.scrollPause * (0.6 + rand()) : 20 + rand() * 40);
      }
    }
    if (dx) await raw.wheel(dx, 0);
  };
  keyboard.type = async (text, opts = {}) => {
    for (const ch of String(text)) {
      if (/[a-z]/i.test(ch) && rand() < cfg.mistype) {
        await raw.type("qwertyuiopasdfghjklzxcvbnm"[Math.floor(rand() * 26)], { delay: 0 });
        await sleep(150 + rand() * 250);
        await raw.press("Backspace");
        await sleep(80 + rand() * 120);
      }
      await raw.type(ch, { delay: 0 });
      let d = cfg.typingDelay + (rand() * 2 - 1) * cfg.typingSpread;
      if (rand() < cfg.pauseChance) d += 400 + rand() * 600;
      await sleep(Math.max(d, 10));
    }
  };
  keyboard.press = async (...a) => { await sleep(30 + rand() * 40); await raw.press(...a); };
  return page;
}

function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export async function humanizeBrowser(browser, cfg = resolveHumanConfig()) {
  const origNewPage = browser.newPage.bind(browser);
  const origNewContext = browser.newContext.bind(browser);
  browser.newPage = async (...a) => humanizePage(await origNewPage(...a), cfg);
  const humanContext = (ctx) => {
    const np = ctx.newPage.bind(ctx);
    ctx.newPage = async (...a) => humanizePage(await np(...a), cfg);
    return ctx;
  };
  browser.newContext = async (...a) => humanContext(await origNewContext(...a));
  return browser;
}

// ---------------------------------------------------------------------------
// Launch family
// ---------------------------------------------------------------------------

async function loadChromium() {
  for (const mod of ["playwright-core", "playwright"]) {
    try { return (await import(mod)).chromium; } catch { /* try next */ }
  }
  throw new Error("playwright-core or playwright must be installed: npm i playwright-core");
}

export async function launch(options = {}) {
  const chromium = await loadChromium();
  const launchOpts = await buildLaunchOptions(options);
  const browser = await chromium.launch(launchOpts);
  if (options.humanize)
    await humanizeBrowser(browser, resolveHumanConfig(options.humanPreset, options.humanConfig));
  return browser;
}

export async function launchContext(options = {}) {
  const chromium = await loadChromium();
  const browser = await chromium.launch(await buildLaunchOptions(options));
  const ctx = await browser.newContext(buildContextOptions(options));
  if (options.humanize) {
    const cfg = resolveHumanConfig(options.humanPreset, options.humanConfig);
    const np = ctx.newPage.bind(ctx);
    ctx.newPage = async (...a) => humanizePage(await np(...a), cfg);
  }
  const origClose = ctx.close.bind(ctx);
  ctx.close = async (...a) => { await origClose(...a); await browser.close(); };
  return ctx;
}

export async function launchPersistentContext(options = {}) {
  const chromium = await loadChromium();
  if (!options.userDataDir) throw new Error("launchPersistentContext requires options.userDataDir");
  const ctxOpts = buildContextOptions(options);
  const ctx = await chromium.launchPersistentContext(
    options.userDataDir, { ...(await buildLaunchOptions(options)), ...ctxOpts });
  if (options.humanize) {
    const cfg = resolveHumanConfig(options.humanPreset, options.humanConfig);
    for (const p of ctx.pages()) humanizePage(p, cfg);
    const np = ctx.newPage.bind(ctx);
    ctx.newPage = async (...a) => humanizePage(await np(...a), cfg);
  }
  return ctx;
}
