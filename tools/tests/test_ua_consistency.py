"""Regression tests for automatic UA and Client Hints version consistency."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PATCHES = ROOT / "patches"


def added_lines(patch_name: str) -> str:
    """Return added patch lines, excluding the +++ file header."""
    return "\n".join(
        line[1:]
        for line in (PATCHES / patch_name).read_text(encoding="utf-8").splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )


def test_fingerprint_data_has_no_stale_browser_version_constants():
    data = (PATCHES / "0107-components-ungoogled-fingerprint_data.patch").read_text(
        encoding="utf-8"
    )
    assert "kChromiumVersions" not in data
    assert "kChromeDefaultVersion" not in data
    assert "149.0.7827" not in data
    assert "#include <cstddef>" in data
    assert "std::size_t kMacosGpuModelCount" in data
    assert "std::size_t kGpuModelCount" in data
    assert all(
        line.startswith(
            (
                "diff --git ",
                "new file mode ",
                "index ",
                "--- ",
                "+++ ",
                "@@",
                "+",
                "-",
                " ",
                "\\\\",
            )
        )
        for line in data.splitlines()
    )


def test_ua_version_override_is_shared_by_product_and_brand_metadata():
    ua = added_lines("0004-components-embedder_support-user_agent_utils-cc.patch")
    assert "#include \"base/uxr_config.h\"" in ua
    assert "GetEffectiveUserAgentFullVersion" in ua
    assert "UxrConfig::GetInstance().Get(\"uxr-ua-full-version\")" in ua
    assert "GetEffectiveUserAgentMajorVersion" in ua
    assert "GetUserAgentBrandList(" in ua
    assert "metadata.full_version = GetEffectiveUserAgentFullVersion()" in ua


def test_version_override_rewrites_product_version_before_building_ua():
    ua = added_lines("0004-components-embedder_support-user_agent_utils-cc.patch")
    assert "ReplaceProductVersion" in ua
    assert "user_agent_version" in ua
    assert "product = ReplaceProductVersion(product, user_agent_version)" in ua


def test_renderer_receives_ua_config_before_initialize_renderer():
    host = (PATCHES / "0005-content-browser-renderer_host-render_process_host_impl-cc.patch").read_text(
        encoding="utf-8"
    )
    config_call = 'GetRendererInterface()->SetUxrConfig(std::move(uxr_cfg));'
    initialize_call = "GetRendererInterface()->InitializeRenderer("
    assert host.count(config_call) == 1
    assert host.index(config_call) < host.index(initialize_call)


def test_renderer_initialization_rebuilds_ua_and_brand_versions_from_override():
    host = (PATCHES / "0005-content-browser-renderer_host-render_process_host_impl-cc.patch").read_text(
        encoding="utf-8"
    )
    assert "effective_user_agent_metadata.full_version = ua_full_version;" in host
    assert "effective_user_agent.replace(product_separator + 1" in host
    assert "brand_full_version_list" in host

def test_version_override_alias_remains_the_single_cli_entry_point():
    main = added_lines("0036-chrome-app-chrome_main-fingerprint-normalize.patch")
    assert '"fingerprint-brand-version",        "uxr-ua-full-version"' in main


def test_browser_override_rewrite_has_required_version_include_and_cache_migration():
    host = (PATCHES / "0005-content-browser-renderer_host-render_process_host_impl-cc.patch").read_text(
        encoding="utf-8"
    )
    assert '+#include "base/version.h"' in host
    update = (ROOT / "build" / "windows" / "update-restored-source.ps1").read_text(
        encoding="utf-8"
    )
    assert "render_process_host_impl.cc" in update
    assert "effective_user_agent_metadata.full_version = ua_full_version;" in update
