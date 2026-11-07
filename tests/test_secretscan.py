from __future__ import annotations

import random
import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from helpers import git, make_repo  # noqa: E402

from baton_plugin import secretscan  # noqa: E402
from baton_plugin.capture import ghost_commit  # noqa: E402

# Built by concatenation so this file never contains a literal token.
SK = "sk-" + "proj-" + "Q7f9Kx2LmN4pR8sT1vW3yZ6bC0dE5gH"
ANT = "sk-ant-api03-" + "aB3dE6gH9jK2mN5pQ8sT1vW4yZ7cF0hJ"
GHP = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
PAT = "github_pat_" + "11ABCDEFG0123456789_abcdefghijklmnop"
AWS = "AKIA" + "Z7Q4M2X9K3P8R5T1"
SLACK = "xoxb-" + "1234567890-0987654321-AbCdEfGhIjKlMnOp"
TG = "8123456789" + ":" + "AAH7k2Lm9Np3Qr5St7Uv9Wx1Yz3Ab5Cd7Ef"
PEM = "-----BEGIN " + "OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----\n"


@pytest.mark.parametrize("token,rule", [
    (SK, "api_key_sk"), (ANT, "api_key_sk"), (GHP, "github_token"), (PAT, "github_token"),
    (AWS, "aws_access_key"), (SLACK, "slack_token"), (TG, "telegram_bot_token"),
])
def test_each_token_rule(token, rule):
    findings = secretscan.scan_text("f.py", f"x = 1\nKEY = '{token}'\n")
    assert rule in {f.rule for f in findings}
    hit = next(f for f in findings if f.rule == rule)
    assert hit.line == 2 and token not in hit.excerpt and hit.excerpt.endswith("chars)")


def test_private_key_block():
    findings = secretscan.scan_text("k", PEM)
    assert [f.rule for f in findings] == ["private_key"]


def test_generic_secret_high_entropy():
    findings = secretscan.scan_text("cfg.py", 'api_key = "Zq8#vL2!pX9@mK4$wR7"\n')
    assert [f.rule for f in findings] == ["generic_secret"]


@pytest.mark.parametrize("text", [
    'password = "aaaaaaaaaaaaaaaaaaaa"\n',            # low entropy
    'api_key = "your-api-key-goes-here-123"\n',       # placeholder
    "OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxx\n",  # placeholder sk
    "AWS = 'AKIAIOSFODNN7EXAMPLE'\n",                 # documented AWS example
    "token = os.environ['TOKEN']\n",
])
def test_known_false_positives(text):
    assert secretscan.scan_text("f", text) == []


@pytest.mark.parametrize("name,blocked", [
    (".env", True), ("config/.env.production", True), (".env.example", False), ("auth.json", True),
    ("id_rsa", True), ("keys/id_ed25519", True), ("srv.pem", True), ("c.p12", True),
    ("credentials.json", True), ("env.py", False), ("my.env.txt", False),
])
def test_forbidden_names(name, blocked):
    assert (secretscan.forbidden_name(name) is not None) is blocked


def test_scan_repo_diff_and_untracked(tmp_path):
    repo = make_repo(tmp_path / "r", {"a.py": "x = 1\n", "old.py": f"T = '{GHP}'\n"})
    (repo / "a.py").write_text(f"x = '{SK}'\n")
    (repo / ".env").write_text("A=1\n")
    (repo / ".env.example").write_text("A=\n")
    res = ghost_commit(repo)
    findings = secretscan.scan(repo, res.project_commit)
    got = {(f.file, f.rule) for f in findings}
    assert ("a.py", "api_key_sk") in got and (".env", "forbidden_name:.env") in got
    assert all(f.file != "old.py" for f in findings)  # unchanged committed file: not in the diff
    assert all(f.file != ".env.example" for f in findings)


def test_scan_unborn_scans_everything(tmp_path):
    repo = make_repo(tmp_path / "r", {"a.py": f"k = '{AWS}'\n"}, commit=False)
    res = ghost_commit(repo)
    assert [f.rule for f in secretscan.scan(repo, res.project_commit)] == ["aws_access_key"]


def test_scan_skips_binary(tmp_path):
    repo = make_repo(tmp_path / "r")
    (repo / "blob.bin").write_bytes(b"\0\1" + SK.encode())
    res = ghost_commit(repo)
    assert secretscan.scan(repo, res.project_commit) == []


def test_scan_clean_repo_is_empty(tmp_path):
    repo = make_repo(tmp_path / "r")
    (repo / "a.py").write_text("def f():\n    return 2\n")
    assert secretscan.scan(repo, ghost_commit(repo).project_commit) == []


def test_redact_text_counts_hits():
    text, hits = secretscan.redact_text(f"a {SK} b {GHP} c")
    assert hits == 2 and SK not in text and GHP not in text and text.count("[REDACTED]") == 2


def test_entropy_values():
    assert secretscan.entropy("aaaa") == 0
    assert secretscan.entropy("abcd") == 2


# ReDoS regression: the former generic_secret regex, kept here as the reference for the same spans.
OLD_GENERIC = re.compile(r"(?i)[\w.-]*(?:api|secret|token|password)[\w.-]*[\"']?\s*[:=]\s*[\"']([^\"'\s]{16,})[\"']")
PIECES = ["api", "TOKEN", "Secret", "password", "ſecret", "key", "x", "_", ".", "-", "=", ":", " ", "\t", "\n",
          '"', "'", "#", "/", "Zq8#vL2!pX9@mK4$wR7", "abcdefghijklmnopqrstuv", "AKIA"]


def test_generic_candidates_match_former_regex():
    rng = random.Random(7)
    for _ in range(30000):
        text = "".join(rng.choice(PIECES) for _ in range(rng.randint(0, 14)))
        old = [(m.start(1), m.end(1)) for m in OLD_GENERIC.finditer(text)]
        assert list(secretscan.generic_candidates(text)) == old, text


@pytest.mark.parametrize("unit", ["a", "token", "api_key ", "api_key= ", "k='" + "x" * 40, "sk-", "1",
                                  "-----BEGIN A ", "a.b-c_", "password\t:\t", "secret='aaaaaaaaaaaaaaaaaaaa' "])
def test_scan_is_linear_on_adversarial_2mib(unit):
    text = (unit * (secretscan.MAX_SCAN_BYTES // len(unit) + 1))[:secretscan.MAX_SCAN_BYTES]
    for fn in (lambda: secretscan.scan_text("min.js", text), lambda: secretscan.redact_text(text)):
        start = time.perf_counter()
        fn()
        assert time.perf_counter() - start < 1.0
