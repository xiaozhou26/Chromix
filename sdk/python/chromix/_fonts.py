"""Linux Fontconfig wiring for the bundled Windows font assets."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any


def linux_font_env(executable: str | os.PathLike) -> dict[str, str]:
    """Return ``FONTCONFIG_FILE`` when a Linux bundle contains ``fonts/``."""
    if sys.platform != "linux":
        return {}

    fonts_dir = Path(executable).resolve().parent / "fonts"
    template = fonts_dir / "fonts.conf.template"
    if not template.is_file():
        return {}

    try:
        cache_dir = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) \
            / "chromix" / "fontconfig"
        cache_dir.mkdir(parents=True, exist_ok=True)
        config = template.read_text(encoding="utf-8")
        config = config.replace("@FONTS_DIR@", str(fonts_dir))
        config = config.replace("@CACHE_DIR@", str(cache_dir))
        config_path = Path(tempfile.gettempdir()) / f"chromix-fontconfig-{os.getuid()}.conf"
        config_path.write_text(config, encoding="utf-8")
        return {"FONTCONFIG_FILE": str(config_path)}
    except OSError:
        return {}


def apply_font_env(executable: str | os.PathLike,
                   launch_kwargs: dict[str, Any]) -> None:
    """Merge the bundled-font environment into Playwright launch options."""
    font_env = linux_font_env(executable)
    user_env = launch_kwargs.get("env")
    if not font_env and user_env is None:
        return
    merged = dict(os.environ)
    merged.update(font_env)
    if user_env:
        merged.update(user_env)
    launch_kwargs["env"] = merged
