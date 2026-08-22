"""chromix — drive the Chromix stealth Chromium engine with a CloakBrowser-compatible API.

Function names, keyword arguments, return types (Playwright ``Browser`` /
``BrowserContext``) and ``CLOAKBROWSER_*`` env-var names all match the
``cloakbrowser`` wrapper (github.com/CloakHQ/CloakBrowser), so existing
CloakBrowser scripts run on Chromix by changing only the import:

    - from cloakbrowser import launch
    + from chromix import launch

    from chromix import launch

    browser = launch(proxy="http://user:pass@proxy:8080", geoip=True, humanize=True)
    page = browser.new_page()
    page.goto("https://example.com")
    browser.close()
"""
from .api import (
    HumanConfig,
    ProxySettings,
    binary_info,
    build_args,
    check_for_update,
    clear_cache,
    ensure_binary,
    get_default_stealth_args,
    launch,
    launch_async,
    launch_context,
    launch_context_async,
    launch_persistent_context,
    launch_persistent_context_async,
    maybe_resolve_geoip,
    resolve_human_config,
)

__version__ = "151.0.7922.174"

__all__ = [
    "launch", "launch_async", "launch_context", "launch_context_async",
    "launch_persistent_context", "launch_persistent_context_async",
    "ProxySettings", "build_args", "maybe_resolve_geoip",
    "get_default_stealth_args", "ensure_binary", "clear_cache",
    "binary_info", "check_for_update", "HumanConfig", "resolve_human_config",
    "__version__",
]
