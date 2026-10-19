"""Platform pins fail closed while preserving shared and per-target identities."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from tools import platform_pins as pins

GLOBAL_VERSION = "152.0.7977.82"
LINUX_VERSION = "153.0.8010.36"


def repository(root, *, linux=False, windows=False, macos=False):
    values = {
        "ChromiumVersion": GLOBAL_VERSION,
        "UngoogledVersion": GLOBAL_VERSION + "-1",
        "UngoogledCommit": "a" * 40,
        "UngoogledWindowsVersion": GLOBAL_VERSION + "-1.1",
        "UngoogledWindowsCommit": "b" * 40,
        "UngoogledMacOSVersion": GLOBAL_VERSION + "-1.1",
        "UngoogledMacOSCommit": "c" * 40,
        "UngoogledLinuxVersion": GLOBAL_VERSION + "-1",
        "UngoogledLinuxCommit": "d" * 40,
    }
    (root / "build").mkdir()
    (root / "CHROMIUM_VERSION").write_text(GLOBAL_VERSION + "\n")
    if linux:
        values.update(LinuxChromiumVersion=LINUX_VERSION,
                      LinuxUngoogledVersion=LINUX_VERSION + "-1",
                      LinuxUngoogledCommit="e" * 40,
                      UngoogledLinuxVersion=LINUX_VERSION + "-1",
                      UngoogledLinuxCommit="f" * 40)
        (root / pins.LINUX_VERSION_FILE).write_text(LINUX_VERSION + "\n")
    if windows:
        values.update(WindowsChromiumVersion=LINUX_VERSION,
                      WindowsUngoogledVersion=LINUX_VERSION + "-1",
                      WindowsUngoogledCommit="e" * 40,
                      UngoogledWindowsVersion=LINUX_VERSION + "-1.1",
                      UngoogledWindowsCommit="9" * 40)
        (root / pins.WINDOWS_VERSION_FILE).write_text(LINUX_VERSION + "\n")
    if macos:
        values.update(MacOSChromiumVersion=GLOBAL_VERSION,
                      MacOSUngoogledVersion=GLOBAL_VERSION + "-1",
                      MacOSUngoogledCommit="a" * 40,
                      ChromiumVersion=LINUX_VERSION, UngoogledVersion=LINUX_VERSION + "-1",
                      UngoogledCommit="e" * 40, UngoogledWindowsVersion=LINUX_VERSION + "-1.1",
                      UngoogledLinuxVersion=LINUX_VERSION + "-1")
        (root / "CHROMIUM_VERSION").write_text(LINUX_VERSION + "\n")
        (root / pins.MACOS_VERSION_FILE).write_text(GLOBAL_VERSION + "\n")
    save(root, values)
    return values


def save(root, values):
    (root / "build/ungoogled-revisions.psd1").write_text(
        "@{\n" + "".join(f'  {key} = "{value}"\n' for key, value in values.items()) + "}\n")


@pytest.mark.parametrize("platform,version,core,overlay", [
    ("linux", "155.0.8059.39", "ffeb45a2bb88bc56a1dfed31410aeb10e9efcd5a",
     "025d99db1673a21992534551598b73ca79360762"),
    ("macos", "155.0.8059.39", "ffeb45a2bb88bc56a1dfed31410aeb10e9efcd5a",
     "b1238bb72b02cd36219991d9029c1c65183ea295"),
    ("windows", "155.0.8059.39", "ffeb45a2bb88bc56a1dfed31410aeb10e9efcd5a",
     "989b4382364baaddb8b3180e1eb1030ca96a7273"),
])
def test_checked_in_target_pins(platform, version, core, overlay):
    result = pins.load_pins(pins.ROOT, platform)
    assert result["ChromiumVersion"] == version
    assert result["UngoogledVersion"] == version + "-1"
    assert result["UngoogledCommit"] == core
    assert result["Ungoogled" + pins.PLATFORMS[platform] + "Commit"] == overlay
    suffix = "-1" if platform == "linux" else "-1.1"
    assert result["Ungoogled" + pins.PLATFORMS[platform] + "Version"] == version + suffix
    assert (pins.ROOT / "CHROMIUM_VERSION").read_text().strip() == LINUX_VERSION
    assert (pins.ROOT / "UNGOOGLED_VERSION").read_text().strip() == LINUX_VERSION + "-1"
    assert (pins.ROOT / "UNGOOGLED_WINDOWS_VERSION").read_text().strip() == "155.0.8059.39-1.1"


@pytest.mark.parametrize("platform", pins.PLATFORMS)
def test_legacy_repositories_keep_global_pins(tmp_path, platform):
    values = repository(tmp_path)
    assert pins.load_pins(tmp_path, platform) == values


@pytest.mark.parametrize("platform", pins.PLATFORMS)
def test_linux_overrides_do_not_change_other_platforms_or_files(tmp_path, platform):
    values = repository(tmp_path, linux=True)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    result = pins.load_pins(tmp_path, platform)
    for field in pins.COMMON_FIELDS:
        assert result[field] == values[("Linux" if platform == "linux" else "") + field]
    assert result["UngoogledLinuxCommit"] == values["UngoogledLinuxCommit"]
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize("missing", [*pins.LINUX_OVERRIDES, pins.LINUX_VERSION_FILE])
def test_linux_partial_overrides_fail_closed(tmp_path, missing):
    values = repository(tmp_path, linux=True)
    if missing == pins.LINUX_VERSION_FILE:
        (tmp_path / missing).unlink()
    else:
        values.pop(missing)
        save(tmp_path, values)
    with pytest.raises(pins.PinError, match="must all be present"):
        pins.load_pins(tmp_path, "linux")
    for platform in ("windows", "macos"):
        assert pins.load_pins(tmp_path, platform)["ChromiumVersion"] == GLOBAL_VERSION


def test_linux_version_file_without_overrides_is_not_legacy(tmp_path):
    repository(tmp_path)
    (tmp_path / pins.LINUX_VERSION_FILE).write_text(LINUX_VERSION)
    with pytest.raises(pins.PinError, match="must all be present"):
        pins.load_pins(tmp_path, "linux")


@pytest.mark.parametrize("field,value", [
    ("LinuxChromiumVersion", "153"),
    ("LinuxUngoogledVersion", GLOBAL_VERSION + "-1"),
    ("LinuxUngoogledCommit", "not-a-commit"),
    ("UngoogledLinuxVersion", GLOBAL_VERSION + "-1"),
    ("UngoogledLinuxVersion", LINUX_VERSION + "-2"),
    ("UngoogledLinuxCommit", ""),
])
def test_linux_core_and_platform_pin_consistency(tmp_path, field, value):
    values = repository(tmp_path, linux=True)
    values[field] = value
    save(tmp_path, values)
    with pytest.raises(pins.PinError):
        pins.load_pins(tmp_path, "linux")


@pytest.mark.parametrize("platform", pins.PLATFORMS)
def test_matching_root_version_file_is_required(tmp_path, platform):
    repository(tmp_path, linux=True)
    name = pins.LINUX_VERSION_FILE if platform == "linux" else "CHROMIUM_VERSION"
    (tmp_path / name).write_text("1.2.3.4\n")
    with pytest.raises(pins.PinError, match=name):
        pins.load_pins(tmp_path, platform)


def test_linux_does_not_use_global_root_as_effective_version(tmp_path):
    repository(tmp_path, linux=True)
    (tmp_path / "CHROMIUM_VERSION").unlink()
    assert pins.load_pins(tmp_path, "linux")["ChromiumVersion"] == LINUX_VERSION


@pytest.mark.parametrize("platform", pins.PLATFORMS)
@pytest.mark.parametrize("value", ['""', "$null", '"153.0.8010.36" + "-1"'])
def test_nonliteral_override_cannot_be_treated_as_absent(tmp_path, platform, value):
    repository(tmp_path)
    field = pins.PLATFORMS[platform] + "ChromiumVersion"
    path = tmp_path / "build/ungoogled-revisions.psd1"
    path.write_text(path.read_text().replace("}\n", f"  {field} = {value}\n}}\n"))
    with pytest.raises(pins.PinError):
        pins.load_pins(tmp_path, platform)


@pytest.mark.parametrize("platform", pins.PLATFORMS)
def test_duplicate_required_pin_is_rejected(tmp_path, platform):
    repository(tmp_path, **{platform: True})
    field = pins.PLATFORMS[platform] + "UngoogledCommit"
    path = tmp_path / "build/ungoogled-revisions.psd1"
    path.write_text(path.read_text().replace("}\n", f'  {field} = "{"e" * 40}"\n}}\n'))
    with pytest.raises(pins.PinError, match="duplicate"):
        pins.load_pins(tmp_path, platform)


@pytest.mark.parametrize("platform", pins.PLATFORMS)
def test_macos_override_preserves_shared_baseline(tmp_path, platform):
    values = repository(tmp_path, macos=True)
    shared = pins.load_shared_pins(tmp_path)
    assert shared == values
    result = pins.load_pins(tmp_path, platform)
    for field in pins.COMMON_FIELDS:
        assert result[field] == values[("MacOS" if platform == "macos" else "") + field]
    assert shared["ChromiumVersion"] == LINUX_VERSION


@pytest.mark.parametrize("missing", [*pins.MACOS_OVERRIDES, pins.MACOS_VERSION_FILE])
def test_macos_partial_overrides_fail_closed(tmp_path, missing):
    values = repository(tmp_path, macos=True)
    if missing == pins.MACOS_VERSION_FILE:
        (tmp_path / missing).unlink()
    else:
        values.pop(missing)
        save(tmp_path, values)
    with pytest.raises(pins.PinError, match="must all be present"):
        pins.load_pins(tmp_path, "macos")
    assert pins.load_shared_pins(tmp_path)["ChromiumVersion"] == LINUX_VERSION
    assert pins.load_pins(tmp_path, "windows")["ChromiumVersion"] == LINUX_VERSION


@pytest.mark.parametrize("field,value", [
    ("MacOSChromiumVersion", "152"), ("MacOSUngoogledVersion", LINUX_VERSION + "-1"),
    ("MacOSUngoogledCommit", "not-a-commit"), ("UngoogledMacOSVersion", LINUX_VERSION + "-1.1"),
    ("UngoogledMacOSVersion", GLOBAL_VERSION + "-2.1"), ("UngoogledMacOSCommit", ""),
])
def test_macos_core_and_platform_identity(tmp_path, field, value):
    values = repository(tmp_path, macos=True)
    values[field] = value
    save(tmp_path, values)
    with pytest.raises(pins.PinError):
        pins.load_pins(tmp_path, "macos")


def test_macos_version_file_without_overrides_fails_closed(tmp_path):
    repository(tmp_path)
    (tmp_path / pins.MACOS_VERSION_FILE).write_text(GLOBAL_VERSION)
    with pytest.raises(pins.PinError, match="must all be present"):
        pins.load_pins(tmp_path, "macos")


def test_macos_version_file_is_required_to_match(tmp_path):
    repository(tmp_path, macos=True)
    (tmp_path / pins.MACOS_VERSION_FILE).write_text(LINUX_VERSION)
    with pytest.raises(pins.PinError, match=pins.MACOS_VERSION_FILE):
        pins.load_pins(tmp_path, "macos")


def test_shared_baseline_is_not_inferred_from_macos(tmp_path):
    repository(tmp_path, macos=True)
    (tmp_path / "CHROMIUM_VERSION").write_text(GLOBAL_VERSION)
    assert pins.load_pins(tmp_path, "macos")["ChromiumVersion"] == GLOBAL_VERSION
    with pytest.raises(pins.PinError, match="CHROMIUM_VERSION"):
        pins.load_shared_pins(tmp_path)


def test_unsupported_platform_is_rejected(tmp_path):
    with pytest.raises(pins.PinError, match="unsupported platform"):
        pins.load_pins(tmp_path, "android")


@pytest.mark.parametrize("platform,field", [
    ("linux", "ChromiumVersion"), ("linux", "UngoogledVersion"),
    ("linux", "UngoogledCommit"), ("linux", "UngoogledLinuxCommit"),
    ("macos", "ChromiumVersion"), ("windows", "UngoogledCommit"),
])
def test_cli_outputs_only_requested_effective_field(tmp_path, platform, field):
    repository(tmp_path, linux=True)
    result = subprocess.run([sys.executable, str(Path(pins.__file__)), "--repo", str(tmp_path),
                             "--platform", platform, "--field", field], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == pins.load_pins(tmp_path, platform)[field] + "\n"
    assert result.stderr == ""


def test_cli_invalid_pins_or_unknown_field_have_no_stdout(tmp_path):
    values = repository(tmp_path, linux=True)
    for field in ("Unknown", "ChromiumVersion"):
        if field == "ChromiumVersion":
            values.pop("LinuxUngoogledCommit")
            save(tmp_path, values)
        result = subprocess.run([sys.executable, str(Path(pins.__file__)), "--repo", str(tmp_path),
                                 "--platform", "linux", "--field", field], capture_output=True, text=True)
        assert result.returncode == 1
        assert result.stdout == ""
        assert "platform pins:" in result.stderr


@pytest.mark.parametrize("platform", pins.PLATFORMS)
def test_linux_windows_overrides_keep_macos_independent(tmp_path, platform):
    values = repository(tmp_path, linux=True, windows=True)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    result = pins.load_pins(tmp_path, platform)
    prefix = pins.PLATFORMS[platform] if platform != "macos" else ""
    for field in pins.COMMON_FIELDS:
        assert result[field] == values[prefix + field]
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize("missing", [*pins.WINDOWS_OVERRIDES, pins.WINDOWS_VERSION_FILE])
def test_windows_partial_overrides_fail_closed(tmp_path, missing):
    values = repository(tmp_path, linux=True, windows=True)
    if missing == pins.WINDOWS_VERSION_FILE:
        (tmp_path / missing).unlink()
    else:
        values.pop(missing)
        save(tmp_path, values)
    with pytest.raises(pins.PinError, match="must all be present"):
        pins.load_pins(tmp_path, "windows")
    assert pins.load_pins(tmp_path, "linux")["ChromiumVersion"] == LINUX_VERSION
    assert pins.load_pins(tmp_path, "macos")["ChromiumVersion"] == GLOBAL_VERSION


@pytest.mark.parametrize("field,value", [
    ("WindowsChromiumVersion", "153"),
    ("WindowsUngoogledVersion", GLOBAL_VERSION + "-1"),
    ("WindowsUngoogledCommit", "not-a-commit"),
    ("UngoogledWindowsVersion", GLOBAL_VERSION + "-1.1"),
    ("UngoogledWindowsVersion", LINUX_VERSION + "-2.1"),
    ("UngoogledWindowsCommit", ""),
])
def test_windows_core_and_overlay_identity(tmp_path, field, value):
    values = repository(tmp_path, windows=True)
    values[field] = value
    save(tmp_path, values)
    with pytest.raises(pins.PinError):
        pins.load_pins(tmp_path, "windows")


def test_windows_version_file_without_overrides_is_rejected(tmp_path):
    repository(tmp_path)
    (tmp_path / pins.WINDOWS_VERSION_FILE).write_text(LINUX_VERSION)
    with pytest.raises(pins.PinError, match="must all be present"):
        pins.load_pins(tmp_path, "windows")


def test_windows_version_file_must_match_effective_pin(tmp_path):
    repository(tmp_path, windows=True)
    (tmp_path / pins.WINDOWS_VERSION_FILE).write_text(GLOBAL_VERSION)
    with pytest.raises(pins.PinError, match=pins.WINDOWS_VERSION_FILE):
        pins.load_pins(tmp_path, "windows")


@pytest.mark.parametrize("platform", pins.PLATFORMS)
def test_json_cli_emits_validated_effective_pins(tmp_path, platform):
    repository(tmp_path, linux=True, windows=True, macos=True)
    command = [sys.executable, str(Path(pins.__file__)), "--repo", str(tmp_path),
               "--platform", platform, "--json"]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == pins.load_pins(tmp_path, platform)
    assert result.stderr == ""
    result = subprocess.run([*command, "--field", "ChromiumVersion"], capture_output=True, text=True)
    assert result.returncode != 0
    assert result.stdout == ""
