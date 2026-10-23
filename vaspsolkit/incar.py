from __future__ import annotations

import re
from typing import Dict, Iterator, Tuple


def _statement_spans(text: str) -> Iterator[Tuple[int, int]]:
    start = index = 0
    quoted = False
    while index < len(text):
        char = text[index]
        if char == '"':
            quoted = not quoted
        elif not quoted:
            if char in "{}":
                raise ValueError("nested INCAR blocks are not supported; use explicit TAG/SUBTAG assignments")
            if char in "#!":
                yield start, index
                newline = text.find("\n", index)
                if newline < 0:
                    return
                index = newline
                start = index + 1
            elif char == "\\" and text[index + 1:index + 2] in {"\n", "\r"}:
                index += 1
                if text[index:index + 2] == "\r\n":
                    index += 1
            elif char in ";\n":
                yield start, index
                start = index + 1
        index += 1
    if quoted:
        raise ValueError("unterminated quoted INCAR value")
    yield start, len(text)


def _assignment_spans(text: str) -> Iterator[Tuple[str, str, int, int]]:
    for start, end in _statement_spans(text):
        statement = text[start:end]
        match = re.match(r"\s*([A-Za-z][A-Za-z0-9_/]*)\s*=\s*", statement)
        if match is None:
            continue
        value = re.sub(r"\\\r?\n", " ", statement[match.end():]).strip()
        yield match.group(1).upper(), value, start + match.start(1), start + len(statement.rstrip())


def incar_assignments(text: str) -> Iterator[Tuple[str, str]]:
    """Read active assignments, retaining duplicates for validation."""
    for key, value, _, _ in _assignment_spans(text):
        yield key, value


def parse_incar(text: str) -> Dict[str, str]:
    return dict(incar_assignments(text))


def replace_or_append(text: str, key: str, value: str) -> str:
    """Change active assignments without deleting sibling tags or comments."""
    key = key.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_/]*", key):
        raise ValueError(f"invalid INCAR tag: {key!r}")
    spans = [(start, end) for tag, _, start, end in _assignment_spans(text) if tag == key]
    replacement = f"{key} = {value}"
    # Reverse edits preserve the original offsets, including multiline values.
    for start, end in reversed(spans):
        text = text[:start] + replacement + text[end:]
    if spans:
        return text
    separator = "" if not text or text.endswith("\n") else "\n"
    return text + separator + replacement + "\n"
