"""Materialize immutable recipe bytes, never reconstruct historical stacks from HEAD."""
from pathlib import Path
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
BASELINE = "1a7d4b9c8f4e7c3530534682b4c233275f1f99a2"


def historical_recipes(destination):
    destination = Path(destination)
    destination.mkdir()
    with tempfile.TemporaryFile() as archive:
        subprocess.run(["git", "-C", str(ROOT), "archive", BASELINE,
                        "build", "tools", "patches", "CHROMIUM_VERSION", "CHROMIUM_MACOS_VERSION",
                        "CHROMIUM_LINUX_VERSION", "CHROMIUM_WINDOWS_VERSION"],
                       stdout=archive, check=True)
        archive.seek(0)
        with tarfile.open(fileobj=archive) as tree:
            tree.extractall(destination, filter="data")
    return destination


def donor_checkout(destination, revision):
    destination = Path(destination)
    subprocess.run(["git", "clone", "--quiet", "--shared", "--no-checkout", str(ROOT), str(destination)],
                   check=True)
    subprocess.run(["git", "-C", str(destination), "sparse-checkout", "set", "--no-cone",
                    "/build/", "/tools/", "/patches/", "/CHROMIUM*", "/UNGOOGLED*"], check=True)
    subprocess.run(["git", "-C", str(destination), "-c", "core.hooksPath=/dev/null",
                    "checkout", "--quiet", "--detach", revision], check=True)
    return destination
