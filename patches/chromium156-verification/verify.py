#!/usr/bin/env python3
"""Replay authenticated Chromium156 source subsets without production pin changes."""
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
import verify_patch_stack


def digest(data):
    return hashlib.sha256(data).hexdigest()


def checked(entry):
    data = (ROOT / entry["path"]).read_bytes()
    assert digest(data) == entry["sha256"], entry["path"]
    return data


def apply(src, data, env, check=False):
    result = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "apply", "--no-index",
         "--whitespace=nowarn", *(["--check"] if check else []), "-"],
        input=data, cwd=src, env=env, capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError(result.stderr.decode())


def verify():
    manifest_path = ROOT / "patches/chromium156/manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    fixture = json.loads(gzip.decompress(checked(manifest["fixture"])))
    series = (ROOT / "patches/series").read_bytes()
    assert digest(series) == manifest["base_series_sha256"]
    names = [line.strip() for line in series.decode().splitlines()
             if line.strip() and not line.startswith("#")]
    assert len(names) == len(set(names)) == manifest["base_patch_count"] == 224
    overrides = {entry["original"]: entry for entry in manifest["overrides"]}
    assert {p.name for p in (ROOT / "patches/chromium156").glob("*.patch")} == {
        Path(entry["path"]).name for entry in overrides.values()}
    for name, entry in overrides.items():
        assert digest((ROOT / name).read_bytes()) == entry["original_sha256"]
        checked(entry)
    initial = json.loads((Path(__file__).parent / "root224-report.json").read_bytes())
    assert len(initial["patches"]) == 224
    for entry in initial["patches"]:
        assert digest((ROOT / entry["base_patch"]).read_bytes()) == entry["sha256"]
    for name, entry in fixture["source"]["files"].items():
        raw = fixture["files"][name].encode()
        assert digest(raw) == entry["sha256"], name
        assert hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest() == entry["git_blob"]
    assert fixture["source"]["errors"] == {}
    preservation = []
    contextual = {"0020", "0024", "0044", "0074", "0077", "0140", "0141",
                  "0142", "0152", "0160", "0184", "0209"}
    for entry in overrides.values():
        if Path(entry["path"]).name[:4] not in contextual:
            continue
        predecessor = ROOT / "patches/chromium155" / Path(entry["path"]).name
        if not predecessor.exists():
            predecessor = ROOT / entry["original"]
        def added(raw):
            return Counter(line for line in raw.decode().splitlines()
                           if line.startswith("+") and line != "+" and not line.startswith("+++"))
        assert added(predecessor.read_bytes()) == added(checked(entry)), entry["path"]
        preservation.append(entry["path"])
    gpu_name = "0150-canvas-mailbox-readback-geometry.patch"
    gpu_before = (ROOT / "patches" / gpu_name).read_text()
    gpu_after = (ROOT / "patches/chromium156" / gpu_name).read_text()
    gpu_after = gpu_after.replace(
        "+      GetMailbox(), shared_image_->size(), clipped.info(),\n"
        "+      static_cast<GLuint>(dst_row_bytes),",
        "+      GetMailbox(), clipped.info(), static_cast<GLuint>(dst_row_bytes),")
    assert added(gpu_before.encode()) == added(gpu_after.encode())
    results = []
    with tempfile.TemporaryDirectory(prefix="chromix156-verify-") as temporary:
        work = Path(temporary)
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_CEILING_DIRECTORIES=str(work))
        core = work / "core"
        core.mkdir()
        (core / "domain_regex.list").write_text(fixture["domain_regex"])
        for platform in ("linux", "windows", "macos"):
            for substituted in (False, True):
                case = work / (platform + ("-substituted" if substituted else "-raw"))
                src, repo, tooling = case / "src", case / "repo", case / "tooling"
                src.mkdir(parents=True)
                (repo / "patches").mkdir(parents=True)
                tooling.mkdir()
                listed_text = fixture["domain_lists"][platform]
                (tooling / "domain_substitution.list").write_text(listed_text)
                (core / "domain_substitution.list").write_text(listed_text)
                listed = set(listed_text.splitlines()) if substituted else set()
                rules = arp._rules(fixture["domain_regex"].encode()) if substituted else []
                for name, text in fixture["files"].items():
                    path = src / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text)
                predecessors = fixture["predecessors"]["core"] + fixture["predecessors"][platform]
                for entry in predecessors:
                    data = entry["text"].encode()
                    assert digest(data) == entry["sha256"]
                    apply(src, data, env, check=True)
                    apply(src, data, env)
                for name in listed:
                    path = src / name
                    if path.is_file():
                        path.write_text(arp._substitute(path.read_text(), rules))
                if substituted:
                    (src / ".chromix-domain-substituted").write_text("fixture\n")
                selected = [(overrides[name]["path"], checked(overrides[name]))
                            if name in overrides else (name, (ROOT / name).read_bytes())
                            for name in names]
                selected += [(entry["path"], checked(entry))
                             for entry in manifest["additions"][platform]]
                effective_series, records = [], []
                for name, raw in selected:
                    physical = "patches/" + Path(name).name
                    (repo / physical).write_bytes(raw)
                    effective_series.append(physical)
                    data, entries = arp.transform_patch(raw, listed, rules)
                    before = {target: digest((src / target).read_bytes())
                              if (src / target).exists() else None
                              for target, _, _ in entries}
                    apply(src, data, env, check=True)
                    apply(src, data, env)
                    records.append({"path": name, "sha256": digest(raw),
                                    "status": "applied", "preimages": before,
                                    "postimages": {target: digest((src / target).read_bytes())
                                                   if (src / target).exists() else None
                                                   for target, _, _ in entries}})
                (repo / "patches/series").write_text("\n".join(effective_series) + "\n")
                roundtrip = verify_patch_stack.verify(
                    src, repo, core=core, tooling=tooling, platform=platform)
                assert roundtrip["status"] == "verified"
                mailbox = (src / "third_party/blink/renderer/platform/graphics/mailbox_texture_backing.cc").read_text()
                assert mailbox.count("bool MailboxTextureBacking::readPixels(") == 1
                assert mailbox.count("int src_x,") == 1
                assert "GetMailbox(), shared_image_->size(), clipped.info()," in mailbox
                assert "DCHECK_CALLED_ON_VALID_THREAD(thread_checker_);" in mailbox
                canvas = (src / "third_party/blink/renderer/modules/canvas/canvas2d/base_rendering_context_2d.cc").read_text()
                assert "base::UxrNoiseChannel(" in canvas
                assert "StaticBitmapImage::ShuffleSubchannelColorData(image_data_pixmap.addr(), image_data_pixmap.info());" in canvas
                assert "CHECK(validate_and_create_params.zero_initialize);" in canvas
                encoder = (src / "third_party/blink/renderer/platform/image-encoders/image_encoder.cc").read_text()
                assert "ShuffleSubchannelColorData" not in encoder
                assert "SkPngRustEncoder::CompressionLevel::kLow" in encoder
                controller = (src / "chrome/browser/enterprise/BUILD.gn").read_text()
                assert controller.count('"signin/managed_profile_creation_controller.cc"') == 1
                png = (src / "third_party/blink/renderer/core/html/canvas/canvas_async_blob_creator.cc").read_text()
                assert "mime_type_ == kMimeTypePng &&" in png
                if platform == "windows":
                    timezone = (src / "third_party/blink/renderer/core/timezone/timezone_controller.cc").read_text()
                    assert "bool TimeZoneController::SetDefaultTimeZone(" in timezone
                    assert "CHECK(timezone);" in timezone
                    renderer = (src / "content/renderer/render_thread_impl.cc").read_text()
                    assert "CHECK(blink::SetRendererDefaultTimeZone(blink::WebString::FromUtf8(timezone)))" in renderer
                results.append({"platform": platform, "architectures": ["x64", "arm64"],
                                "domain_substituted": substituted, "status": "verified",
                                "predecessor_section_count": len(predecessors),
                                "patch_count": len(records), "patches": records,
                                "roundtrip": roundtrip})
    return {"schema_version": 1, "status": "verified", "manifest_sha256": digest(manifest_path.read_bytes()),
            "qualification": "Exact-source subset structural verification; x64/arm64 use identical source patches. No architecture compilation, browser runtime, full upstream tree or production selector integration is claimed.",
            "historical_root224_bytes_unchanged": True,
            "context_rebases_preserve_added_lines": preservation, "cases": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output must not already exist")
    report = verify()
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "cases": len(report["cases"]),
                      "output": str(args.output)}))


if __name__ == "__main__":
    main()
