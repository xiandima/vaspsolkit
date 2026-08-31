"""Fixed task catalogue for the dependency-free interactive menu."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class MenuAction:
    code: str
    title_zh: str
    command: Optional[str]
    effect: str
    group: str
    requires_jobs: bool = False


MENU_ACTIONS: Tuple[MenuAction, ...] = (
    MenuAction("01", "刷新当前 Case 状态", None, "read-only", "status"),
    MenuAction("02", "执行推荐下一步", None, "recommended", "status"),
    MenuAction("03", "查看完整状态与诊断", None, "read-only", "status"),
    MenuAction("10", "检查基础输入文件", None, "read-only", "inputs"),
    MenuAction("11", "初始化 Case 配置", "init", "file-changing", "inputs"),
    MenuAction(
        "12",
        "设置调度器、分区、节点和任务数",
        "configure-scheduler",
        "file-changing",
        "inputs",
    ),
    MenuAction("13", "设置电化学参考参数", "configure-reference", "file-changing", "inputs"),
    MenuAction("20", "准备中性结构优化", "prepare-neutral", "file-changing", "neutral"),
    MenuAction("21", "提交中性任务", "submit-neutral", "external-submit", "neutral"),
    MenuAction("22", "检查中性任务与收敛", "check-neutral", "read-only", "neutral"),
    MenuAction("30", "准备带电点目录", "prepare-charge", "file-changing", "charge"),
    MenuAction("31", "检查带电点输入", "check-prepared", "read-only", "charge"),
    MenuAction(
        "32",
        "选择并提交带电点",
        "submit-selected",
        "external-submit",
        "charge",
        True,
    ),
    MenuAction("40", "监测已记录任务", "monitor", "read-only", "jobs"),
    MenuAction("41", "更换排队任务节点", None, "external-cancel", "jobs", True),
    MenuAction(
        "42",
        "取消排队任务并恢复为 PREPARED",
        "reset-queued",
        "external-cancel",
        "jobs",
        True,
    ),
    MenuAction("50", "收敛检查与错误诊断", "check", "read-only", "results"),
    MenuAction("51", "修复失败任务", "repair", "file-changing", "results", True),
    MenuAction("60", "收集结果", "collect", "file-changing", "results"),
    MenuAction("61", "结果审计", "audit", "file-changing", "results"),
    MenuAction("62", "后处理与 E-U 分析", "postprocess", "file-changing", "results"),
    MenuAction("90", "帮助与命令说明", None, "read-only", "help"),
    MenuAction("00", "退出", None, "exit", "exit"),
)


def normalize_code(value: str) -> str:
    clean = value.strip()
    if not clean.isdigit() or len(clean) > 2:
        raise ValueError("任务编号必须是 0 到 99 的数字")
    return f"{int(clean):02d}"


def action_by_code(value: str) -> MenuAction:
    code = normalize_code(value)
    for action in MENU_ACTIONS:
        if action.code == code:
            return action
    raise KeyError(f"未知任务编号: {code}")
