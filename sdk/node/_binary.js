// chromix (Node) — binary management: detect platform, download the matching
// bundle from the GitHub Release, verify SHA256SUMS, cache under ~/.cache/chromix.
import { spawnSync } from "node:child_process";
import { createWriteStream, existsSync, chmodSync, mkdirSync, createReadStream } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { pipeline } from "node:stream/promises";
import { createHash } from "node:crypto";

export const VERSION = "151.0.7922.174";
const REPO = "xiaozhou26/Chromix";
// Two release channels. "stable" = Chromium 149 (matches the version the mass of
// real users run). "latest" = 151 (newest engine). See build/versions.txt.
export const CHANNELS = {
  stable: { tag: "v149.0.7827.200" },
  latest: { tag: "v151.0.7922.174" },
};
export const CACHE = process.env.CHROMIX_CACHE_DIR || join(homedir(), ".cache", "chromix");
export const hostFor = (tag) => process.env.CHROMIX_DOWNLOAD_HOST
  || `https://github.com/${REPO}/releases/download/${tag}`;

// platform key -> { asset, kind, launcher }
export const ASSETS = {
  "linux-x64": { asset: "chromix-linux-x64.tar.gz", kind: "tar", launcher: "chromix/chromix" },
  "win-x64":   { asset: "chromix-win-x64.zip",      kind: "zip", launcher: "chromix/chromix.cmd" },
  "mac-arm64": { asset: "chromix-mac-arm64.tar.gz", kind: "tar", launcher: "chromix/chromix" },
  "mac-x64":   { asset: "chromix-mac-x64.tar.gz",   kind: "tar", launcher: "chromix/chromix" },
};

export function resolvePlatform() {
  const { platform, arch } = process;
  if (platform === "linux" && arch === "x64") return "linux-x64";
  if (platform === "win32" && arch === "x64") return "win-x64";
  if (platform === "darwin") return arch === "arm64" ? "mac-arm64" : "mac-x64";
  return null;
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

export async function ensureNative(plat, host, tag) {
  const { asset, kind, launcher } = ASSETS[plat];
  const root = join(CACHE, tag, plat);   // cache per release tag so channels don't collide
  const launcherPath = join(root, launcher);
  if (existsSync(launcherPath)) return launcherPath;
  mkdirSync(root, { recursive: true });
  const archive = join(root, asset);
  process.stderr.write(`[chromix] downloading ${host}/${asset} ...\n`);
  const res = await fetch(`${host}/${asset}`);
  if (!res.ok) throw new Error(`download failed: ${res.status}`);
  await pipeline(res.body, createWriteStream(archive));

  const exp = await expectedSha(asset, host);
  if (exp) {
    const act = await sha256(archive);
    if (act !== exp) throw new Error(`SHA256 mismatch for ${asset}: expected ${exp}, got ${act}`);
    process.stderr.write("[chromix] SHA256 verified\n");
  } else {
    process.stderr.write("[chromix] WARNING: no SHA256SUMS published; skipping verification\n");
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
