"""Small bilingual text catalog for the workbench prototype."""
from __future__ import annotations


_TEXT = {
    "zh": {
        "nav.workspace": "首页",
        "nav.tasks": "计算流程",
        "nav.overview": "总览",
        "nav.inputs": "输入与结构",
        "nav.neutral": "中性计算",
        "nav.charges": "带电点",
        "nav.queue": "任务与队列",
        "nav.results": "结果与后处理",
        "nav.settings": "设置",
        "nav.exit": "退出",
        "technical.nelect": "NELECT",
    },
    "en": {
        "nav.workspace": "Workspace",
        "nav.tasks": "Tasks",
        "nav.overview": "Overview",
        "nav.inputs": "Input check",
        "nav.neutral": "Neutral job",
        "nav.charges": "Charge points",
        "nav.queue": "Job queue",
        "nav.results": "Results",
        "nav.settings": "Settings",
        "nav.exit": "Exit",
        "technical.nelect": "NELECT",
    },
}


def tr(language: str, key: str) -> str:
    if language not in _TEXT:
        raise ValueError(f"unsupported language: {language}")
    return _TEXT[language].get(key, key)
