"""Pure renderer for the fixed-number VASPsolKit menu."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Tuple

from .terminal_display import TerminalTheme, display_width, pad_display, wrap_display


GROUP_LABELS = {
    "status": "状态与推荐",
    "inputs": "输入与设置",
    "neutral": "中性计算",
    "charge": "带电点计算",
    "jobs": "任务管理",
    "results": "检查与结果",
}


@dataclass(frozen=True)
class MenuItemView:
    code: str
    title: str
    group: str
    available: bool
    reason: str = ""


@dataclass(frozen=True)
class MenuView:
    case_label: str
    stage: str
    neutral_status: str
    charge_done: int
    charge_total: int
    recommendation_code: str
    recommendation_title: str
    recommendation_reason: str
    items: Tuple[MenuItemView, ...]


def _rule(label: str, width: int, character: str = "-") -> str:
    center = f" {label} "
    remaining = max(0, width - display_width(center))
    left = remaining // 2
    return character * left + center + character * (remaining - left)


def _status(theme: TerminalTheme, status: str) -> str:
    if status == "CONVERGED":
        return theme.success(status)
    if status in {"FAILED", "NEEDS_REVIEW", "BLOCKED"}:
        return theme.error(status)
    if status in {"SUBMITTED", "QUEUED", "RUNNING", "UNKNOWN"}:
        return theme.recommended(status)
    return status


def _summary_lines(
    label: str,
    value: str,
    *,
    width: int,
    style: Callable[[str], str],
) -> List[str]:
    prefix = f" {label:<10}"
    available = max(1, width - display_width(prefix))
    fragments = wrap_display(value, available)
    continuation = " " * display_width(prefix)
    return [
        (prefix if index == 0 else continuation) + style(fragment)
        for index, fragment in enumerate(fragments)
    ]


def _item_lines(
    item: MenuItemView,
    *,
    theme: TerminalTheme,
    width: int,
) -> List[str]:
    prefix_plain = f"    {item.code}) {item.title}"
    if item.available:
        return [f"    {theme.action(item.code)}) {item.title}"]
    reason = f"[不可用：{item.reason}]"
    reason_start = width - display_width(reason)
    if display_width(prefix_plain) < reason_start:
        return [theme.muted(pad_display(prefix_plain, reason_start) + reason)]
    lines = [theme.muted(prefix_plain)]
    lines.extend(
        theme.muted("        " + line)
        for line in wrap_display(reason, width - 8)
    )
    return lines


def render_menu(
    view: MenuView,
    *,
    theme: TerminalTheme,
    width: int = 80,
) -> List[str]:
    lines: List[str] = [theme.action(_rule("VASPsolKit", width, "=")), ""]
    lines.extend(
        _summary_lines(
            "Case",
            view.case_label,
            width=width,
            style=theme.strong,
        )
    )
    lines.extend(
        _summary_lines(
            "Stage",
            view.stage,
            width=width,
            style=lambda text: text,
        )
    )
    lines.append(
        f" Progress  Neutral {_status(theme, view.neutral_status)}"
        f"   Charge {view.charge_done} / {view.charge_total}"
    )
    lines.append("")
    recommendation = (
        f">> [{view.recommendation_code}] 推荐下一步："
        f"{view.recommendation_title}"
    )
    lines.extend(
        " " + theme.recommended(line)
        for line in wrap_display(
            recommendation,
            width - 1,
            subsequent_indent="    ",
        )
    )
    reason_lines = wrap_display(
        view.recommendation_reason,
        width - 4,
        subsequent_indent="    ",
    )
    lines.extend(
        "    " + line if index == 0 else line
        for index, line in enumerate(reason_lines)
    )
    lines.append("")

    current_group = ""
    utility_started = False
    for item in view.items:
        if item.group in {"help", "exit"}:
            if not utility_started:
                if lines[-1] != "":
                    lines.append("")
                utility_started = True
        elif item.group != current_group:
            if current_group and lines[-1] != "":
                lines.append("")
            current_group = item.group
            lines.append(theme.muted(_rule(GROUP_LABELS[current_group], width)))
        lines.extend(_item_lines(item, theme=theme, width=width))

    lines.extend(["", theme.muted("-" * min(width, 62))])
    return lines
