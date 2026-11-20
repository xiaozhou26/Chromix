#!/usr/bin/env python3
"""Replay exact Chromium157 source subsets independently of production selection."""
from __future__ import annotations

import argparse
from collections import Counter
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import apply_restored_patches as arp


def digest(data):
    return hashlib.sha256(data).hexdigest()


def checked(entry):
    raw = (ROOT / entry["path"]).read_bytes()
    assert digest(raw) == entry["sha256"], entry["path"]
    return raw


def apply(src, data, env, *, check=False, reverse=False):
    result = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "apply", "--no-index",
         "--whitespace=nowarn", *(["--check"] if check else []),
         *(["--reverse"] if reverse else []), "-"],
        cwd=src, input=data, env=env, capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError(result.stderr.decode())


def added(raw):
    return Counter(line for line in raw.decode().splitlines()
                   if line.startswith("+") and line != "+" and not line.startswith("+++"))


def snapshot(src, names):
    return {name: digest((src / name).read_bytes()) if (src / name).exists() else None
            for name in sorted(names)}


def linux_recipe(manifest):
    evidence = manifest["linux_downstream"]
    recipe = json.loads(checked(evidence["source_manifest"]))
    fixture = json.loads(gzip.decompress(checked(evidence["platform_fixture"])))
    audit = json.loads(checked(evidence["platform_audit"]))
    assert audit["status"] == "passed" and not audit["build_verified"]
    assert audit["implementation_commit"] == evidence["implementation_commit"]
    assert audit["manifest_sha256"] == evidence["source_manifest"]["sha256"]
    assert audit["fixture_sha256"] == digest(gzip.decompress(checked(evidence["platform_fixture"])))
    assert audit["recipe"]["sha256"] == evidence["recipe_sha256"]
    assert digest(json.dumps(audit["recipe"]["inputs"], sort_keys=True).encode()) == evidence["recipe_sha256"]
    assert recipe["core_commit"] == manifest["core_commit"] == fixture["core_commit"]
    assert recipe["platform_commit"] == manifest["platform_commits"]["linux"]
    identity = manifest["platform_identities"]["linux"]
    assert recipe["platform_core_gitlink"] == identity["core_gitlink"]
    assert identity["core_override"] == manifest["core_commit"]
    assert recipe["official_platform_tag"] is None and not identity["target_version_available"]
    assert digest(fixture["upstream_series"].encode()) == recipe["upstream_series_sha256"]
    names = [line.strip() for line in fixture["upstream_series"].splitlines()
             if line.strip() and not line.startswith("#")]
    assert len(recipe["patches"]) == len(names) == 9
    assert names == [entry["upstream_path"] for entry in recipe["patches"]]
    assert [entry["path"] for entry in recipe["patches"]] == [entry["patch"] for entry in audit["patches"]]
    assert all(entry["returncode"] == 0 for entry in audit["patches"])
    assert recipe["final_outputs"] == audit["final_outputs"]
    for entry in recipe["patches"]:
        checked(entry)
        assert digest(fixture["upstream_patches"][entry["upstream_path"]].encode()) == entry["upstream_sha256"]
    return recipe, fixture


def invariants(src, platform):
    def text(name):
        return (src / name).read_text()
    timezone = text("third_party/blink/renderer/core/timezone/timezone_controller.cc")
    locale_key = text("base/i18n/icubridge/default_icu_locale.h")
    assert "friend class ::blink::TimeZoneController;" in locale_key
    assert "SetICUDefaultLocale" not in timezone
    assert "SetLocaleOverride" not in timezone
    assert "base::i18n::IcuLocaleConverter::GetInstance().ToLanguageTag(locale)" in timezone
    assert timezone.index("base::i18n::SetDefaultIcuLocale(") < timezone.index("blink::switches::kTopChromeWebUI")
    assert "U_SUCCESS(status) && !locale.isBogus() && *locale.getLanguage()" in timezone
    if platform == "windows":
        assert "SetDefaultTimeZone(effective_id);" in timezone
        assert "if (!HasTimeZoneOverride()) {" in timezone
    else:
        assert "if (!HasTimeZoneOverride())\n    SetIcuTimeZoneAndNotifyV8(effective_id);" in timezone
    mailbox = text("third_party/blink/renderer/platform/graphics/mailbox_texture_backing.cc")
    assert mailbox.count("bool MailboxTextureBacking::readPixels(") == 1
    assert "CHECK(thread_checker_.CalledOnValidThread());" in mailbox
    assert "GetMailbox(), shared_image_->size(), clipped.info()," in mailbox
    assert "SkImageInfo::ByteSizeOverflowed(dst_info.computeByteSize(dst_row_bytes))" in mailbox
    canvas = text("third_party/blink/renderer/modules/canvas/canvas2d/base_rendering_context_2d.cc")
    assert "CHECK(validate_and_create_params.zero_initialize);" in canvas
    assert "base::UxrNoiseChannel(" in canvas
    assert "StaticBitmapImage::ShuffleSubchannelColorData(image_data_pixmap.addr(), image_data_pixmap.info());" in canvas
    encoder = text("third_party/blink/renderer/platform/image-encoders/image_encoder.cc")
    assert "ShuffleSubchannelColorData" not in encoder
    assert "SkPngRustEncoder::CompressionLevel::kLow" in encoder
    controller = text("chrome/browser/enterprise/BUILD.gn")
    assert controller.count('"signin/managed_profile_creation_controller.cc"') == 1
    png = text("third_party/blink/renderer/core/html/canvas/canvas_async_blob_creator.cc")
    assert "mime_type_ == kMimeTypePng &&" in png
    ua = text("components/embedder_support/user_agent_utils.cc")
    assert ua.count('#include "base/version.h"') == 1
    assert ua.count("std::string GetUserAgentInternal() {") == 1
    assert ua.count("std::string BuildUserAgentFromProduct(const std::string& product) {") == 1
    storage = text("third_party/blink/public/devtools_protocol/domains/Storage.pdl")
    handler = text("chrome/browser/devtools/protocol/storage_handler.h")
    for command in ("getPrivateVerificationTokens", "getPrivateVerificationTokensIssuerConfigs",
                    "clearPrivateVerificationTokens", "deletePrivateVerificationToken",
                    "setPrivateVerificationTokensTracking"):
        assert ("command " + command in storage) == (platform != "linux")
        assert (command[0].upper() + command[1:] in handler) == (platform != "linux")
    for command in ("runBounceTrackingMitigations", "getTrustTokens", "clearTrustTokens",
                    "setStorageBucketTracking", "deleteStorageBucket", "getCookies", "getUsageAndQuota"):
        assert "command " + command in storage
    if platform == "linux":
        assert "PrivateVerificationToken" not in storage
        assert "privateVerificationTokensUpdated" not in storage
        for name in ("chrome/browser/devtools/inspector_protocol_config.json",
                     "content/browser/devtools/protocol_config.json"):
            config = json.loads(text(name))
            entry = next(entry for entry in config["protocol"]["options"] if entry["domain"] == "Storage")
            assert "PrivateVerification" not in json.dumps(entry)
            assert "privateVerification" not in json.dumps(entry)
        blink = text("third_party/blink/renderer/platform/BUILD.gn")
        web_bundle = text("components/web_package/BUILD.gn")
        for content in (blink, web_bundle):
            assert "assert(!is_linux || enable_cpp_api_from_rust," in content
        assert '"fonts:ift_patcher_bindings"' in blink
        assert "signed_web_bundles_rust_bindings" in web_bundle
        assert "web_bundle_parser_factory.cc" in web_bundle
        assert '"//third_party/node:check_version"' in text("third_party/node/node.gni")
    if platform == "windows":
        timezone = text("third_party/blink/renderer/core/timezone/timezone_controller.cc")
        assert "bool TimeZoneController::SetDefaultTimeZone(" in timezone
        assert "CHECK(timezone);" in timezone
        renderer = text("content/renderer/render_thread_impl.cc")
        assert "CHECK(blink::SetRendererDefaultTimeZone(blink::WebString::FromUtf8(timezone)))" in renderer
    if platform == "macos":
        angle = text("ui/gl/init/gl_display_initializer.cc")
        assert "#if BUILDFLAG(IS_MAC)\n       // A bare --use-angle requests the native default, not an empty backend.\n       requested_renderer.empty() ||\n#endif" in angle
        hevc = text("third_party/blink/renderer/modules/mediarecorder/media_recorder_handler.cc")
        assert hevc.count("std::string HEVCCodecSuffix(") == 1
        assert "#if BUILDFLAG(IS_MAC) && BUILDFLAG(ENABLE_HEVC_PARSER_AND_HW_DECODER)" in hevc
        assert "if (config.Parse(last_seen_codec_description_))" in hevc
        assert "last_seen_codec_description_ = *codec_description;" in hevc


def verify():
    manifest_path = ROOT / "patches/chromium157/manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    fixture = json.loads(gzip.decompress(checked(manifest["fixture"])))
    source = fixture["source"]
    assert source == json.loads((Path(__file__).parent / "source-manifest.json").read_bytes())
    assert source["version"] == manifest["chromium_version"] == "157.0.8103.78"
    for key in ("chromium_commit", "core_commit", "core_tag_object", "platform_commits", "platform_identities"):
        assert source[key] == manifest[key]
    assert source["errors"] == {}
    assert fixture["files"]["chrome/VERSION"] == "MAJOR=157\nMINOR=0\nBUILD=8103\nPATCH=78\n"
    for name, entry in source["files"].items():
        raw = fixture["files"][name].encode()
        assert len(raw) == entry["bytes"] and digest(raw) == entry["sha256"], name
        assert hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest() == entry["git_blob"], name
        assert entry["revision"] in (source["chromium_commit"], *source["revisions"].values())
        assert "/+/" + entry["revision"] + "/" in entry["url"]
    series = (ROOT / "patches/series").read_bytes()
    assert digest(series) == manifest["base_series_sha256"]
    names = [line.strip() for line in series.decode().splitlines()
             if line.strip() and not line.startswith("#")]
    assert len(names) == len(set(names)) == manifest["base_patch_count"] == 224
    assert names == [entry["path"] for entry in manifest["base_patches"]]
    for entry in manifest["base_patches"] + manifest["historical_files"]:
        checked(entry)
    overrides = {entry["original"]: entry for entry in manifest["overrides"]}
    assert len(overrides) == len(manifest["overrides"])
    assert {p.name for p in (ROOT / "patches/chromium157").glob("*.patch")} == {
        Path(entry["path"]).name for entry in overrides.values()}
    additions = {entry["path"]: entry for entries in manifest["additions"].values() for entry in entries}
    assert {p.name for p in (ROOT / "patches/chromium157-additions").glob("*.patch")} == {
        Path(name).name for name in additions}
    preservation = []
    for entry in list(overrides.values()) + list(additions.values()):
        raw = checked(entry)
        targets = [target for target, _, _ in arp.transform_patch(raw, set(), [])[1]]
        if entry.get("kind") == "linux-disabled-feature-protocol":
            assert entry["path"] == "patches/chromium157-additions/0239-linux-disabled-private-verification-token-protocol.patch"
            assert targets == [
                "third_party/blink/public/devtools_protocol/domains/Storage.pdl",
                "chrome/browser/devtools/inspector_protocol_config.json",
                "content/browser/devtools/protocol_config.json",
            ]
            assert entry in manifest["additions"]["linux"]
            assert all(entry not in manifest["additions"][platform] for platform in ("macos", "windows"))
            continue
        assert len(targets) == 1, entry["path"]
        if entry.get("kind") == "locale-api-compatibility":
            assert entry["path"] in {
                "patches/chromium157-additions/0236-timezone-default-locale-setter-key.patch",
                "patches/chromium157-additions/0237-timezone-default-locale-api.patch",
            }
            continue
        if entry.get("kind") == "direct-header-compatibility":
            assert entry["path"] == "patches/chromium157-additions/0238-user-agent-version-header.patch"
            assert added(raw) == Counter({'+#include "base/version.h"': 1})
            assert not any(line.startswith("-") and not line.startswith("---")
                           for line in raw.decode().splitlines())
            continue
        predecessor = (ROOT / entry["predecessor"]).read_bytes()
        assert digest(predecessor) == entry["predecessor_sha256"]
        assert added(predecessor) == added(raw), entry["path"]
        if "original" in entry:
            assert digest((ROOT / entry["original"]).read_bytes()) == entry["original_sha256"]
        preservation.append(entry["path"])
    recipe, platform_fixture = linux_recipe(manifest)
    assert source["linux_downstream"] == manifest["linux_downstream"]
    assert set(platform_fixture["sources"]) <= set(fixture["files"])
    assert source["platform_source_files"] == fixture["platform_source_files"]
    expected_sections = [(entry["path"], name) for entry in recipe["patches"]
                         for name, _, _ in arp.transform_patch(checked(entry), set(), [])[1]]
    assert [(entry["path"], entry["target"]) for entry in fixture["predecessors"]["linux"]] == expected_sections
    for entry in fixture["predecessors"]["linux"]:
        raw = (ROOT / entry["path"]).read_bytes()
        assert digest(raw) == entry["patch_sha256"] and entry["text"].encode() in raw
    results = []
    with tempfile.TemporaryDirectory(prefix="chromix157-verify-") as temporary:
        work = Path(temporary)
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_CEILING_DIRECTORIES=str(work))
        for platform in ("linux", "windows", "macos"):
            for substituted in (False, True):
                src = work / (platform + ("-substituted" if substituted else "-raw"))
                src.mkdir()
                for name in fixture["platform_source_files"][platform]:
                    path = src / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(fixture["files"][name])
                result = {"platform": platform, "architectures": ["x64", "arm64"],
                          "domain_substituted": substituted, "status": "failed",
                          "overlay_mode": "downstream-nine-patch-recipe" if platform == "linux" else "actual-platform",
                          "build_verified": False, "runtime_verified": False,
                          "expected_patch_count": {"linux": 230, "windows": 235, "macos": 231}[platform],
                          "patches": [], "predecessors": []}
                results.append(result)
                try:
                    for layer in (("linux_core",) if platform == "linux" else ("core", platform)):
                        for entry in fixture["predecessors"][layer]:
                            result["stage"] = "overlay"
                            result["current_file"] = entry["target"]
                            result["current_patch"] = entry["path"]
                            raw = entry["text"].encode()
                            assert digest(raw) == entry["sha256"]
                            apply(src, raw, env, check=True)
                            apply(src, raw, env)
                            result["predecessors"].append({k: v for k, v in entry.items() if k != "text"})
                    if platform == "linux":
                        for name, text in platform_fixture["sources"].items():
                            assert (src / name).read_bytes() == text.encode(), name
                        platform_initial = snapshot(src, recipe["final_outputs"])
                        platform_patches = []
                        for entry in recipe["patches"]:
                            result["stage"] = "downstream-platform-apply"
                            result["current_patch"] = entry["path"]
                            result["current_file"] = list(entry["inputs"])
                            assert snapshot(src, entry["inputs"]) == entry["inputs"], entry["path"]
                            raw = checked(entry)
                            apply(src, raw, env, check=True)
                            apply(src, raw, env)
                            assert snapshot(src, entry["outputs"]) == entry["outputs"], entry["path"]
                            platform_patches.append(raw)
                            result["predecessors"].append({"path": entry["path"], "sha256": entry["sha256"],
                                                           "inputs": entry["inputs"], "outputs": entry["outputs"],
                                                           "status": "applied", "layer": "linux-downstream"})
                        assert snapshot(src, recipe["final_outputs"]) == recipe["final_outputs"]
                        for reverse in (True, False):
                            sequence = reversed(platform_patches) if reverse else platform_patches
                            for raw in sequence:
                                apply(src, raw, env, check=True, reverse=reverse)
                                apply(src, raw, env, reverse=reverse)
                            assert snapshot(src, recipe["final_outputs"]) == (
                                platform_initial if reverse else recipe["final_outputs"])
                        result["full_platform_replay"] = {
                            "status": "verified", "patch_count": len(platform_patches),
                            "source_file_count": len(platform_initial), "roundtrip": "reverse-forward-exact-bytes",
                            "initial": platform_initial, "final": recipe["final_outputs"],
                            "source_manifest": manifest["linux_downstream"]["source_manifest"],
                            "previous_full_platform_audit": manifest["linux_downstream"]["platform_audit"]}
                    listed = set(fixture["domain_lists"][platform].splitlines()) if substituted else set()
                    rules = arp._rules(fixture["domain_regex"].encode()) if substituted else []
                    for name in listed:
                        path = src / name
                        if path.is_file():
                            path.write_text(arp._substitute(path.read_text(), rules))
                    selected = [overrides.get(entry["path"], entry) for entry in manifest["base_patches"]]
                    selected += manifest["additions"][platform]
                    assert len(selected) == result["expected_patch_count"]
                    prepared = []
                    targets = set()
                    for entry in selected:
                        raw = checked(entry)
                        data, entries = arp.transform_patch(raw, listed, rules)
                        prepared.append((entry, data, entries))
                        targets.update(target for target, _, _ in entries)
                    initial = snapshot(src, targets)
                    for entry, data, entries in prepared:
                        result["stage"] = "sequential-apply"
                        result["current_patch"] = entry["path"]
                        result["current_file"] = entries[0][0]
                        before = snapshot(src, [target for target, _, _ in entries])
                        apply(src, data, env, check=True)
                        apply(src, data, env)
                        result["patches"].append({"path": entry["path"], "sha256": entry["sha256"],
                                                  "status": "applied", "preimages": before,
                                                  "postimages": snapshot(src, before)})
                    invariants(src, platform)
                    roundtrip_patches = prepared
                    roundtrip_initial = initial.copy()
                    if platform == "linux":
                        prefix = []
                        for entry in recipe["patches"]:
                            data, entries = arp.transform_patch(checked(entry), listed, rules)
                            prefix.append((entry, data, entries))
                        roundtrip_patches = prefix + prepared
                        for name, text in platform_fixture["sources"].items():
                            targets.add(name)
                            if name in listed:
                                text = arp._substitute(text, rules)
                            roundtrip_initial[name] = digest(text.encode())
                    final = snapshot(src, targets)
                    for reverse in (True, False):
                        sequence = reversed(roundtrip_patches) if reverse else roundtrip_patches
                        result["stage"] = "reverse" if reverse else "forward"
                        for entry, data, entries in sequence:
                            result["current_patch"] = entry["path"]
                            result["current_file"] = entries[0][0]
                            apply(src, data, env, check=True, reverse=reverse)
                            apply(src, data, env, reverse=reverse)
                        assert snapshot(src, targets) == (roundtrip_initial if reverse else final)
                    invariants(src, platform)
                    result.update(status="verified", patch_count=len(prepared),
                                  roundtrip={"status": "verified", "method": "reverse-forward-exact-bytes",
                                             "patch_count": len(roundtrip_patches),
                                             "scope": "platform9-plus-chromix230" if platform == "linux" else "chromix-stack",
                                             "initial": roundtrip_initial, "final": final})
                    for key in ("stage", "current_patch", "current_file"):
                        del result[key]
                except (ValueError, AssertionError) as error:
                    result["error"] = str(error)
    return {"schema_version": 1, "status": "verified" if all(r["status"] == "verified" for r in results) else "blocked",
            "manifest_sha256": digest(manifest_path.read_bytes()),
            "qualification": manifest["qualification"], "historical_root224_and156_bytes_unchanged": True,
            "added_lines_preserved": preservation, "source_file_count": len(source["files"]),
            "overlay_coverage": source["overlay_coverage"], "cases": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output must not already exist")
    report = verify()
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "cases": [
        {k: r[k] for k in ("platform", "overlay_mode", "domain_substituted", "status")} for r in report["cases"]],
        "output": str(args.output)}))
    return int(report["status"] != "verified")


if __name__ == "__main__":
    raise SystemExit(main())
