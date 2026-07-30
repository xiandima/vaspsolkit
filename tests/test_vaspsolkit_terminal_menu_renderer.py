from __future__ import annotations


def _view():
    from vaspsolkit.terminal_menu_renderer import MenuItemView, MenuView

    return MenuView(
        case_label="Pt111 / OOH",
        stage="neutral_converged",
        neutral_status="CONVERGED",
        charge_done=0,
        charge_total=5,
        recommendation_code="30",
        recommendation_title="准备带电点目录",
        recommendation_reason="中性结构已收敛，可由 CONTCAR 和 CHGCAR 生成带电点。",
        items=(
            MenuItemView("01", "刷新当前 Case 状态", "status", True, ""),
            MenuItemView("11", "初始化 Case 配置", "inputs", False, "Case 已经初始化"),
            MenuItemView("30", "准备带电点目录", "charge", True, ""),
            MenuItemView("00", "退出", "exit", True, ""),
        ),
    )


def test_plain_menu_has_approved_information_order_and_reasons() -> None:
    from vaspsolkit.terminal_display import TerminalTheme
    from vaspsolkit.terminal_menu_renderer import render_menu

    lines = render_menu(_view(), theme=TerminalTheme(False), width=80)
    text = "\n".join(lines)
    assert "VASPsolKit" in lines[0]
    assert text.index("Case") < text.index("推荐下一步") < text.index("状态与推荐")
    assert "[不可用：Case 已经初始化]" in text
    assert lines[-1] == "-" * 62
    assert "输入任务编号" not in text


def test_every_plain_menu_line_fits_eighty_columns() -> None:
    from vaspsolkit.terminal_display import TerminalTheme, display_width
    from vaspsolkit.terminal_menu_renderer import render_menu

    lines = render_menu(_view(), theme=TerminalTheme(False), width=80)
    assert all(display_width(line) <= 80 for line in lines)


def test_colored_menu_wraps_long_dynamic_content_without_losing_reason() -> None:
    from dataclasses import replace

    from vaspsolkit.terminal_display import TerminalTheme, display_width
    from vaspsolkit.terminal_menu_renderer import MenuItemView, render_menu

    view = replace(
        _view(),
        case_label="Pt111 / " + "非常长的吸附体系名称" * 6,
        recommendation_title="准备带电点计算并检查全部输入文件" * 4,
        items=(
            MenuItemView(
                "32",
                "选择并提交带电点",
                "charge",
                False,
                "当前没有已检查且可提交的 PREPARED 带电点",
            ),
        ),
    )
    lines = render_menu(view, theme=TerminalTheme(True), width=80)
    text = "\n".join(lines)

    assert "\x1b[" in text
    assert all(display_width(line) <= 80 for line in lines)
    assert "PREPARED" in text
    assert "\x1b[2m" in text
