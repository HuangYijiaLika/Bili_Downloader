"""交互式 TUI 模块。

使用 questionary 提供箭头键导航的终端交互界面。
"""

import glob as _glob
import os
import re as _re
import shutil
import sys
import threading
import time
import unicodedata

import questionary
from questionary import Style

from .auth import get_credentials, force_relogin
from .config import get_download_dir, get_temp_dir, load_config, save_config, set_, _invalidate_cache
from .cascade_checkbox import cascade_checkbox
from .commands import (
    get_bangumi_list,
    format_bangumi_list,
    get_bangumi_detail,
    format_bangumi_detail,
    get_episode_detail,
    format_episode_detail,
    get_favorite_lists,
    format_favorite_lists,
    get_favorite_content,
    format_favorite_content,
    format_favorite_video_detail,
    parse_video_id,
    get_video_info,
    format_video_info,
    download_video,
)


# questionary.Choice(value=None) 的行为：None 会被忽略，实际返回 title 文本。
# 因此用 __BACK__ 作为"返回"的标记值。
_BACK = "__BACK__"
_NEXT = "__NEXT__"
_PREV = "__PREV__"


def _swidth(s: str) -> int:
    """计算字符串的终端显示宽度（CJK 字符占 2 格）。"""
    w = 0
    for ch in s:
        w += 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
    return w


def _cut(s: str, max_width: int) -> str:
    """按显示宽度截断字符串，超出部分用 '...' 替代。"""
    if _swidth(s) <= max_width:
        return s
    ellipsis = "..."
    target = max_width - _swidth(ellipsis)
    if target <= 0:
        return ellipsis
    result = []
    w = 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
        if w + cw > target:
            break
        result.append(ch)
        w += cw
    return "".join(result) + ellipsis


def _clear() -> None:
    """清屏。"""
    os.system("cls" if sys.platform == "win32" else "clear")


def _term_height() -> int:
    """获取终端高度，失败返回默认值。"""
    try:
        return shutil.get_terminal_size().lines
    except Exception:
        return 24


def _load_dl_records(subdir: str = "") -> set:
    """读取下载记录文件。"""
    out_dir = get_download_dir()
    if subdir:
        out_dir = os.path.join(out_dir,
                               _re.sub(r'[\\/:*?"<>|]', '_', subdir).strip())
    record_path = os.path.join(out_dir, ".biliadl")
    records = set()
    if os.path.exists(record_path):
        try:
            with open(record_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        records.add(line)
        except Exception:
            pass
    return records


def _append_record(subdir: str, aid: str, page: int) -> None:
    """追加一条下载记录（去重）。"""
    out_dir = get_download_dir()
    if subdir:
        out_dir = os.path.join(out_dir,
                               _re.sub(r'[\\/:*?"<>|]', '_', subdir).strip())
    record_path = os.path.join(out_dir, ".biliadl")
    entry = f"{aid}_{page}\n"
    # 检查是否已存在
    if os.path.exists(record_path):
        try:
            with open(record_path, "r", encoding="utf-8") as f:
                if entry in f.read():
                    return
        except Exception:
            pass
    # 追加
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(record_path, "a", encoding="utf-8") as f:
            f.write(entry)
    except OSError:
        pass


def _safe_max_workers() -> int:
    """获取安全的并行下载数，不超过终端可显示范围。"""
    configured = int(load_config().get("max_parallel", "3"))
    term_h = _term_height()
    # 为标题、汇总行等留 8 行
    max_visible = max(1, term_h - 8)
    return min(configured, max_visible)


# 自定义样式（B站粉配色）
_STYLE = Style([
    ("qmark", "fg:#FB7299 bold"),
    ("question", "bold"),
    ("answer", "fg:#FB7299 bold"),
    ("pointer", "fg:#FB7299 bold"),
    ("highlighted", "fg:#FB7299 bold"),
    ("selected", ""),
    ("separator", "fg:#666666"),
    ("instruction", "fg:#aaaaaa"),
    ("text", ""),
])

_SEP = questionary.Separator("-" * 30)

# 每页数量
_PAGE_SIZE = 12


def _is_back(value) -> bool:
    """判断选项是否为返回键。兼容多种可能的值。"""
    if value is None:
        return True
    if value == _BACK:
        return True
    if isinstance(value, str) and "返回" in value:
        return True
    return False


# ========== 并行下载进度条 ==========

class _MultiBar:
    """多行进度条，用于并行下载时原地刷新每行。

    已完成行在上方，进行中行在下方。
    总行数不超过终端高度 - 10。
    """

    # 终端编码安全的进度条字符
    try:
        "█".encode(sys.stdout.encoding or "utf-8")
        _BAR_FILL = "█"
        _BAR_EMPTY = "░"
    except (UnicodeEncodeError, LookupError):
        _BAR_FILL = "#"
        _BAR_EMPTY = "-"

    def __init__(self, max_slots: int):
        self._lock = threading.Lock()
        self._slots = {}  # slot -> (label, done, total)
        self._finished = []  # [(slot, text), ...] FIFO
        self._lines = 0

    def callback(self, slot: int, label: str):
        """为指定 slot 创建一个 progress_callback。"""
        def _cb(_lbl, done, total):
            self.update(slot, label, done, total)
        return _cb

    def update(self, slot: int, label: str, done: int, total: int):
        with self._lock:
            self._slots[slot] = (label, done, total)
            self._render()

    def finish(self, slot: int, text: str):
        with self._lock:
            self._slots.pop(slot, None)
            self._finished.append(text)
            self._render()

    def _render(self):
        if self._lines:
            sys.stdout.write(f"\033[{self._lines}A")

        term_h = _term_height()
        max_lines = max(3, term_h - 10)

        # 进行中的行
        active_lines = []
        for slot in sorted(self._slots.keys()):
            label, done, total = self._slots[slot]
            if total:
                pct = min(done / total, 1.0) if total > 0 else 0
                fw = 15
                filled = int(fw * pct)
                bar = (self._BAR_FILL * filled) + (self._BAR_EMPTY * (fw - filled))
                done_s = _fmt_bytes(done)
                total_s = _fmt_bytes(total)
                label_short = _cut(label, 50)
                active_lines.append(
                    f"\033[K  {label_short:50s} [{bar}] {pct*100:3.0f}% ({done_s}/{total_s})"
                )

        # 已完成行 + 进行中行，总行数不超过 max_lines
        total_avail = len(self._finished) + len(active_lines)
        if total_avail > max_lines:
            trim = total_avail - max_lines
            # 优先保留进行中行，从已完成行头部裁剪
            self._finished = self._finished[trim:]

        new_lines = [f"\033[K{t}" for t in self._finished] + active_lines

        try:
            for line in new_lines:
                sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except UnicodeEncodeError:
            for line in new_lines:
                sys.stdout.write(line.encode(sys.stdout.encoding or "utf-8",
                                             errors="replace").decode(
                                                 sys.stdout.encoding or "utf-8",
                                                 errors="replace") + "\n")
            sys.stdout.flush()
        self._lines = len(new_lines)

    def clear(self):
        if self._lines:
            sys.stdout.write(f"\033[{self._lines}A")
            for _ in range(self._lines):
                sys.stdout.write("\033[K\n")
            sys.stdout.write(f"\033[{self._lines}A")
            sys.stdout.flush()
            self._lines = 0


def _fmt_bytes(n: int) -> str:
    """字节大小格式化。"""
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f}M"
    elif n >= 1024:
        return f"{n / 1024:.0f}K"
    return f"{n}B"


def main_loop() -> None:
    """主交互循环。"""
    _clear()
    credential = get_credentials()
    if credential is None:
        print("未配置凭证，程序退出。")
        return

    while True:
        choice = questionary.select(
            "你想做什么？",
            choices=[
                questionary.Choice("📺 追番列表", value="bangumi"),
                questionary.Choice("⭐ 收藏夹", value="favorites"),
                questionary.Choice("📥 下载视频", value="download"),
                _SEP,
                questionary.Choice("⚙️ 设置", value="settings"),
                questionary.Choice("🚪 退出", value="quit"),
            ],
            style=_STYLE,
        ).ask()

        if choice is None or choice == "quit":
            print("再见~")
            break

        if choice == "settings":
            _settings_flow(credential)
            _clear()
            continue

        if choice == "bangumi":
            _bangumi_flow(credential)
            _clear()

        elif choice == "favorites":
            _favorites_flow(credential)
            _clear()

        elif choice == "download":
            _download_flow(credential)
            _clear()


# ========== 设置 ==========

def _settings_flow(credential) -> None:
    """设置界面：修改下载/临时目录、重新登录。"""
    while True:
        _clear()
        cfg = load_config()
        dl = cfg.get("download_dir", "./download")
        tmp = cfg.get("temp_dir", "./temp")
        mp = cfg.get("max_parallel", "3")

        choice = questionary.select(
            "⚙️ 设置",
            choices=[
                questionary.Choice(f"📁 下载目录: {dl}", value="download_dir"),
                questionary.Choice(f"🗂️ 临时目录: {tmp}", value="temp_dir"),
                questionary.Choice(f"🔀 最大并行下载数: {mp}", value="max_parallel"),
                _SEP,
                questionary.Choice("🔑 重新登录", value="relogin"),
                _SEP,
                questionary.Choice("← 返回主菜单", value=_BACK),
            ],
            style=_STYLE,
        ).ask()

        if _is_back(choice):
            return

        if choice == "download_dir" or choice == "temp_dir":
            label = "下载目录" if choice == "download_dir" else "临时目录"
            cur = dl if choice == "download_dir" else tmp
            new_val = questionary.text(
                f"请输入新的{label}（当前: {cur}）：",
                default=cur,
                style=_STYLE,
            ).ask()
            if new_val is not None and new_val.strip():
                set_(choice, new_val.strip())
                _invalidate_cache()
                # 确保目录存在
                try:
                    os.makedirs(get_download_dir() if choice == "download_dir"
                                else get_temp_dir(), exist_ok=True)
                except OSError:
                    pass
            continue

        if choice == "max_parallel":
            new_val = questionary.text(
                f"请输入最大并行下载数（当前: {mp}，范围 1-8）：",
                default=mp,
                style=_STYLE,
            ).ask()
            if new_val is not None and new_val.strip():
                try:
                    v = int(new_val.strip())
                    if 1 <= v <= 8:
                        set_("max_parallel", str(v))
                        _invalidate_cache()
                    else:
                        print("请输入 1-8 之间的数字。")
                        questionary.press_any_key_to_continue("按任意键继续...", style=_STYLE).ask()
                except ValueError:
                    print("请输入有效数字。")
                    questionary.press_any_key_to_continue("按任意键继续...", style=_STYLE).ask()
            continue

        if choice == "relogin":
            _clear()
            result = force_relogin()
            if result:
                # 更新 credential 引用（通过可变容器）
                nonlocal_cred = result
                # 用新的 credential 替换旧的字段
                credential.sessdata = nonlocal_cred.sessdata
                credential.bili_jct = nonlocal_cred.bili_jct
                credential.buvid3 = nonlocal_cred.buvid3
                credential.dedeuserid = nonlocal_cred.dedeuserid
                print("✓ 重新登录成功")
            questionary.press_any_key_to_continue("按任意键继续...", style=_STYLE).ask()
            continue


# ========== 追番流程 ==========

def _bangumi_flow(credential) -> None:
    """追番列表交互流程（带翻页）。"""
    # 第一步：选类型
    type_ = questionary.select(
        "番剧类型",
        choices=[
            questionary.Choice("番剧", value="BANGUMI"),
            questionary.Choice("电视剧 / 纪录片", value="DRAMA"),
            _SEP,
            questionary.Choice("← 返回主菜单", value=_BACK),
        ],
        style=_STYLE,
    ).ask()

    if _is_back(type_):
        return

    # 第二步：选状态
    status = questionary.select(
        "追番状态",
        choices=[
            questionary.Choice("全部", value="ALL"),
            questionary.Choice("想看", value="WANT"),
            questionary.Choice("在看", value="WATCHING"),
            questionary.Choice("已看", value="WATCHED"),
            _SEP,
            questionary.Choice("← 返回上一步", value=_BACK),
        ],
        style=_STYLE,
    ).ask()

    if _is_back(status):
        return _bangumi_flow(credential)

    # 第三步：分页浏览
    _bangumi_pages(credential, type_, status, page=1)


def _bangumi_pages(credential, type_: str, status: str, page: int) -> None:
    """分页显示追番列表，可选中番剧查看详情。"""
    _clear()
    print(f"查询中（类型={type_}, 状态={status}, 第{page}页）...")
    try:
        data = get_bangumi_list(
            credential=credential,
            type_=type_,
            status=status,
            page=page,
            page_size=_PAGE_SIZE,
        )
    except Exception as e:
        print(f"查询失败: {e}")
        questionary.press_any_key_to_continue("按任意键返回...", style=_STYLE).ask()
        return

    items = data.get("list", [])

    print()
    print(format_bangumi_list(data))

    total = data.get("total", 0)
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

    # 构建选项：选中番剧 + 翻页 + 返回
    page_choices = []
    for i, item in enumerate(items, 1):
        title = item.get("title", "未知")
        page_choices.append(
            questionary.Choice(f"[{i}] {title}", value=("bangumi", i))
        )
    page_choices.append(_SEP)
    if page > 1:
        page_choices.append(questionary.Choice("◀ 上一页", value=_PREV))
    if page < total_pages:
        page_choices.append(questionary.Choice("▶ 下一页", value=_NEXT))
    page_choices.append(questionary.Choice("← 返回主菜单", value=_BACK))

    action = questionary.select(
        f"第 {page}/{total_pages} 页 — 选中番剧查看详情",
        choices=page_choices,
        style=_STYLE,
    ).ask()

    if _is_back(action):
        return
    elif action == _NEXT:
        return _bangumi_pages(credential, type_, status, page + 1)
    elif action == _PREV:
        return _bangumi_pages(credential, type_, status, page - 1)
    elif isinstance(action, tuple) and action[0] == "bangumi":
        idx = action[1] - 1
        if 0 <= idx < len(items):
            item = items[idx]
            _bangumi_detail_view(credential, item, type_, status, page)
            # 从详情返回后重新渲染当前列表页
            return _bangumi_pages(credential, type_, status, page)


def _bangumi_detail_view(credential, item: dict, type_: str, status: str, list_page: int) -> None:
    """显示番剧详情 + 剧集列表，可选中剧集查看详情。"""
    _clear()
    title = item.get("title", "未知")
    media_id = item.get("media_id", 0)
    season_id = item.get("season_id", 0)

    print(f"查询中（{title}）...")
    try:
        detail = get_bangumi_detail(credential, media_id=media_id, season_id=season_id)
    except Exception as e:
        print(f"查询详情失败: {e}")
        print()
        questionary.select(
            "", choices=[questionary.Choice("← 返回追番列表", value=_BACK)], style=_STYLE,
        ).ask()
        return

    detail["title"] = title
    print()
    print(format_bangumi_detail(detail, number=0))
    eps = detail.get("episodes", [])

    # 构建剧集选择（无论有无剧集，都提供返回选项）
    ep_choices = []
    for i, ep in enumerate(eps, 1):
        ep_title = ep.get("title", "-")
        ep_choices.append(
            questionary.Choice(f"[{i}] {ep_title}", value=i)
        )
    if not ep_choices:
        ep_choices.append(questionary.Choice("（无剧集信息）", value=0, disabled=True))
    ep_choices.append(_SEP)
    ep_choices.append(questionary.Choice("📥 批量下载剧集", value="download"))
    ep_choices.append(questionary.Choice("← 返回追番列表", value=_BACK))

    choice = questionary.select(
        "选择剧集查看详情",
        choices=ep_choices,
        use_indicator=True,
        style=_STYLE,
    ).ask()

    if _is_back(choice):
        return

    if choice == "download":
        _bangumi_batch_download(credential, eps, item, detail, type_, status, list_page)
        return

    if isinstance(choice, int) and choice >= 1 and choice <= len(eps):
        ep = eps[choice - 1]
        _episode_detail_view(credential, ep, item, detail, type_, status, list_page)


def _episode_detail_view(credential, ep: dict, item: dict, detail: dict,
                          type_: str, status: str, list_page: int) -> None:
    """显示单个剧集的详细信息。"""
    _clear()
    epid = ep.get("epid", 0)
    bangumi_title = item.get("title", "未知")

    print(f"查询中（epid={epid}）...")
    try:
        ep_detail = get_episode_detail(credential, epid=epid, bangumi_title=bangumi_title)
    except Exception as e:
        print(f"查询剧集详情失败: {e}")
        print()
        questionary.select(
            "", choices=[questionary.Choice("← 返回剧集列表", value=_BACK)], style=_STYLE,
        ).ask()
        return

    print()
    print(format_episode_detail(ep_detail))
    print()

    questionary.select(
        "", choices=[questionary.Choice("← 返回剧集列表", value=_BACK)], style=_STYLE,
    ).ask()

    # 回到番剧详情+剧集列表
    _bangumi_detail_view(credential, item, type_, status, list_page)


def _bangumi_batch_download(credential, eps: list, item: dict, detail: dict,
                              type_: str, status: str, list_page: int) -> None:
    """批量下载番剧剧集。"""
    _clear()
    bangumi_title = item.get("title", "未知")

    # 1. 级联复选框选剧集
    labels = [f"[{i+1}] {ep.get('title', '-')}  (BV: {ep.get('bvid', '-')})"
              for i, ep in enumerate(eps)]
    selected = cascade_checkbox(
        labels,
        instruction=f"《{bangumi_title}》共 {len(eps)} 集，勾选要下载的：",
    )
    if selected is None or len(selected) == 0:
        return _bangumi_detail_view(credential, item, type_, status, list_page)

    # 2. 选择清晰度
    quality_choice = questionary.select(
        "选择清晰度",
        choices=_QUALITY_CHOICES + [_SEP, questionary.Choice("← 返回", value=_BACK)],
        style=_STYLE,
    ).ask()
    if _is_back(quality_choice):
        return _bangumi_batch_download(credential, eps, item, detail, type_, status, list_page)
    qv, qa = quality_choice

    # 3. ffmpeg
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        ffmpeg = questionary.text("未检测到 ffmpeg，请手动输入路径：", style=_STYLE).ask()
        if not ffmpeg:
            return
        ffmpeg = ffmpeg.strip()

    # 4. 预处理：计算路径、处理重复文件（同步完成，避免 event loop 冲突）
    records = _load_dl_records(bangumi_title)
    total_sel = len(selected)
    tasks = []  # [(ep_idx, bvid, aid, basename, out_path, label), ...]
    dup_action = None  # None=首次询问, "skip_all"/"overwrite_all"=自动处理
    for count, idx in enumerate(selected, 1):
        ep = eps[idx]
        ep_title = ep.get("title", "-")
        bvid = ep.get("bvid", "")
        aid = ep.get("aid", "")

        if not bvid:
            print(f"⊙ [{count}/{total_sel}] {ep_title} — 无 BV 号，跳过")
            continue

        # 计算输出路径
        safe_title = _re.sub(r'[\\/:*?"<>|]', '_', bangumi_title).strip()
        safe_page = _re.sub(r'[\\/:*?"<>|]', '_', ep_title).strip()
        basename = f"{safe_title}_P{idx+1}_{safe_page}"
        out_path = os.path.join(get_download_dir(), f"{basename}.mp4")

        # 查重：先看记录，再看文件
        dup_reason = None  # "record" or "file"
        if aid and f"{aid}_0" in records:
            dup_reason = "record"
        elif os.path.exists(out_path):
            dup_reason = "file"
            # 补录缺失的记录
            _append_record(bangumi_title, aid, 0)

        if dup_reason:
            if dup_action is None:
                label = "已记录" if dup_reason == "record" else "已存在"
                choices = [
                    questionary.Choice("跳过", value="skip"),
                    questionary.Choice("覆盖", value="overwrite"),
                ]
                if dup_reason == "file":
                    choices.append(
                        questionary.Choice("重命名（_2, _3...）", value="rename"))
                choices.append(_SEP)
                choices.append(questionary.Choice("⚡全部跳过", value="skip_all"))
                choices.append(questionary.Choice("⚡全部覆盖", value="overwrite_all"))
                action = questionary.select(
                    f"[{count}/{total_sel}] {label}: {os.path.basename(out_path)}",
                    choices=choices,
                    style=_STYLE,
                ).ask()
                if action in ("skip_all", "overwrite_all"):
                    dup_action = action
                    action = "skip" if action == "skip_all" else "overwrite"
                if action == "skip":
                    print(f"⊙ [{count}/{total_sel}] {ep_title} — 已跳过")
                    continue
                elif action == "rename":
                    c = 2
                    while True:
                        new_base = f"{basename}_{c}"
                        new_path = os.path.join(get_download_dir(), f"{new_base}.mp4")
                        if not os.path.exists(new_path):
                            break
                        c += 1
                    basename = new_base
                    out_path = new_path
                # "overwrite": 直接覆盖
            elif dup_action == "skip_all":
                # 静默补录
                if os.path.exists(out_path) and aid:
                    _append_record(bangumi_title, aid, 0)
                print(f"⊙ [{count}/{total_sel}] {ep_title} — 已跳过（全部跳过）")
                continue
            elif dup_action == "overwrite_all":
                if os.path.exists(out_path) and aid:
                    _append_record(bangumi_title, aid, 0)
                pass  # 直接覆盖

        label = f"[{count}/{total_sel}] {ep_title}"
        tasks.append((idx, bvid, aid, basename, out_path, label))

    if not tasks:
        print("  没有需要下载的剧集。")
        print()
        questionary.press_any_key_to_continue("按任意键返回剧集列表...", style=_STYLE).ask()
        return _bangumi_detail_view(credential, item, type_, status, list_page)

    # 5. 并行下载
    max_workers = min(_safe_max_workers(), len(tasks))
    import concurrent.futures

    mbar = _MultiBar(len(tasks))
    results = {}

    def _do_one(slot, task):
        ep_idx, bvid, aid, basename, out_path, label = task
        cb = mbar.callback(slot, label)
        try:
            download_video(
                credential=credential,
                bvid=bvid,
                aid=aid,
                page_index=0,
                quality_v=qv,
                quality_a=qa,
                ffmpeg_path=ffmpeg,
                progress_callback=cb,
                subdir=bangumi_title,
            )
            mbar.finish(slot, f"✓ {label} → {os.path.basename(out_path)}")
            results[slot] = True
        except Exception as e:
            mbar.finish(slot, f"✗ {label} 失败: {e}")
            results[slot] = False

    print(f"\n  「{bangumi_title}」共 {len(tasks)} 集，开始并行下载（最多 {max_workers} 个同时）...\n")
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    futures = [ex.submit(_do_one, i, t) for i, t in enumerate(tasks)]
    try:
        while not all(f.done() for f in futures):
            time.sleep(0.5)
    except KeyboardInterrupt:
        # 取消尚未开始的任务，已开始的让它跑完（含写记录）
        for f in futures:
            f.cancel()
        print("\n  正在停止，等待已开始的下载完成...")
        try:
            while not all(f.done() for f in futures):
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            ex.shutdown(wait=True)
        mbar.clear()
        ok = sum(1 for v in results.values() if v)
        print(f"\n  已停止。完成: {ok}/{len(tasks)}")
        print()
        return
    ex.shutdown(wait=True)

    mbar.clear()
    ok = sum(1 for v in results.values() if v)
    print(f"  完成: {ok}/{len(tasks)}")
    print()
    questionary.press_any_key_to_continue("按任意键返回剧集列表...", style=_STYLE).ask()
    _bangumi_detail_view(credential, item, type_, status, list_page)


# ========== 收藏夹流程 ==========

def _favorites_flow(credential) -> None:
    """收藏夹交互流程：直接选择收藏夹查看内容（带翻页）。"""
    _clear()
    print("正在加载收藏夹...")
    try:
        items = get_favorite_lists(credential=credential)
    except Exception as e:
        print(f"查询失败: {e}")
        questionary.press_any_key_to_continue("按任意键返回...", style=_STYLE).ask()
        return

    if not items:
        print("（无视频收藏夹）")
        questionary.press_any_key_to_continue("按任意键返回...", style=_STYLE).ask()
        return

    # 直接构建选择菜单，不再额外打印表格
    choices = []
    for item in items:
        mid = item.get("id")
        title = item.get("title", "未知")[:28]
        count = item.get("media_count", 0)
        choices.append(
            questionary.Choice(
                f"[{mid}] {title}  ({count} 个视频)",
                value=mid,
            )
        )
    choices.append(_SEP)
    choices.append(questionary.Choice("← 返回主菜单", value=_BACK))

    media_id = questionary.select(
        "选择一个收藏夹查看内容",
        choices=choices,
        style=_STYLE,
    ).ask()

    if _is_back(media_id):
        return

    if not isinstance(media_id, int):
        try:
            media_id = int(media_id)
        except (ValueError, TypeError):
            print(f"无效的收藏夹 ID: {media_id}")
            return

    _favorite_content_pages(credential, media_id, page=1)


def _favorite_content_pages(credential, media_id: int, page: int) -> None:
    """分页显示收藏夹内容，可选中视频查看详情。"""
    _clear()
    print(f"查询中（收藏夹 {media_id}，第 {page} 页）...")
    try:
        data = get_favorite_content(credential=credential, media_id=media_id, page=page)
    except Exception as e:
        print(f"查询失败: {e}")
        questionary.press_any_key_to_continue("按任意键返回...", style=_STYLE).ask()
        return

    print()
    print(format_favorite_content(data))

    medias = data.get("medias", [])
    has_more = data.get("has_more", False)
    info = data.get("info", {})
    total = info.get("media_count", 0)
    total_pages = max(1, (total + 19) // 20)  # API 每页固定20

    content_choices = []
    for i, media in enumerate(medias, 1):
        item_title = media.get("title", "未知")
        content_choices.append(
            questionary.Choice(f"[{i}] {item_title[:40]}", value=("video", i))
        )
    content_choices.append(_SEP)
    if page > 1:
        content_choices.append(questionary.Choice("◀ 上一页", value=_PREV))
    if has_more or page < total_pages:
        content_choices.append(questionary.Choice("▶ 下一页", value=_NEXT))
    content_choices.append(_SEP)
    content_choices.append(questionary.Choice("📥 批量下载全部", value="batch_dl"))
    content_choices.append(questionary.Choice("换一个收藏夹", value=_BACK))

    action = questionary.select(
        f"第 {page}/{total_pages} 页 — 选中视频查看详情",
        choices=content_choices,
        style=_STYLE,
    ).ask()

    if _is_back(action):
        return _favorites_flow(credential)
    elif action == _NEXT:
        return _favorite_content_pages(credential, media_id, page + 1)
    elif action == _PREV:
        return _favorite_content_pages(credential, media_id, page - 1)
    elif action == "batch_dl":
        _favorite_batch_download(credential, media_id)
        return _favorite_content_pages(credential, media_id, page)
    elif isinstance(action, tuple) and action[0] == "video":
        idx = action[1] - 1
        if 0 <= idx < len(medias):
            media = medias[idx]
            _favorite_video_detail_view(credential, media, idx + 1, media_id, page)
            return _favorite_content_pages(credential, media_id, page)


def _favorite_video_detail_view(credential, media: dict, number: int,
                                 media_id: int, page: int) -> None:
    """显示单个收藏视频的详细信息。"""
    _clear()
    print()
    print(format_favorite_video_detail(media, number=number))
    print()

    questionary.select(
        "", choices=[questionary.Choice("← 返回收藏夹内容", value=_BACK)], style=_STYLE,
    ).ask()


def _favorite_batch_download(credential, media_id: int) -> None:
    """批量下载收藏夹中全部视频（含多分集）。"""
    import concurrent.futures
    import shutil

    _clear()

    # 1. 拉取所有页的视频列表
    print("正在拉取收藏夹全部视频...")
    all_medias = []
    fav_title = "未知"
    page = 1
    while True:
        try:
            data = get_favorite_content(credential=credential, media_id=media_id, page=page)
        except Exception as e:
            print(f"拉取第 {page} 页失败: {e}")
            break
        medias = data.get("medias", [])
        all_medias.extend(medias)
        if not fav_title or fav_title == "未知":
            fav_title = data.get("info", {}).get("title", "未知")
        if not data.get("has_more", False):
            break
        page += 1

    # 2. 构建视频列表（含 AV 号，用于记录查重）
    video_list = []  # [(bvid, aid, title, page_count)]
    for m in all_medias:
        bvid = m.get("bvid", "")
        if not bvid:
            continue
        aid = str(m.get("id", ""))
        title = m.get("title", "未知")
        page_count = m.get("page", 1)
        video_list.append((bvid, aid, title, page_count))

    if not video_list:
        print("  该收藏夹无视频。")
        questionary.press_any_key_to_continue("按任意键返回...", style=_STYLE).ask()
        return

    # 3. 级联复选框勾选视频
    labels = []
    for bvid, aid, title, pc in video_list:
        if pc > 1:
            labels.append(f"{title}  ({pc}P)")
        else:
            labels.append(title)
    selected = cascade_checkbox(
        labels,
        instruction=f"《{fav_title}》共 {len(video_list)} 个视频，勾选要下载的：",
    )
    if selected is None or len(selected) == 0:
        return

    # 3.5 检查输出目录是否已有文件
    out_dir_check = os.path.join(get_download_dir(),
                                 _re.sub(r'[\\/:*?"<>|]', '_', fav_title).strip())
    existing_count = len(_glob.glob(os.path.join(out_dir_check, "*.mp4")))
    if existing_count > 0:
        dir_check = questionary.select(
            f"输出目录「{os.path.basename(out_dir_check)}」已有 {existing_count} 个 mp4 文件",
            choices=[
                questionary.Choice("继续（重复文件会询问）", value="continue"),
                questionary.Choice("取消下载", value="cancel"),
            ],
            style=_STYLE,
        ).ask()
        if dir_check == "cancel":
            return

    # 4. 选择清晰度
    quality_choice = questionary.select(
        "选择清晰度",
        choices=_QUALITY_CHOICES + [_SEP, questionary.Choice("← 返回", value=_BACK)],
        style=_STYLE,
    ).ask()
    if _is_back(quality_choice):
        return _favorite_batch_download(credential, media_id)
    qv, qa = quality_choice

    # 5. ffmpeg
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        ffmpeg = questionary.text("未检测到 ffmpeg，请手动输入路径：", style=_STYLE).ask()
        if not ffmpeg:
            return
        ffmpeg = ffmpeg.strip()

    # 6. 构建下载任务（纯本地记录查重，瞬间完成）
    print("  正在检查已有文件...")
    records = _load_dl_records(fav_title)
    tasks = []  # [(bvid, page_index, label)]

    dup_action = None
    for idx in selected:
        bvid, aid, title, pc = video_list[idx]

        # 查记录（纯内存操作，无 API，无磁盘 IO）
        all_recorded = (pc > 0)
        for p in range(pc):
            if f"{aid}_{p}" not in records:
                all_recorded = False
                break

        if all_recorded:
            # 已有记录 → 重复，根据策略处理

            if dup_action == "skip_all":
                print(f"⊙ 「{title}」— 已跳过（全部跳过）")
                continue
            elif dup_action == "overwrite_all":
                pass  # 直接覆盖
            else:
                action = questionary.select(
                    f"「{title}」(已记录 {pc} 集)，如何处理？",
                    choices=[
                        questionary.Choice("跳过", value="skip"),
                        questionary.Choice("覆盖", value="overwrite"),
                        _SEP,
                        questionary.Choice("⚡全部覆盖", value="overwrite_all"),
                        questionary.Choice("⚡全部跳过", value="skip_all"),
                    ],
                    style=_STYLE,
                ).ask()
                if action in ("skip_all", "overwrite_all"):
                    dup_action = action
                    action = "skip" if action == "skip_all" else "overwrite"
                if action == "skip":
                    print(f"⊙ 「{title}」— 已跳过")
                    continue

        # 添加下载任务
        for p in range(pc):
            if pc > 1:
                label = f"{title} P{p+1}"
            else:
                label = title
            tasks.append((bvid, p, label))

    # 7. 并行下载
    if not tasks:
        print("  所有视频均已跳过，无需下载。")
        print()
        questionary.press_any_key_to_continue("按任意键返回...", style=_STYLE).ask()
        return

    max_workers = min(_safe_max_workers(), len(tasks))
    mbar = _MultiBar(len(tasks))
    results = {}

    def _do_one(slot, task):
        bvid, page_index, label = task
        cb = mbar.callback(slot, label)
        try:
            download_video(
                credential=credential,
                bvid=bvid,
                aid="",
                page_index=page_index,
                quality_v=qv,
                quality_a=qa,
                ffmpeg_path=ffmpeg,
                progress_callback=cb,
                subdir=fav_title,
            )
            mbar.finish(slot, f"✓ {label}")
            results[slot] = True
        except Exception as e:
            mbar.finish(slot, f"✗ {label} 失败: {e}")
            results[slot] = False

    print(f"\n  收藏夹「{fav_title}」共 {len(tasks)} 个分集，开始并行下载（最多 {max_workers} 个同时）...\n")
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    futures = [ex.submit(_do_one, i, t) for i, t in enumerate(tasks)]
    try:
        while not all(f.done() for f in futures):
            time.sleep(0.5)
    except KeyboardInterrupt:
        for f in futures:
            f.cancel()
        print("\n  正在停止，等待已开始的下载完成...")
        try:
            while not all(f.done() for f in futures):
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            ex.shutdown(wait=True)
        mbar.clear()
        ok = sum(1 for v in results.values() if v)
        print(f"\n  已停止。完成: {ok}/{len(tasks)}")
        print()
        return
    ex.shutdown(wait=True)

    mbar.clear()
    ok = sum(1 for v in results.values() if v)
    print(f"  完成: {ok}/{len(tasks)}")
    print()
    questionary.press_any_key_to_continue("按任意键返回...", style=_STYLE).ask()


# ========== 下载流程 ==========

from bilibili_api.video import VideoQuality, AudioQuality  # noqa: E402

_QUALITY_CHOICES = [
    questionary.Choice("360P",   value=(VideoQuality._360P, AudioQuality._64K)),
    questionary.Choice("480P",   value=(VideoQuality._480P, AudioQuality._132K)),
    questionary.Choice("720P",   value=(VideoQuality._720P, AudioQuality._192K)),
    questionary.Choice("1080P",  value=(VideoQuality._1080P, AudioQuality._192K)),
    questionary.Choice("1080P+", value=(VideoQuality._1080P_PLUS, AudioQuality._192K)),
    questionary.Choice("1080P60", value=(VideoQuality._1080P_60, AudioQuality._192K)),
    questionary.Choice("4K",     value=(VideoQuality._4K, AudioQuality._192K)),
]


def _download_flow(credential) -> None:
    """视频下载交互流程。"""
    _clear()

    # 1. 输入 BV/AV 号
    raw = questionary.text(
        "请输入 BV号 / AV号（如 BV1xx, av123, 纯数字）：",
        style=_STYLE,
    ).ask()

    if raw is None or not raw.strip():
        return

    id_type, vid = parse_video_id(raw.strip())
    if id_type is None:
        print("无效的 BV/AV 号，请重新输入。")
        questionary.press_any_key_to_continue("按任意键返回...", style=_STYLE).ask()
        return _download_flow(credential)

    # 2. 获取视频信息
    bvid = vid if id_type == "bv" else ""
    aid = vid if id_type == "av" else ""
    _clear()
    print("正在获取视频信息...")
    try:
        info = get_video_info(credential, bvid=bvid, aid=aid)
    except Exception as e:
        print(f"获取视频信息失败: {e}")
        questionary.press_any_key_to_continue("按任意键返回...", style=_STYLE).ask()
        return

    print()
    print(format_video_info(info))

    pages = info.get("pages", [])
    multi_page = len(pages) > 1

    # 3. 选择分集
    if multi_page:
        labels = [f"[{i+1}] {p.get('title', '-')}  ({p.get('duration', '-')})"
                  for i, p in enumerate(pages)]
        selected_indices = cascade_checkbox(
            labels,
            instruction=f"共 {len(pages)} 集，勾选要下载的分集：",
        )
        if selected_indices is None:
            return _download_flow(credential)
    else:
        selected_indices = [0]

    # 4. 选择清晰度
    quality_choice = questionary.select(
        "选择清晰度",
        choices=_QUALITY_CHOICES + [_SEP, questionary.Choice("← 返回", value=_BACK)],
        style=_STYLE,
    ).ask()

    if _is_back(quality_choice):
        return _download_flow(credential)

    qv, qa = quality_choice

    # 5. ffmpeg 路径（自动检测）
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        print(f"  已检测到 ffmpeg: {ffmpeg}")
    else:
        ffmpeg = questionary.text(
            "未检测到 ffmpeg，请手动输入路径：",
            style=_STYLE,
        ).ask()
        if ffmpeg is None:
            return
        ffmpeg = ffmpeg.strip()

    # 6. 预处理：计算路径、处理重复文件
    records = _load_dl_records()  # 直接下载，无子目录
    dl_aid = info.get("aid", aid)
    tasks = []  # [(page_index, basename, out_path, label), ...]
    dup_action = None
    for idx in selected_indices:
        page = pages[idx]
        page_title = page.get("title", "")

        safe_title = _re.sub(r'[\\/:*?"<>|]', '_', info["title"]).strip()
        safe_page = _re.sub(r'[\\/:*?"<>|]', '_', page_title).strip()
        if safe_page:
            basename = f"{safe_title}_P{idx+1}_{safe_page}"
        else:
            basename = safe_title
        out_path = os.path.join(get_download_dir(), f"{basename}.mp4")

        # 查重：先看记录，再看文件
        dup_reason = None
        if dl_aid and f"{dl_aid}_{idx}" in records:
            dup_reason = "record"
        elif os.path.exists(out_path):
            dup_reason = "file"
            # 补录缺失的记录
            _append_record("", dl_aid, idx)

        if dup_reason:
            if dup_action is None:
                label = "已记录" if dup_reason == "record" else "已存在同名文件"
                choices = [
                    questionary.Choice("跳过", value="skip"),
                    questionary.Choice("覆盖", value="overwrite"),
                ]
                if dup_reason == "file":
                    choices.append(
                        questionary.Choice("重命名（添加 _2, _3...）", value="rename"))
                choices.append(_SEP)
                choices.append(questionary.Choice("⚡全部跳过", value="skip_all"))
                choices.append(questionary.Choice("⚡全部覆盖", value="overwrite_all"))
                action = questionary.select(
                    f"{label}: {os.path.basename(out_path)}",
                    choices=choices,
                    style=_STYLE,
                ).ask()
                if action in ("skip_all", "overwrite_all"):
                    dup_action = action
                    action = "skip" if action == "skip_all" else "overwrite"
                if action == "skip":
                    print(f"⊙ {page_title or info['title']} — 已跳过")
                    continue
                elif action == "overwrite":
                    pass
                elif action == "rename":
                    c = 2
                    while True:
                        new_base = f"{basename}_{c}"
                        new_path = os.path.join(get_download_dir(), f"{new_base}.mp4")
                        if not os.path.exists(new_path):
                            break
                        c += 1
                    basename = new_base
                    out_path = new_path
            elif dup_action == "skip_all":
                if os.path.exists(out_path) and dl_aid:
                    _append_record("", dl_aid, idx)
                print(f"⊙ {page_title or info['title']} — 已跳过（全部跳过）")
                continue
            elif dup_action == "overwrite_all":
                if os.path.exists(out_path) and dl_aid:
                    _append_record("", dl_aid, idx)
                pass

        label = f"[{idx+1}] {page_title}" if multi_page else info["title"]
        tasks.append((idx, basename, out_path, label))

    if not tasks:
        print("  没有需要下载的分集。")
        print()
        questionary.press_any_key_to_continue("按任意键返回主菜单...", style=_STYLE).ask()
        return

    # 7. 并行下载
    max_workers = min(_safe_max_workers(), len(tasks))
    import concurrent.futures

    mbar = _MultiBar(len(tasks))
    results = {}

    def _do_one(slot, task):
        page_index, basename, out_path, label = task
        cb = mbar.callback(slot, label)
        try:
            download_video(
                credential=credential,
                bvid=bvid,
                aid=aid,
                page_index=page_index,
                quality_v=qv,
                quality_a=qa,
                ffmpeg_path=ffmpeg,
                progress_callback=cb,
            )
            mbar.finish(slot, f"✓ {label} → {os.path.basename(out_path)}")
            results[slot] = True
        except Exception as e:
            mbar.finish(slot, f"✗ {label} 失败: {e}")
            results[slot] = False

    if len(tasks) > 1:
        print(f"\n  并行下载（最多 {max_workers} 个同时）...\n")
    else:
        print()
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    futures = [ex.submit(_do_one, i, t) for i, t in enumerate(tasks)]
    try:
        while not all(f.done() for f in futures):
            time.sleep(0.5)
    except KeyboardInterrupt:
        for f in futures:
            f.cancel()
        print("\n  正在停止，等待已开始的下载完成...")
        try:
            while not all(f.done() for f in futures):
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            ex.shutdown(wait=True)
        mbar.clear()
        ok = sum(1 for v in results.values() if v)
        print(f"\n  已停止。完成: {ok}/{len(tasks)}")
        print()
        return
    ex.shutdown(wait=True)

    mbar.clear()
    ok = sum(1 for v in results.values() if v)
    if len(tasks) > 1:
        print(f"  完成: {ok}/{len(tasks)}")
    print()
    questionary.press_any_key_to_continue("按任意键返回主菜单...", style=_STYLE).ask()
