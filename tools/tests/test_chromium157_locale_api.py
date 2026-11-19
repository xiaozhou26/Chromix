"""Exact157 locale API provenance, replay, and optional native object/link checks."""
from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess

import pytest

from tools import patch_selection as selection

ROOT = Path(__file__).resolve().parents[2]
HEADER = "base/i18n/icubridge/default_icu_locale.h"
SOURCE = "third_party/blink/renderer/core/timezone/timezone_controller.cc"
SETTER = "base/i18n/icubridge/default_icu_locale.cc"
CONVERTER = "base/i18n/icu4c_tag_converter.cc"
PATCHES = (
    "patches/chromium157-additions/0236-timezone-default-locale-setter-key.patch",
    "patches/chromium157-additions/0237-timezone-default-locale-api.patch",
)


def verifier():
    spec = importlib.util.spec_from_file_location(
        "locale157_verify", ROOT / "patches/chromium157-verification/verify.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture():
    review = json.loads((ROOT / selection.REVIEW157).read_bytes())
    return json.loads(gzip.decompress(verifier().checked(review["fixture"])))


def test_exact_api_declaration_implementation_and_override_contract():
    data = fixture()
    for name in (HEADER, SETTER, CONVERTER, "base/i18n/rtl.h",
                 "base/i18n/tag_converters.h",
                 "third_party/blink/renderer/core/inspector/locale_controller.cc"):
        raw = data["files"][name].encode()
        origin = data["source"]["files"][name]
        assert origin["revision"] == "4b3d0dabb8250b6f152d2d85d79e7706e309d087"
        assert selection.digest(raw) == origin["sha256"]
        assert hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest() == origin["git_blob"]
    header = data["files"][HEADER]
    assert "SetICUDefaultLocale" not in data["files"]["base/i18n/rtl.h"]
    assert "BASE_I18N_EXPORT void SetDefaultIcuLocale(DefaultIcuLocaleSetterKey key," in header
    assert "friend class ::blink::TimeZoneController;" not in header
    assert header.index(" private:") < header.index("\n  DefaultIcuLocaleSetterKey() = default;")
    setter = data["files"][SETTER]
    assert "GetLocaleHolder().SetLocale(language_tag);" in setter
    assert "icu::Locale::setDefault(locale, error_code);" in setter
    converter = data["files"][CONVERTER]
    assert "icu_locale.toLanguageTag<std::string>(status)" in converter
    assert "icu::Locale::forLanguageTag(language_tag.tag_string(), status)" in converter
    override = data["files"]["third_party/blink/renderer/core/inspector/locale_controller.cc"]
    assert "embedder_locale_(base::i18n::GetDefaultIcuLocale())" in override
    assert "if (is_claiming_override && !locale_override_.empty())" in override
    assert "UpdateLocale(embedder_locale_);" in override


@pytest.mark.parametrize("platform", ["linux", "macos", "windows"])
@pytest.mark.parametrize("substituted", [False, True])
def test_locale_patch_chain_preserves_source_behavior(tmp_path, platform, substituted):
    data = fixture()
    verify = verifier()
    review = json.loads((ROOT / selection.REVIEW157).read_bytes())
    replacements = {entry["original"]: entry for entry in review["overrides"]}
    selected = [replacements.get(entry["path"], entry) for entry in review["base_patches"]]
    selected += review["additions"][platform]
    assert [entry["path"] for entry in selected[-3:-1]] == list(PATCHES)
    assert len(selected) == {"linux": 229, "macos": 231, "windows": 235}[platform]
    listed = set(data["domain_lists"][platform].splitlines()) if substituted else set()
    rules = verify.arp._rules(data["domain_regex"].encode()) if substituted else []
    for name in (HEADER, SOURCE):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        text = data["files"][name]
        path.write_text(verify.arp._substitute(text, rules) if name in listed else text)
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CEILING_DIRECTORIES=str(tmp_path.parent))
    prepared = []
    for entry in selected:
        raw, targets = verify.arp.transform_patch(verify.checked(entry), listed, rules)
        if not any(target in (HEADER, SOURCE) for target, _, _ in targets):
            continue
        assert len(targets) == 1
        prepared.append(raw)
        if entry["path"] == PATCHES[0]:
            before = {name: (tmp_path / name).read_text() for name in (HEADER, SOURCE)}
        verify.apply(tmp_path, raw, env, check=True)
        verify.apply(tmp_path, raw, env)
    after = {name: (tmp_path / name).read_text() for name in (HEADER, SOURCE)}
    assert after[HEADER].replace("class TimeZoneController;\n", "").replace(
        "  friend class ::blink::TimeZoneController;\n", "") == before[HEADER]
    restored = after[SOURCE].replace(
        '#include "base/i18n/icu4c_tag_converter.h"\n#include "base/i18n/icubridge/default_icu_locale.h"',
        '#include "base/i18n/rtl.h"').replace(
        "      // Keep the Chromium locale holder and ICU default synchronized.\n"
        "      base::i18n::SetDefaultIcuLocale(\n"
        "          base::i18n::DefaultIcuLocaleSetterKey(),\n"
        "          base::i18n::IcuLocaleConverter::GetInstance().ToLanguageTag(locale));",
        "      // Legacy ICU names preserve extensions and avoid es-419 host remapping.\n"
        "      base::i18n::SetICUDefaultLocale(locale.getName());")
    assert restored == before[SOURCE]
    assert "SetLocaleOverride" not in after[SOURCE]
    assert "icu::Locale::setDefault" not in after[SOURCE]
    assert after[SOURCE].index("SetDefaultIcuLocale(") < after[SOURCE].index("kTopChromeWebUI")
    if platform == "windows":
        assert "SetDefaultTimeZone(effective_id);" in after[SOURCE]
        assert "if (!HasTimeZoneOverride()) {" in after[SOURCE]
    else:
        assert "if (!HasTimeZoneOverride())\n    SetIcuTimeZoneAndNotifyV8(effective_id);" in after[SOURCE]
    for raw in reversed(prepared):
        verify.apply(tmp_path, raw, env, reverse=True)
    for name in (HEADER, SOURCE):
        text = data["files"][name]
        assert (tmp_path / name).read_text() == (verify.arp._substitute(text, rules) if name in listed else text)


def test_historical_patch_bytes_and_157_only_selection():
    old = json.loads(subprocess.check_output([
        "git", "-C", str(ROOT), "show", "38f35ce29d6e1d02dde1d041f5a745d84cfbba9b:" + selection.MANIFEST157]))
    for entry in old["base_patches"] + old["overrides"]:
        assert selection.digest((ROOT / entry["path"]).read_bytes()) == entry["sha256"]
    for platform, entry in old["platforms"].items():
        for patch in entry["additions"]:
            assert selection.digest((ROOT / patch["path"]).read_bytes()) == patch["sha256"]
        identity, patches = selection.select(ROOT, platform)
        assert [name for name, _ in patches][-3:-1] == list(PATCHES)
        assert len(patches) == selection.PATCH_COUNTS157[platform]
        assert identity["selection"]["version"] == selection.VERSION157
    assert "0236-timezone" not in (ROOT / selection.MANIFEST156).read_text()
    assert "0237-timezone" not in (ROOT / "patches/series").read_text()


def test_native_timezone_object_and_real_setter_link(tmp_path):
    supplied = os.environ.get("CHROMIX_LOCALE157_SOURCE")
    if not supplied:
        pytest.skip("set CHROMIX_LOCALE157_SOURCE to the patched exact157 tree with generated build outputs")
    src = Path(supplied).resolve()
    out = src / os.environ.get("CHROMIX_LOCALE157_OUT", "out/Chromix")
    assert (src / "chrome/VERSION").read_text() == "MAJOR=157\nMINOR=0\nBUILD=8103\nPATCH=78\n"
    report = json.loads((ROOT / "patches/chromium157-verification/verified.json").read_bytes())
    case = next(case for case in report["cases"] if case["platform"] == "linux" and case["domain_substituted"])
    for name in (HEADER, SOURCE):
        assert selection.digest((src / name).read_bytes()) == case["roundtrip"]["final"][name]
    data = fixture()
    for name in (SETTER, CONVERTER):
        assert (src / name).read_text() == data["files"][name]
    targets = subprocess.check_output(["ninja", "-t", "targets", "all"], cwd=out, text=True).splitlines()
    objects = []
    for name in (SOURCE, SETTER, CONVERTER):
        suffix = "/" + Path(name).stem + ".o"
        matches = [line.split(": ")[0] for line in targets if line.split(": ")[0].endswith(suffix)]
        assert len(matches) == 1, matches
        command = subprocess.check_output(["ninja", "-t", "commands", matches[0]], cwd=out, text=True, errors="replace").splitlines()[-1]
        args = shlex.split(command)
        obj = tmp_path / (Path(name).stem + ".o")
        args[args.index("-o") + 1] = str(obj)
        if "-MF" in args:
            args[args.index("-MF") + 1] = str(obj) + ".d"
        result = subprocess.run(args, cwd=out, capture_output=True, text=True, timeout=600)
        assert result.returncode == 0, result.stdout + result.stderr
        assert obj.stat().st_size > 0
        objects.append(obj)
    tools = src / "third_party/llvm-build/Release+Asserts/bin"
    linked = tmp_path / "timezone-locale-linked.o"
    subprocess.run([str(tools / "ld.lld"), "-r", *map(str, objects), "-o", str(linked)], check=True)
    undefined = subprocess.check_output([str(tools / "llvm-nm"), "-C", "--undefined-only", str(linked)], text=True)
    symbols = subprocess.check_output([str(tools / "llvm-nm"), "-C", str(linked)], text=True)
    for symbol in ("base::i18n::SetDefaultIcuLocale(", "base::i18n::IcuLocaleConverter::GetInstance()",
                   "base::i18n::IcuLocaleConverter::ToLanguageTag("):
        assert symbol in symbols
        assert symbol not in undefined
    assert "SetICUDefaultLocale" not in symbols
