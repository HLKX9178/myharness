"""`ui.StreamPrinter`：边收边打的显示层。

它只管观感，不该影响内容。这里钉住三件事：
1. 每条 assistant 消息各带一次前缀（否则正文会接在工具日志后面）；
2. 一条消息只输出一次前缀，不是每片一次；
3. 没说过话的回合不留下一个空的 `BOT >>`。
"""

import re

from myharness.ui import StreamPrinter

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    return ANSI.sub("", text)


def test_prefix_is_printed_once_per_message(capsys):
    printer = StreamPrinter()

    printer.begin_message()
    for piece in ["你", "好"]:
        printer(piece)
    printer.end_message()

    out = _plain(capsys.readouterr().out)
    assert out.count("BOT >>") == 1
    assert "BOT >> 你好\n" in out


def test_each_assistant_message_gets_its_own_prefix(capsys):
    """多条消息（中间夹着工具调用）各起一行，不接在上一行后面。"""
    printer = StreamPrinter()

    for text in ["先看一下目录。", "目录里有三个文件。"]:
        printer.begin_message()
        printer(text)
        printer.end_message()

    out = _plain(capsys.readouterr().out)
    assert out.count("BOT >>") == 2
    assert "BOT >> 先看一下目录。\n" in out
    assert "BOT >> 目录里有三个文件。\n" in out


def test_silent_turn_prints_nothing(capsys):
    """模型整轮只调工具：不该出现一个空的 BOT >>。"""
    printer = StreamPrinter()
    printer.begin_message()
    printer.end_message()

    assert capsys.readouterr().out == ""
    assert printer.started is False


def test_end_message_without_text_does_not_add_blank_line(capsys):
    """工具型回合结束后，下一条消息的前缀紧跟着走，不多一个空行。"""
    printer = StreamPrinter()

    printer.begin_message()
    printer.end_message()  # 这条消息没有文本

    printer.begin_message()
    printer("有文本了")
    printer.end_message()

    out = _plain(capsys.readouterr().out)
    assert out.count("\n") == 1
    assert out.startswith("BOT >> 有文本了")


def test_empty_text_is_ignored(capsys):
    printer = StreamPrinter()
    printer.begin_message()
    printer("")
    printer.end_message()

    assert capsys.readouterr().out == ""
    assert printer.started is False
