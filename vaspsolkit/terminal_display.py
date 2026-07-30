"""Dependency-free terminal styling and display-width helpers."""
from __future__ import annotations

from dataclasses import dataclass
import os
import re
import sys
import unicodedata
from typing import Mapping, Optional, TextIO


ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
RESET = "\x1b[0m"


@dataclass(frozen=True)
class TerminalTheme:
    enabled: bool = False

    @classmethod
    def detect(
        cls,
        *,
        stream: Optional[TextIO] = None,
        environ: Optional[Mapping[str, str]] = None,
    ) -> "TerminalTheme":
        selected_stream = stream if stream is not None else sys.stdout
        selected_environ = environ if environ is not None else os.environ
        is_tty = bool(getattr(selected_stream, "isatty", lambda: False)())
        no_color = "NO_COLOR" in selected_environ
        dumb = selected_environ.get("TERM", "").lower() == "dumb"
        return cls(enabled=is_tty and not no_color and not dumb)

    def _style(self, text: str, code: str) -> str:
        return f"\x1b[{code}m{text}{RESET}" if self.enabled else text

    def action(self, text: str) -> str:
        return self._style(text, "36")

    def recommended(self, text: str) -> str:
        return self._style(text, "33")

    def success(self, text: str) -> str:
        return self._style(text, "32")

    def error(self, text: str) -> str:
        return self._style(text, "31")

    def muted(self, text: str) -> str:
        return self._style(text, "2")

    def strong(self, text: str) -> str:
        return self._style(text, "1")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def _character_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    if unicodedata.category(char) in {"Cf", "Cc"}:
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def display_width(text: str) -> int:
    return sum(_character_width(char) for char in strip_ansi(text))


def pad_display(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def wrap_display(
    text: str,
    width: int,
    *,
    subsequent_indent: str = "",
) -> list[str]:
    if width <= 0:
        raise ValueError("width must be positive")
    plain = strip_ansi(text)
    lines: list[str] = []
    current = ""
    current_width = 0
    indent_width = display_width(subsequent_indent)
    for char in plain:
        char_width = _character_width(char)
        limit = width if not lines else width - indent_width
        if current and current_width + char_width > limit:
            prefix = "" if not lines else subsequent_indent
            lines.append(prefix + current.rstrip())
            current = ""
            current_width = 0
        current += char
        current_width += char_width
    if current or not lines:
        prefix = "" if not lines else subsequent_indent
        lines.append(prefix + current.rstrip())
    return lines
