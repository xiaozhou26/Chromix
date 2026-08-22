// Unit tests for the chromix Node SDK binary layer.
//
// Covers the pure, release-critical logic that decides *which* bundle a user
// gets and whether it is trusted — the platform resolver, the SHA256SUMS
// parser, and the hasher — with no network and no browser launch.
//
// Run:  node --test sdk/node/test/*.test.*
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createHash } from "node:crypto";

import { resolvePlatform, sha256, expectedSha, ASSETS } from "../_binary.js";

// --- helpers ---------------------------------------------------------------

// process.platform / process.arch are read at call time by resolvePlatform(), so we can
// swap them for a case and restore afterwards. They are configurable data properties.
function withProcess(platform, arch, fn) {
  const desc = { platform: Object.getOwnPropertyDescriptor(process, "platform"),
                 arch: Object.getOwnPropertyDescriptor(process, "arch") };
  Object.defineProperty(process, "platform", { value: platform, configurable: true });
  Object.defineProperty(process, "arch", { value: arch, configurable: true });
  try { return fn(); }
  finally {
    Object.defineProperty(process, "platform", desc.platform);
    Object.defineProperty(process, "arch", desc.arch);
  }
}

// Swap globalThis.fetch for one call and restore, so expectedSha() can be tested offline.
async function withFetch(impl, fn) {
  const orig = globalThis.fetch;
  globalThis.fetch = impl;
  try { return await fn(); }
  finally { globalThis.fetch = orig; }
}

const okText = (body) => async () => ({ ok: true, text: async () => body });

// --- platform --------------------------------------------------------------

test("resolvePlatform maps supported platform/arch pairs", () => {
  const cases = [
    ["linux", "x64", "linux-x64"],
    ["win32", "x64", "win-x64"],
    ["darwin", "arm64", "mac-arm64"],
    ["darwin", "x64", "mac-x64"],
  ];
  for (const [platform, arch, expected] of cases) {
    assert.equal(withProcess(platform, arch, resolvePlatform), expected, `${platform}/${arch}`);
  }
});

test("resolvePlatform returns null for unsupported combos", () => {
  const cases = [
    ["linux", "arm64"],   // no arm64 Linux bundle yet
    ["linux", "ia32"],
    ["win32", "arm64"],
    ["win32", "ia32"],
    ["freebsd", "x64"],
    ["android", "arm64"],
  ];
  for (const [platform, arch] of cases) {
    assert.equal(withProcess(platform, arch, resolvePlatform), null, `${platform}/${arch}`);
  }
});

// --- checksums -------------------------------------------------------------

test("sha256 matches Node crypto for a known buffer", async () => {
  const dir = mkdtempSync(join(tmpdir(), "chromix-sdk-"));
  const file = join(dir, "blob.bin");
  const data = Buffer.from("chromix".repeat(4096));
  writeFileSync(file, data);
  const expected = createHash("sha256").update(data).digest("hex");
  assert.equal(await sha256(file), expected);
});

test("expectedSha parses the matching asset from SHA256SUMS", async () => {
  const asset = ASSETS["linux-x64"].asset;
  const body = `aa11bb22  ${asset}\ndeadbeef  chromix-win-x64.zip\n`;
  const got = await withFetch(okText(body), () => expectedSha(asset));
  assert.equal(got, "aa11bb22");
});

test("expectedSha handles the sha256sum '*asset' binary marker", async () => {
  const asset = ASSETS["linux-x64"].asset;
  const got = await withFetch(okText(`CAFEF00D *${asset}\n`), () => expectedSha(asset));
  assert.equal(got, "cafef00d"); // lower-cased
});

test("expectedSha returns null when the asset is absent", async () => {
  const body = "aa11bb22  some-other-asset.tar.gz\n";
  const got = await withFetch(okText(body), () => expectedSha(ASSETS["linux-x64"].asset));
  assert.equal(got, null);
});

test("expectedSha returns null on a non-ok response", async () => {
  const got = await withFetch(async () => ({ ok: false, status: 404 }),
                              () => expectedSha(ASSETS["linux-x64"].asset));
  assert.equal(got, null);
});

test("expectedSha swallows a network error instead of throwing", async () => {
  const boom = async () => { throw new Error("network down"); };
  const got = await withFetch(boom, () => expectedSha("anything"));
  assert.equal(got, null);
});

// --- assets table ----------------------------------------------------------

test("ASSETS stays consistent with resolvePlatform", () => {
  // Every key resolvePlatform() can return must exist in ASSETS, and each launcher path must
  // live under chromix/ so extraction lands where ensureNative expects.
  const resolvable = ["linux-x64", "win-x64", "mac-arm64", "mac-x64"];
  for (const key of resolvable) assert.ok(key in ASSETS, `missing asset for ${key}`);
  for (const [plat, { asset, kind, launcher }] of Object.entries(ASSETS)) {
    assert.ok(asset.startsWith("chromix-") && asset.includes(plat), `${plat}: ${asset}`);
    assert.ok(["tar", "zip"].includes(kind), `${plat}: kind ${kind}`);
    assert.ok(launcher.startsWith("chromix/"), `${plat}: launcher ${launcher}`);
  }
});
