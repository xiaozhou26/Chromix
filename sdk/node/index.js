// tilion-fortress (Node) — install & drive the Fortress stealth Chromium engine.
// Ships the prebuilt binary only (no engine source). Detects platform, downloads the
// matching bundle from the GitHub Release, verifies SHA256, caches it, launches with CDP.
// macOS/Windows fall back to the Docker image until native binaries are published.
import { spawn, spawnSync } from "node:child_process";
import { createWriteStream, existsSync, chmodSync, mkdirSync, createReadStream } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { pipeline } from "node:stream/promises";
import { createHash } from "node:crypto";

export const VERSION = "151.0.7922.138";
const REPO = "tiliondev/fortress";
// Two release channels. "stable" = Chromium 149 (recommended default — matches the Chrome version
// the mass of real users run). "latest" = 151 (newest engine). Override with { channel } or the
// FORTRESS_CHANNEL env var.
export const CHANNELS = {
  stable: { tag: "v149.0.7827.232", docker: "tilion/fortress:149.0.7827.232" },
  latest: { tag: "v151.0.7922.138",   docker: "tilion/fortress:151.0.7922.138" },
};
const DEFAULT_CHANNEL = process.env.FORTRESS_CHANNEL || "stable";
const CACHE = process.env.FORTRESS_BROWSERS_PATH || join(homedir(), ".cache", "tilion-fortress");
const hostFor = (tag) => process.env.FORTRESS_DOWNLOAD_HOST || `https://github.com/${REPO}/releases/download/${tag}`;
export { CACHE, hostFor, DEFAULT_CHANNEL };

// platform key -> { asset, kind, launcher }
export const ASSETS = {
  "linux-x64": { asset: "tilion-fortress-linux-x64.tar.gz", kind: "tar", launcher: "tilion-fortress/tilion" },
  "win-x64":   { asset: "tilion-fortress-win-x64.zip",       kind: "zip", launcher: "tilion-fortress/tilion.cmd" },
  "mac-arm64": { asset: "tilion-fortress-mac-arm64.tar.gz",  kind: "tar", launcher: "tilion-fortress/tilion" },
  "mac-x64":   { asset: "tilion-fortress-mac-x64.tar.gz",    kind: "tar", launcher: "tilion-fortress/tilion" },
};

export function resolvePlatform() {
  const { platform, arch } = process;
  if (platform === "linux" && arch === "x64") return "linux-x64";
  if (platform === "win32" && arch === "x64") return "win-x64";
  if (platform === "darwin") return arch === "arm64" ? "mac-arm64" : "mac-x64";
  return null;
}

export function personaArgs(persona) {
  if (!persona) return [];
  const map = { platform: "--uxr-platform", timezone: "--uxr-timezone", languages: "--uxr-languages",
    webglRenderer: "--uxr-webgl-renderer", webglVendor: "--uxr-webgl-vendor",
    hwConcurrency: "--uxr-hw-concurrency", deviceMemory: "--uxr-device-memory",
    screenWidth: "--uxr-screen-width", screenHeight: "--uxr-screen-height", canvasSeed: "--uxr-canvas-seed" };
  return Object.entries(persona).map(([k, v]) => `${map[k] || `--uxr-${camelToKebab(k)}`}=${v}`);
}

export function fingerprintArgs(fp) {
  if (!fp) return [];
  const map = { seed: "--fingerprint", platform: "--fingerprint-platform",
    gpuVendor: "--fingerprint-gpu-vendor", gpuRenderer: "--fingerprint-gpu-renderer",
    hardwareConcurrency: "--fingerprint-hardware-concurrency", deviceMemory: "--fingerprint-device-memory",
    screenWidth: "--fingerprint-screen-width", screenHeight: "--fingerprint-screen-height",
    taskbarHeight: "--fingerprint-taskbar-height", brand: "--fingerprint-brand",
    brandVersion: "--fingerprint-brand-version", platformVersion: "--fingerprint-platform-version",
    timezone: "--fingerprint-timezone", locale: "--fingerprint-locale",
    location: "--fingerprint-location", storageQuota: "--fingerprint-storage-quota",
    fontsDir: "--fingerprint-fonts-dir", windowsFontMetrics: "--fingerprint-windows-font-metrics",
    fontWhitelist: "--uxr-font-whitelist",
    webrtcIp: "--fingerprint-webrtc-ip", noise: "--fingerprint-noise",
    sapiVoices: "--fingerprint-sapi-voices", allow3pCookies: "--fingerprint-allow-3p-cookies" };
  return Object.entries(fp).map(([k, v]) => `${map[k] || `--fingerprint-${camelToKebab(k)}`}=${v}`);
}

// Switches that would silently degrade stealth if a caller passed them.
const FORBIDDEN_ARGS = new Set([
  // Forces the SwiftShader software rasterizer; its renderer string and
  // render-output hashes are an instant fingerprint match.
  "--enable-unsafe-swiftshader",
]);

function sanitizeArgs(extra) {
  const kept = [], dropped = [];
  for (const a of extra || []) {
    if (FORBIDDEN_ARGS.has(a.split("=")[0])) dropped.push(a);
    else kept.push(a);
  }
  if (dropped.length) process.stderr.write(`[tilion-fortress] dropped stealth-breaking args: ${dropped}\n`);
  return kept;
}

// Timezone by longitude band (approximate but coherent with the proxy geo).
const TZ_BANDS = [[-180,"Etc/GMT+12"],[-165,"Etc/GMT+11"],[-150,"Etc/GMT+10"],[-135,"Etc/GMT+9"],
  [-120,"Etc/GMT+8"],[-105,"Etc/GMT+7"],[-90,"Etc/GMT+6"],[-75,"Etc/GMT+5"],[-60,"Etc/GMT+4"],
  [-45,"Etc/GMT+3"],[-30,"Etc/GMT+2"],[-15,"Etc/GMT+1"],[0,"Etc/GMT"],[15,"Etc/GMT-1"],[30,"Etc/GMT-2"],
  [45,"Etc/GMT-3"],[60,"Etc/GMT-4"],[75,"Etc/GMT-5"],[90,"Etc/GMT-6"],[105,"Etc/GMT-7"],[120,"Etc/GMT-8"],
  [135,"Etc/GMT-9"],[150,"Etc/GMT-10"],[165,"Etc/GMT-11"],[180,"Etc/GMT-12"]];
const tzForLongitude = (lon) => (TZ_BANDS.find(([edge]) => lon < edge) || [180, "Etc/GMT-12"])[1];

async function geoipLookup(proxy, timeoutMs = 10000) {
  try {
    const url = "http://ip-api.com/json/?fields=status,lat,lon,timezone,countryCode";
    const r = await fetch(url, { signal: AbortSignal.timeout(timeoutMs) });
    const data = await r.json();
    if (data.status === "success") return data;
  } catch { /* fail open */ }
  return null;
}

function camelToKebab(s) {
  return s.replace(/([a-z])([A-Z])/g, "$1-$2").toLowerCase();
}

export async function sha256(path) {
  const h = createHash("sha256");
  await pipeline(createReadStream(path), h);
  return h.digest("hex");
}

export async function expectedSha(asset, host) {
  try {
    const r = await fetch(`${host}/SHA256SUMS`);
    if (!r.ok) return null;
    for (const line of (await r.text()).split("\n")) {
      const p = line.trim().split(/\s+/);
      if (p.length === 2 && p[1].replace(/^\*/, "") === asset) return p[0].toLowerCase();
    }
  } catch { /* none */ }
  return null;
}

async function ensureNative(plat, host, tag) {
  const { asset, kind, launcher } = ASSETS[plat];
  const root = join(CACHE, tag, plat);   // cache per release tag so channels don't collide
  const launcherPath = join(root, launcher);
  if (existsSync(launcherPath)) return launcherPath;
  mkdirSync(root, { recursive: true });
  const archive = join(root, asset);
  process.stderr.write(`[tilion-fortress] downloading ${host}/${asset} ...\n`);
  const res = await fetch(`${host}/${asset}`);
  if (!res.ok) throw new Error(`download failed: ${res.status}`);
  await pipeline(res.body, createWriteStream(archive));

  const exp = await expectedSha(asset, host);
  if (exp) {
    const act = await sha256(archive);
    if (act !== exp) throw new Error(`SHA256 mismatch for ${asset}: expected ${exp}, got ${act}`);
    process.stderr.write("[tilion-fortress] SHA256 verified\n");
  } else {
    process.stderr.write("[tilion-fortress] WARNING: no SHA256SUMS published; skipping verification\n");
  }

  if (kind === "tar") {
    if (spawnSync("tar", ["xzf", archive, "-C", root], { stdio: "inherit" }).status !== 0)
      throw new Error("tar extraction failed");
  } else { // zip (Windows): use PowerShell Expand-Archive
    if (spawnSync("powershell", ["-NoProfile", "-Command",
        `Expand-Archive -Force -LiteralPath '${archive}' -DestinationPath '${root}'`],
        { stdio: "inherit" }).status !== 0) throw new Error("zip extraction failed");
  }
  if (!launcher.endsWith(".cmd") && existsSync(launcherPath)) chmodSync(launcherPath, 0o755);
  if (!existsSync(launcherPath)) throw new Error(`launcher missing after extract: ${launcherPath}`);
  return launcherPath;
}

async function assetExists(plat, host) {
  try { return (await fetch(`${host}/${ASSETS[plat].asset}`, { method: "HEAD" })).ok; }
  catch { return false; }
}

// Exported for the CloakBrowser-compat layer (sdk/node/cloak.js), which needs
// to drive the download for playwright's executable_path directly.
export { ensureNative };

async function waitCdp(port, timeoutMs = 90000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try { const r = await fetch(`http://127.0.0.1:${port}/json/version`); if (r.ok) return (await r.json()).webSocketDebuggerUrl; }
    catch {}
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error("Fortress CDP endpoint did not come up");
}

export class Fortress {
  constructor({ port = 9222, persona = null, extraArgs = [], headless = true, channel = DEFAULT_CHANNEL,
                fingerprint = null, geoip = false, proxy = null, gpuBlocklist = true } = {}) {
    if (!CHANNELS[channel]) throw new Error(`unknown channel '${channel}'; use one of ${Object.keys(CHANNELS)}`);
    const { tag, docker } = CHANNELS[channel];
    this.persona = { ...(persona || {}) };
    this.fingerprint = fingerprint;
    this.proxy = proxy;
    this.extraArgs = sanitizeArgs(extraArgs);
    // Keep the GPU off the software-fallback path: with the blocklist active,
    // an unsupported host GPU falls back to SwiftShader whose renderer string
    // and render hashes are an instant tell.
    if (gpuBlocklist && !this.extraArgs.includes("--ignore-gpu-blocklist"))
      this.extraArgs.push("--ignore-gpu-blocklist");
    this.geoipPending = geoip;
    Object.assign(this, { port, headless, channel, tag, docker, host: hostFor(tag),
                          proc: null, dockerName: null, cdpUrl: null });
  }
  static async launch(opts) { return new Fortress(opts).start(); }

  // Align timezone/locale with the egress IP (geoip: true). Overrides the
  // "match the persona to your egress" rule mechanically; fails open.
  async _applyGeoip() {
    const geo = await geoipLookup(this.proxy);
    if (!geo) { process.stderr.write("[tilion-fortress] geoip lookup failed; persona unchanged\n"); return; }
    if (geo.timezone && this.persona.timezone === undefined) this.persona.timezone = geo.timezone;
    else if (geo.lon !== undefined && this.persona.timezone === undefined)
      this.persona.timezone = tzForLongitude(Number(geo.lon));
    if (geo.countryCode && this.persona.languages === undefined)
      this.persona.languages = geo.countryCode.toLowerCase();
    process.stderr.write(`[tilion-fortress] geoip aligned persona (tz=${this.persona.timezone}, lang=${this.persona.languages})\n`);
  }

  async start() {
    if (this.geoipPending) await this._applyGeoip();
    const plat = resolvePlatform();
    const native = plat && (plat === "linux-x64" || await assetExists(plat, this.host));
    if (native) await this._startNative(plat); else this._startDocker();
    this.cdpUrl = await waitCdp(this.port);
    return this;
  }

  async _startNative(plat) {
    const launcher = await ensureNative(plat, this.host, this.tag);
    const args = [];
    if (this.headless) args.push("--headless=new", "--no-sandbox");
    args.push(`--remote-debugging-port=${this.port}`, `--user-data-dir=${join(CACHE, "profile")}`,
              ...personaArgs(this.persona), ...fingerprintArgs(this.fingerprint), ...this.extraArgs);
    this.proc = spawn(launcher, args, { stdio: "ignore", shell: launcher.endsWith(".cmd") });
  }

  _startDocker() {
    if (spawnSync("docker", ["--version"]).status !== 0)
      throw new Error("No native binary for this platform yet and Docker not installed. Install Docker Desktop or use Linux x64.");
    this.dockerName = `tilion-fortress-${process.pid}-${this.port}`;
    const args = ["run", "-d", "--rm", "--name", this.dockerName, "-p", `${this.port}:9222`, this.docker,
      ...personaArgs(this.persona), ...fingerprintArgs(this.fingerprint), ...this.extraArgs];
    if (spawnSync("docker", args, { stdio: "ignore" }).status !== 0) throw new Error("docker run failed");
  }

  async close() {
    if (this.proc) {
      // On Windows the launcher runs under cmd.exe (shell:true); proc.kill() would
      // only reap the shell and orphan chrome.exe, so kill the whole process tree.
      if (process.platform === "win32" && this.proc.pid)
        spawnSync("taskkill", ["/F", "/T", "/PID", String(this.proc.pid)], { stdio: "ignore" });
      else this.proc.kill();
      this.proc = null;
    }
    if (this.dockerName) { spawnSync("docker", ["rm", "-f", this.dockerName], { stdio: "ignore" }); this.dockerName = null; }
  }
}
export default Fortress;

// CloakBrowser-compatible API — `import { launch } from "tilion-fortress"` and
// existing cloakbrowser scripts work unchanged (see cloak.js for the mapping).
export {
  launch, launchContext, launchPersistentContext,
  buildLaunchOptions, buildContextOptions, humanizeBrowser, humanizePage,
  ensureBinary, clearCache, binaryInfo, checkForUpdate,
  getDefaultStealthArgs, CHROMIUM_VERSION, DEFAULT_VIEWPORT,
} from "./cloak.js";
