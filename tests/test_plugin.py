"""Hermes wiring: manifest vs real registrations (fake ctx), tools, check_fn, pre_verify, /baton.

No Hermes import: the fake ctx records what register() asks for, with the v0.23.0 signatures
(hermes_cli/plugins.py:463, :635, :649, :878, :935; hermes_cli/plugins_content.py:32).
"""

from __future__ import annotations

import inspect
import json
import re
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from helpers import make_bare, make_repo  # noqa: E402

from baton_plugin import ids, secretscan  # noqa: E402
from baton_plugin.store import BatonStore  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "demo" / "sample"


class FakeCtx:
    def __init__(self, settings: dict | None = None, cwd: str | None = None) -> None:
        self.settings = dict(settings or {})
        self.cwd = cwd
        self.tools: dict = {}
        self.hooks: dict = {}
        self.commands: dict = {}
        self.cli: dict = {}
        self.skills: dict = {}
        self.redaction: list = []

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_tool(self, name, toolset, schema, handler, check_fn=None, requires_env=None, is_async=False,
                      description="", emoji="", override=False):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler, "check_fn": check_fn,
                            "description": description, "is_async": is_async}

    def register_hook(self, hook_name, callback, *, hold_stream=False):
        self.hooks.setdefault(hook_name, []).append(callback)

    def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        self.commands[name] = {"handler": handler, "description": description, "args_hint": args_hint}

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli[name] = {"help": help, "setup_fn": setup_fn, "handler_fn": handler_fn, "description": description}

    def register_skill(self, name, path, description="", frontmatter=None):
        assert ":" not in name and Path(path).exists()
        self.skills[name] = {"path": Path(path), "description": description}

    def register_redaction_patterns(self, patterns) -> int:
        self.redaction.extend(patterns)
        return len(patterns)

    def session_cwd(self):
        return self.cwd


def _yaml_list(text: str, key: str) -> list[str]:
    m = re.search(rf"^{key}:\n((?:  - .+\n)+)", text, re.MULTILINE)
    return [line.strip()[2:].strip() for line in m.group(1).splitlines()] if m else []


def _yaml_schema_keys(text: str) -> list[str]:
    block = text.split("\nconfig_schema:\n", 1)[1]
    keys = []
    for line in block.splitlines():
        if line and not line.startswith(" "):
            break
        m = re.match(r"^  ([a-z_]+):\s*$", line)
        if m:
            keys.append(m.group(1))
    return keys


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "hermes-home"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    return h


@pytest.fixture
def registered(plugin, home):
    ctx = FakeCtx()
    plugin.register(ctx)
    return ctx


def test_manifest_matches_registrations(registered):
    text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    assert sorted(_yaml_list(text, "provides_tools")) == sorted(registered.tools)
    assert sorted(_yaml_list(text, "provides_hooks")) == sorted(registered.hooks)
    assert re.search(r'^requires_hermes: ">=0\.23\.0"$', text, re.MULTILINE)


def test_config_schema_lists_every_setting(plugin):
    text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    assert _yaml_schema_keys(text) == list(plugin.SETTING_KEYS)


def test_tool_schemas_are_function_shaped(registered):
    for name, entry in registered.tools.items():
        schema = entry["schema"]
        assert schema["name"] == name and entry["toolset"] == "baton"
        assert schema["parameters"]["type"] == "object"
        assert "id" in schema["parameters"]["required"]
        assert not entry["is_async"]


def test_command_and_cli_registered(registered):
    assert "baton" in registered.commands and "baton" in registered.cli
    params = inspect.signature(registered.commands["baton"]["handler"]).parameters
    assert "session_id" in params  # declared, so the dispatcher passes it (plugin_command_dispatch.py)
    assert not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def test_skills_registered_with_matching_descriptions(registered):
    assert set(registered.skills) == {"baton", "baton-protocol"}
    for name, entry in registered.skills.items():
        text = entry["path"].read_text(encoding="utf-8")
        m = re.search(r"^description:\s*(.+)$", text, re.MULTILINE)
        assert m and m.group(1).strip() == entry["description"]
        assert len(entry["description"]) <= 60
        assert re.search(rf"^name:\s*{re.escape(name)}$", text, re.MULTILINE)


def _literal_prefix(pattern: str) -> str:  # mirrors agent/redact.py:1782 (v0.23.0)
    for i, ch in enumerate(pattern):
        if ch in "[(\\.?*+|{^$":
            return pattern[:i]
    return pattern


SAMPLES = {
    "private_key": "-----BEGIN OPENSSH PRIVATE KEY-----",
    "api_key_sk": "sk-ant-api03-" + "abcdEFGH1234ijklMNOP5678",
    "github_token": "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
    "aws_access_key": "AKIA" + "ABCDEFGHIJKLMNOP",
    "slack_token": "xoxb-" + "1234567890-abcdefghij",
}


def test_redaction_patterns_are_accepted_shapes_and_cover_secretscan(registered):
    pats = registered.redaction
    assert pats
    for p in pats:
        re.compile(p)
        assert len(_literal_prefix(p)) >= 2, p
        assert "|" not in p, p
    for rule, sample in SAMPLES.items():
        assert any(name == rule for name, _ in secretscan.RULES)
        assert any(re.search(p, sample) for p in pats), rule


def test_check_fn_follows_role_at_call_time(plugin, home):
    ctx = FakeCtx({"role": "local"})
    plugin.register(ctx)
    check = ctx.tools["baton_check"]["check_fn"]
    assert check() is False
    ctx.settings["role"] = "remote"
    assert check() is True
    ctx.settings["role"] = "both"
    assert check() is True
    ctx.settings["role"] = "nonsense"
    assert check() is True  # unknown role falls back to the default "both"


def test_tool_handlers_return_json_errors(registered):
    handler = registered.tools["baton_check"]["handler"]
    out = json.loads(handler({"id": ids.new_id()}, session_id="s1", task_id="t"))
    assert out["error"] == "not_received"
    out = json.loads(handler({"id": "../etc"}))
    assert out["error"] == "invalid_argument"
    registered.settings["role"] = "local"
    out = json.loads(registered.tools["baton_receive"]["handler"]({"id": ids.new_id()}))
    assert out["error"] == "role_local"


def _remote_record(home: Path, baton_id: str, session_id: str, worktree: Path) -> None:
    BatonStore(home / "plugin-data" / "baton").save_remote(baton_id, {
        "id": baton_id, "state": "RUNNING", "session_id": session_id, "worktree": str(worktree),
        "last_check": None, "result": None})


def test_pre_verify_wired_and_role_gated(registered, home, tmp_path):
    hook = registered.hooks["pre_verify"][0]
    kwargs = dict(platform="cli", model="m", coding=True, attempt=0, final_response="done")
    assert hook(session_id="s1", changed_paths=[], **kwargs) is None
    baton_id = ids.new_id()
    _remote_record(home, baton_id, "s1", tmp_path / "missing-worktree")
    out = hook(session_id="s1", changed_paths=["/tmp/x.py"], **kwargs)
    assert out["action"] == "continue" and baton_id in out["message"] and "baton_check" in out["message"]
    assert hook(session_id="other", changed_paths=["/tmp/x.py"], **kwargs) is None
    registered.settings["role"] = "local"
    assert hook(session_id="s1", changed_paths=["/tmp/x.py"], **kwargs) is None


def test_pre_verify_never_raises(registered, home):
    folder = home / "plugin-data" / "baton" / "remote"
    folder.mkdir(parents=True)
    (folder / f"{ids.new_id()}.json").write_text("[not a dict]")
    assert registered.hooks["pre_verify"][0](session_id="s", changed_paths=["/x"]) is None


def _sample_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "laptop" / "sample"
    shutil.copytree(SAMPLE, repo)
    make_repo(repo, files={})
    (repo / "calc.py").write_text((repo / "calc.py").read_text() + "\n# wip\n")
    return repo


def test_slash_usage_and_errors(registered):
    slash = registered.commands["baton"]["handler"]
    assert "Usage: /baton" in slash("")
    assert "Usage: /baton" in slash("--dry-run")
    assert "missing_remote_git" in slash("fix it --dry-run")
    assert "baton:" in slash('fix "unclosed')


def test_slash_dry_run_uses_session_cwd(plugin, home, tmp_path):
    repo = _sample_repo(tmp_path)
    bare = make_bare(tmp_path / "shared.git")
    ctx = FakeCtx({"remote_git": str(bare)}, cwd=str(repo))
    plugin.register(ctx)
    out = ctx.commands["baton"]["handler"]('fix mean() --done-when "python -m pytest -q" --dry-run',
                                           session_id=None, platform="cli")
    assert "dry run" in out and "calc.py" in out and "python -m pytest -q" in out
    assert not (home / "plugin-data" / "baton" / "batons").exists()  # dry run writes no record


def test_slash_status_empty(registered):
    assert registered.commands["baton"]["handler"]("status") == "baton: no batons yet"
