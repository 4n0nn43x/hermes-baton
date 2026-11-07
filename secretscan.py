"""Blocking secret scan of what a baton would send, plus the text redaction pass for transcripts.

Code is never masked (a masked secret breaks the code): a finding blocks the pass.
"""

from __future__ import annotations

import fnmatch
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import gitutil

MAX_SCAN_BYTES = 2 * 1024 * 1024
FORBIDDEN_NAMES = ("auth.json", "id_rsa", "id_ed25519", "credentials.json")
FORBIDDEN_GLOBS = ("*.pem", "*.p12")
ALLOWED_ENV_NAMES = (".env.example", ".env.sample", ".env.template")
PLACEHOLDER_WORDS = ("example", "placeholder", "your", "dummy", "changeme", "xxxx", "redacted", "fake", "test")

# Zero-width match at the start of each [\w.-] run: group 1 is the whole name, group 2 the quoted value.
# Each attempt is bounded by the run it starts on, so the scan is linear in the text; the former
# `[\w.-]*(?:api|secret|token|password)[\w.-]*...` backtracked quadratically (ReDoS).
GENERIC = re.compile(r"(?<![\w.-])(?=([\w.-]+)[\"']?\s*[:=]\s*[\"']([^\"'\s]{16,})[\"'])")
GENERIC_NAME = re.compile(r"(?i)api|secret|token|password")
RULES: list[tuple[str, re.Pattern]] = [
    ("private_key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")),
    ("api_key_sk", re.compile(r"\bsk-(?:ant-(?:api\d\d-)?|or-v1-|proj-|svcacct-)?[A-Za-z0-9_-]{20,}")),
    ("github_token", re.compile(r"\b(?:ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,})")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[abp]-[A-Za-z0-9-]{10,}")),
    ("telegram_bot_token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("generic_secret", GENERIC),
]
GENERIC_MIN_ENTROPY = 3.5


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    rule: str
    excerpt: str

    def to_dict(self) -> dict:
        return {"file": self.file, "line": self.line, "rule": self.rule, "excerpt": self.excerpt}


def entropy(text: str) -> float:
    if not text:
        return 0.0
    counts = Counter(text)
    n = len(text)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def mask(secret: str) -> str:
    return f"{secret[:4]}...({len(secret)} chars)"


def _is_placeholder(token: str) -> bool:
    low = token.lower()
    if any(word in low for word in PLACEHOLDER_WORDS):
        return True
    body = re.sub(r"^(sk-(ant-|or-v1-|proj-)?|ghp_|github_pat_|xox[abp]-|AKIA)", "", token)
    return len(set(body)) <= 4  # sk-aaaaaaaa..., 1111..., ***...


def forbidden_name(path: str) -> str | None:
    name = PurePosixPath(path).name
    if name in ALLOWED_ENV_NAMES:
        return None
    if name == ".env" or name.startswith(".env."):
        return "forbidden_name:.env"
    if name in FORBIDDEN_NAMES:
        return f"forbidden_name:{name}"
    for glob in FORBIDDEN_GLOBS:
        if fnmatch.fnmatch(name, glob):
            return f"forbidden_name:{glob}"
    return None


def iter_matches(text: str):
    """Yield (rule, start, end, secret) for every real-looking secret in `text`."""
    for rule, pattern in RULES:
        if rule == "generic_secret":
            for start, end in generic_candidates(text):
                secret = text[start:end]
                if entropy(secret) < GENERIC_MIN_ENTROPY or _is_placeholder(secret):
                    continue
                yield rule, start, end, secret
            continue
        for m in pattern.finditer(text):
            secret = m.group(0)
            if rule != "private_key" and _is_placeholder(secret):
                continue
            yield rule, m.start(), m.end(), secret


def generic_candidates(text: str):
    """(start, end) of each quoted value assigned to a name containing api/secret/token/password.

    Same spans as a plain non-overlapping finditer of `name ["']? [:=] "value"` (tested against the
    former regex): a candidate inside the previous match is skipped.
    """
    resume = 0
    for m in GENERIC.finditer(text):
        if m.start() >= resume and GENERIC_NAME.search(m.group(1)):
            resume = m.end(2) + 1
            yield m.start(2), m.end(2)


def scan_text(path: str, text: str) -> list[Finding]:
    findings = []
    line_starts = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n"]
    for rule, start, _end, secret in iter_matches(text):
        line = _line_of(line_starts, start)
        findings.append(Finding(path, line, rule, mask(secret)))
    return findings


def _line_of(line_starts: list[int], offset: int) -> int:
    lo, hi = 0, len(line_starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if line_starts[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1


def outgoing_paths(repo: Path | str, project_commit: str, base: str | None) -> list[str]:
    """Files added or changed by `project_commit` versus `base` (all files when there is no base)."""
    if not base:
        return gitutil.tree_files(repo, project_commit)
    raw = gitutil.run_git(
        ["diff-tree", "-r", "-z", "--no-renames", "--name-only", "--diff-filter=ACMRT", base, project_commit],
        repo).stdout.decode("utf-8", "replace")
    return [p for p in raw.split("\0") if p]


def scan(repo: Path | str, project_commit: str, base: str | None = "HEAD") -> list[Finding]:
    """Scan names and contents of the files `project_commit` adds or changes versus `base`.

    `base="HEAD"` resolves the repository HEAD (None on an unborn branch: every file is scanned).
    """
    if base == "HEAD":
        base = gitutil.rev_parse(repo, "HEAD")
    findings: list[Finding] = []
    for path in outgoing_paths(repo, project_commit, base):
        rule = forbidden_name(path)
        if rule:
            findings.append(Finding(path, 0, rule, PurePosixPath(path).name))
            continue
        data = gitutil.show_file(repo, project_commit, path)
        if not data or b"\0" in data[:8192]:
            continue
        findings.extend(scan_text(path, data[:MAX_SCAN_BYTES].decode("utf-8", "replace")))
    return findings


def redact_text(text: str) -> tuple[str, int]:
    """Replace every secret match with [REDACTED]; return the new text and the number of hits."""
    spans = sorted({(s, e) for _r, s, e, _sec in iter_matches(text)})
    merged: list[list[int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    parts, last = [], 0
    for s, e in merged:
        parts.append(text[last:s])
        parts.append("[REDACTED]")
        last = e
    parts.append(text[last:])
    return "".join(parts), len(merged)
