from __future__ import annotations


class FakeStream:
    def __init__(self, tty: bool):
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


def test_theme_enables_ansi_only_for_supported_tty() -> None:
    from vaspsolkit.terminal_display import TerminalTheme

    enabled = TerminalTheme.detect(
        stream=FakeStream(True), environ={"TERM": "xterm-256color"}
    )
    assert enabled.enabled
    assert enabled.action("01") == "\x1b[36m01\x1b[0m"
    assert enabled.success("CONVERGED") == "\x1b[32mCONVERGED\x1b[0m"

    assert not TerminalTheme.detect(stream=FakeStream(False), environ={}).enabled
    assert not TerminalTheme.detect(
        stream=FakeStream(True), environ={"NO_COLOR": "1", "TERM": "xterm"}
    ).enabled
    assert not TerminalTheme.detect(
        stream=FakeStream(True), environ={"TERM": "dumb"}
    ).enabled


def test_disabled_theme_preserves_plain_text() -> None:
    from vaspsolkit.terminal_display import TerminalTheme

    theme = TerminalTheme(enabled=False)
    assert theme.action("01") == "01"
    assert theme.recommended("推荐") == "推荐"
    assert theme.success("CONVERGED") == "CONVERGED"
    assert theme.error("FAILED") == "FAILED"
    assert theme.muted("不可用") == "不可用"
    assert theme.strong("Case") == "Case"


def test_display_width_handles_ascii_cjk_combining_and_ansi() -> None:
    from vaspsolkit.terminal_display import display_width

    assert display_width("Case") == 4
    assert display_width("带电点") == 6
    assert display_width("e\u0301") == 1
    assert display_width("\x1b[36m带电点\x1b[0m") == 6


def test_pad_and_wrap_respect_terminal_columns() -> None:
    from vaspsolkit.terminal_display import display_width, pad_display, wrap_display

    assert pad_display("带电点", 10) == "带电点    "
    lines = wrap_display(
        "不可用：当前没有可取消的排队带电任务",
        20,
        subsequent_indent="    ",
    )
    assert len(lines) >= 2
    assert all(display_width(line) <= 20 for line in lines)
    assert lines[1].startswith("    ")
