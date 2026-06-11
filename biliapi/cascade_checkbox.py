"""级联复选框：勾选"全选"自动勾选/取消所有子项，支持滚动。"""

import shutil
from typing import List
from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style


def _term_height() -> int:
    try:
        return shutil.get_terminal_size().lines
    except Exception:
        return 24


def cascade_checkbox(choices: List[str], instruction: str = "",
                     max_visible: int = 0) -> List[int]:
    """级联复选框。

    返回选中的 choices 索引列表（0-based）。
    全选勾选 → 自动全选子项；取消全选 → 自动取消全部。
    手动全选子项 → 自动勾选全选；取消任一 → 自动取消全选。
    选项超屏时自动滚动。
    """
    if not choices:
        return []

    n = len(choices)
    checked = [False] * (n + 1)  # [0]=全选, [1:]=子项
    cursor = [0]  # 当前高亮行 (0=全选, 1..n=子项)
    scroll = [0]  # 第一个可见子项索引

    # 可见窗口大小
    if max_visible <= 0:
        max_visible = max(5, _term_height() - 8)
    win_size = max_visible

    def _toggle(idx: int) -> None:
        if idx == 0:
            new_state = not checked[0]
            for i in range(n + 1):
                checked[i] = new_state
        else:
            checked[idx] = not checked[idx]
            checked[0] = all(checked[1:])

    def _scroll_to_cursor() -> None:
        """确保光标在可见窗口内。"""
        if cursor[0] == 0:
            return  # 全选始终可见
        cur = cursor[0]  # 1-based 子项索引
        # 可见范围: scroll+1 .. scroll+win_size
        vis_start = scroll[0] + 1
        vis_end = scroll[0] + win_size
        if cur < vis_start:
            scroll[0] = cur - 1
        elif cur > vis_end:
            scroll[0] = cur - win_size
        # 边界钳制
        if scroll[0] < 0:
            scroll[0] = 0
        max_scroll = max(0, n - win_size)
        if scroll[0] > max_scroll:
            scroll[0] = max_scroll

    submit_hover = [False]

    def _render() -> list:
        lines = []
        if instruction:
            lines.append(("class:info", f"  {instruction}\n"))
        lines.append(("class:info",
                      "  ↑↓ 移动  Enter 切换  Tab 跳到提交  Esc 取消\n\n"))

        # 全选：三态
        all_on = all(checked[1:])
        any_on = any(checked[1:])
        if all_on:
            mark = "☑"
        elif any_on:
            mark = "☒"
        else:
            mark = "☐"
        hl = "class:hl" if cursor[0] == 0 and not submit_hover[0] else ""
        pointer = ">" if cursor[0] == 0 and not submit_hover[0] else " "
        lines.append(("", f"  {pointer} "))
        lines.append((hl or "class:all", f"{mark} 全选\n"))

        # 上方省略提示
        if scroll[0] > 0:
            lines.append(("class:info", f"  │  ↑ 还有 {scroll[0]} 项\n"))

        # 可见子项窗口
        vis_start = scroll[0]
        vis_end = min(n, scroll[0] + win_size)
        for i in range(vis_start, vis_end):
            idx = i + 1  # checked 索引
            text = choices[i]
            mark = "☑" if checked[idx] else "☐"
            hl = "class:hl" if cursor[0] == idx and not submit_hover[0] else ""
            cur = ">" if cursor[0] == idx and not submit_hover[0] else " "
            lines.append(("class:tree", "  │"))
            lines.append(("", f"  {cur} "))
            lines.append((hl or "", f"{mark} {text}\n"))

        # 下方省略提示
        remaining = n - vis_end
        if remaining > 0:
            lines.append(("class:info", f"  │  ↓ 还有 {remaining} 项\n"))

        # 提交按钮
        lines.append(("", "\n"))
        btn_hl = "class:btn-hl" if submit_hover[0] else "class:btn"
        btn_prefix = ">" if submit_hover[0] else " "
        lines.append(("", f"  {btn_prefix} "))
        lines.append((btn_hl, "══ 提交 ══"))

        return lines

    kb = KeyBindings()

    @kb.add("up")
    def _(event):
        submit_hover[0] = False
        if cursor[0] == 0:
            # 从全选向上 → 跳到最后一个可见子项
            vis_end = min(n, scroll[0] + win_size)
            cursor[0] = vis_end
            _scroll_to_cursor()
        elif cursor[0] == 1:
            if scroll[0] > 0:
                scroll[0] -= 1
                cursor[0] = scroll[0] + 1
            else:
                cursor[0] = 0
        else:
            cursor[0] -= 1
            _scroll_to_cursor()

    @kb.add("down")
    def _(event):
        if cursor[0] == 0:
            submit_hover[0] = False
            cursor[0] = scroll[0] + 1 if n > 0 else 0
            if cursor[0] > n:
                cursor[0] = 0
            return
        if cursor[0] < n:
            cursor[0] += 1
            _scroll_to_cursor()
            submit_hover[0] = False
        elif cursor[0] == n:
            cursor[0] = 0
            submit_hover[0] = False

    @kb.add("tab")
    def _(event):
        if submit_hover[0]:
            submit_hover[0] = False
            cursor[0] = 0
        else:
            cursor[0] = 0
            submit_hover[0] = True

    @kb.add("enter")
    def _(event):
        if submit_hover[0]:
            event.app.exit(result=[i for i in range(n) if checked[i + 1]])
        else:
            _toggle(cursor[0])

    @kb.add("escape")
    def _(event):
        event.app.exit(result=None)

    @kb.add("c-c")
    def _(event):
        event.app.exit(result=None)

    content = FormattedTextControl(text=_render, focusable=True)

    app = Application(
        layout=Layout(HSplit([Window(content)])),
        key_bindings=kb,
        style=Style.from_dict({
            "hl": "bg:#FB7299 #ffffff bold",
            "all": "#FB7299 bold",
            "info": "#aaaaaa",
            "tree": "#888888",
            "btn": "bg:#666666 #ffffff bold",
            "btn-hl": "bg:#FB7299 #ffffff bold",
        }),
        full_screen=False,
    )

    return app.run()
