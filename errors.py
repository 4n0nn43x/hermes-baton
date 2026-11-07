"""The single error type of the plugin: a stable machine code plus a human message."""

from __future__ import annotations

from typing import Any


class BatonError(Exception):
    """Raised for every expected failure. `code` is stable and documented in SPEC.md."""

    def __init__(self, code: str, message: str = "", details: Any = None) -> None:
        self.code = code
        self.message = message or code
        self.details = details
        super().__init__(f"{code}: {self.message}")

    def to_dict(self) -> dict:
        out = {"error": self.code, "message": self.message}
        if self.details is not None:
            out["details"] = self.details
        return out
