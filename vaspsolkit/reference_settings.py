"""Validation and provenance helpers for the per-Case SHE reference."""
from __future__ import annotations

from dataclasses import dataclass
import csv
import math
from pathlib import Path
from typing import Callable, Optional


InputFn = Callable[[str], str]
OutputFn = Callable[[str], object]


@dataclass(frozen=True)
class ReferenceSettings:
    value: float
    source: str = ""
    confirmed: bool = True


@dataclass(frozen=True)
class ReferenceFreshness:
    status: str
    summary_value: Optional[float] = None
    summary_source: str = ""
    detail: str = ""


def validate_she_reference(value: float) -> float:
    selected = float(value)
    if not math.isfinite(selected) or selected <= 0.0:
        raise ValueError("SHE reference 必须是有限正数")
    return selected


def unusual_she_reference(value: float) -> bool:
    selected = validate_she_reference(value)
    return selected < 3.0 or selected > 6.0


def prompt_reference_settings(
    *,
    default_value: float,
    default_source: str,
    input_fn: InputFn,
    output: OutputFn,
    explicit_value: Optional[float] = None,
    explicit_source: Optional[str] = None,
) -> ReferenceSettings:
    output("电势换算公式：U vs. SHE = Work function - SHE reference")
    if explicit_value is not None:
        value = validate_she_reference(explicit_value)
        if unusual_she_reference(value):
            output(f"警告：SHE reference {value:g} eV 位于非常用范围 3.0–6.0 eV。")
        return ReferenceSettings(value, (explicit_source or "").strip(), True)
    while True:
        raw = input_fn(f"SHE reference [{default_value:.2f} eV] >> ").strip()
        try:
            value = validate_she_reference(default_value if not raw else float(raw))
        except (TypeError, ValueError):
            output("SHE reference 必须是有限正数，请重新输入。")
            continue
        if unusual_she_reference(value):
            output(f"警告：SHE reference {value:g} eV 位于非常用范围 3.0–6.0 eV。")
            if input_fn("确认继续使用该数值？[y/N] ").strip().lower() != "y":
                continue
        source = input_fn("参考来源或说明（可选） >> ").strip() or default_source
        return ReferenceSettings(value, source, True)


def summary_reference_fields(workflow) -> dict[str, object]:
    return {
        "she_reference_eV": workflow.she_reference,
        "she_reference_source": workflow.she_reference_source,
    }


def inspect_reference_freshness(summary_path: Path, workflow) -> ReferenceFreshness:
    path = Path(summary_path)
    if not path.is_file():
        return ReferenceFreshness("missing", detail="尚未收集 summary.csv")
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    required = {"she_reference_eV", "she_reference_source"}
    if not rows or not required.issubset(rows[0]):
        return ReferenceFreshness("unknown", detail="summary.csv 缺少 SHE reference 字段")
    try:
        values = [validate_she_reference(float(row["she_reference_eV"])) for row in rows]
        sources = [row["she_reference_source"] for row in rows]
    except (KeyError, TypeError, ValueError):
        return ReferenceFreshness("unknown", detail="summary.csv 的 SHE reference 字段无效")
    first_value, first_source = values[0], sources[0]
    if any(not math.isclose(value, first_value, rel_tol=0.0, abs_tol=1e-9) for value in values) or any(source != first_source for source in sources):
        return ReferenceFreshness("unknown", detail="summary.csv 各行的 SHE reference 不一致")
    current = math.isclose(first_value, workflow.she_reference, rel_tol=0.0, abs_tol=1e-9) and first_source == workflow.she_reference_source
    return ReferenceFreshness(
        "current" if current else "stale",
        first_value,
        first_source,
        "参考参数一致" if current else "当前配置与 summary.csv 的参考参数不同",
    )
