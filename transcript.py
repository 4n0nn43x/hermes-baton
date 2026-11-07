"""Session transcript for the remote agent: `hermes sessions export --redact`, an extra redaction pass
with the secret patterns, then trimming (last turns, short tool outputs, size cap)."""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .secretscan import redact_text

CAP_BYTES = 64 * 1024
MAX_TURNS = 20
TOOL_OUTPUT_BYTES = 2048
EXPORT_TIMEOUT_S = 60
_HEADING = re.compile(r"^### (User|Assistant|Tool|System)\b.*$", re.MULTILINE)


@dataclass
class TranscriptResult:
    text: str
    bytes: int
    extra_hits: int
    cli_redact: bool = False
    note: str = ""

    @property
    def data(self) -> bytes:
        return self.text.encode("utf-8")


def default_hermes_argv() -> list[str]:
    return [sys.executable, "-m", "hermes_cli.main"]


def _cut(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", "ignore") + f"\n[... {len(raw) - limit} bytes cut]\n"


def trim(markdown: str, max_turns: int = MAX_TURNS, tool_bytes: int = TOOL_OUTPUT_BYTES) -> str:
    """Keep the last `max_turns` user/assistant messages and the tool outputs between them."""
    matches = list(_HEADING.finditer(markdown))
    if not matches:
        return markdown
    sections = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        sections.append((m.group(1), markdown[m.start():end]))
    turn_idx = [i for i, (role, _) in enumerate(sections) if role in ("User", "Assistant")]
    start = turn_idx[-max_turns] if len(turn_idx) > max_turns else 0
    kept = []
    for role, body in sections[start:]:
        if role == "System":
            continue
        kept.append(_cut(body, tool_bytes) if role == "Tool" else body)
    dropped = len(turn_idx) - min(len(turn_idx), max_turns)
    head = f"_{dropped} earlier messages omitted._\n\n" if dropped else ""
    return head + "".join(kept)


def _task_block(task: str) -> str:
    return f"# Baton task\n\n{task.strip()}\n"


def compose(task: str, session_md: str | None, cap: int = CAP_BYTES) -> str:
    text = _task_block(task)
    if session_md:
        text += "\n## Session (recent turns)\n\n" + trim(session_md)
    raw = text.encode("utf-8")
    if len(raw) > cap:
        # Keep the task and the most recent part of the session.
        task_part = _task_block(task).encode("utf-8")[: cap // 4]
        marker = b"\n[... transcript cut to fit the size cap ...]\n"
        tail = raw[-(cap - len(task_part) - len(marker)):]
        text = (task_part + marker + tail).decode("utf-8", "ignore")
    return text


def run_export(session_id: str, hermes_argv: Sequence[str], runner: Callable = subprocess.run,
               timeout: float = EXPORT_TIMEOUT_S) -> str:
    """Run the CLI export into a private temp dir; md exports write one file per session there."""
    with tempfile.TemporaryDirectory(prefix="baton-export-") as tmp:
        out_dir = Path(tmp) / "out"
        argv = [*hermes_argv, "sessions", "export", f"{out_dir}/", "--session-id", session_id,
                "--format", "md", "--redact"]
        proc = runner(argv, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()[-300:]
            raise RuntimeError(f"export exited {proc.returncode}: {err}")
        candidates = sorted(out_dir.glob("*.md")) if out_dir.is_dir() else []
        if not candidates:
            raise RuntimeError("export produced no markdown file")
        return candidates[0].read_text(encoding="utf-8", errors="replace")


def export(session_id: str | None, hermes_argv: Sequence[str] | None = None, *, task: str = "",
           runner: Callable = subprocess.run, cap: int = CAP_BYTES) -> TranscriptResult:
    """Build the transcript. A failed export degrades to the task text alone (noted, never fatal)."""
    session_md, cli_redact, note = None, False, ""
    if session_id:
        try:
            session_md = run_export(session_id, list(hermes_argv or default_hermes_argv()), runner)
            cli_redact = True
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            note = f"session export unavailable ({type(exc).__name__}: {str(exc)[:200]})"
    text, hits = redact_text(compose(task, session_md, cap))
    return TranscriptResult(text, len(text.encode("utf-8")), hits, cli_redact, note)
