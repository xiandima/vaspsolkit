from __future__ import annotations

import re


def replace_or_append(text: str, key: str, value: str) -> str:
    """Replace an INCAR assignment, including commented assignments, or append it."""
    pattern = rf"^\s*#?\s*{re.escape(key)}\s*=.*$"
    line = f"{key} = {value}"
    if re.search(pattern, text, flags=re.MULTILINE):
        return re.sub(pattern, line, text, count=1, flags=re.MULTILINE)
    return text.rstrip() + "\n" + line + "\n"
