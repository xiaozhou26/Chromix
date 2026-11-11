"""Historical 226-patch recipes are tested against their fixed reviewed checkout."""
import atexit
from functools import cache
from pathlib import Path
import subprocess
import tempfile

BASE = 'f6da88f521078f8577c65b4b8d5426ab36a4606d'


@cache
def recipe_repo():
    temporary = tempfile.TemporaryDirectory(prefix='mac156-226-recipes-')
    atexit.register(temporary.cleanup)
    root = Path(temporary.name).resolve() / 'repo'
    current = Path(__file__).resolve().parents[2]
    subprocess.run(['git', 'clone', '--quiet', '--shared', '--no-checkout', str(current), str(root)], check=True)
    subprocess.run(['git', '-C', str(root), 'checkout', '--quiet', BASE], check=True)
    return root
