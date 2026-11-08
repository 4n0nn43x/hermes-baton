"""Regressions found by the real demo run (demo/run-local.sh, two isolated Hermes v0.23.0 homes).

No Hermes import: the scanner rule is mirrored here with its source line.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "demo" / "sample"
SKIP_PARTS = {".git", "tests", ".work", "__pycache__", ".pytest_cache"}

# Hermes v0.23.0 tools/skills_guard.py:290 "destructive_root_rm": a CRITICAL finding outside tests/,
# and `hermes plugins install` refuses a dangerous verdict even with --force. demo/teardown.sh once
# held `rm -rf /demo/.work` (inside a docker run) and the install of the whole plugin was blocked.
_EXEMPT = r"(?:tmp|var/tmp|dev/shm|run|var/lib/apt/lists|var/cache/(?:apt|apk|yum|dnf))"
ROOT_RM = re.compile(
    r"rm\s+-rf\s+((?:[^\s;&|<>#][^\s;&|<>]*\s+)+?)?/(?(1)(?!/(?:\s|$)))(?:"
    rf"(?!{_EXEMPT}(?:\b|/))|{_EXEMPT}/(?:[^/\s]*/)*\.\.(?=/|[\s;&|]|$))")


def test_root_rm_rule_mirror_matches_the_old_teardown_line():
    assert ROOT_RM.search("-c 'rm -rf /demo/.work' >/dev/null 2>&1 || true")
    assert not ROOT_RM.search("-c 'rm -rf .work' >/dev/null 2>&1 || true")
    assert not ROOT_RM.search('rm -rf "$WORK"')


def test_no_recursive_root_delete_outside_tests():
    hits = []
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT)
        if not path.is_file() or SKIP_PARTS & set(rel.parts):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        hits += [f"{rel}:{n}" for n, line in enumerate(lines, 1) if ROOT_RM.search(line)]
    assert not hits, f"plugin install would be blocked (destructive_root_rm): {hits}"


def test_sample_stays_red_inside_the_plugin_checkout(tmp_path):
    """The demo copies the sample to demo/.work/project, inside the plugin checkout. Without its own
    pytest.ini, pytest took the plugin root as rootdir and tried to import the plugin's __init__.py as
    a package: 4 collection errors instead of one red test (first real demo run)."""
    plugin = tmp_path / "hermes-baton"
    plugin.mkdir()
    shutil.copy(ROOT / "pyproject.toml", plugin / "pyproject.toml")
    (plugin / "__init__.py").write_text("from . import gitutil  # relative import, like the plugin\n",
                                        encoding="utf-8")
    project = plugin / "demo" / ".work" / "project"
    shutil.copytree(SAMPLE, project)
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=project,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert proc.stdout.strip().splitlines()[-1].startswith("1 failed, 2 passed in"), proc.stdout
