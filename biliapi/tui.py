"""交互式 TUI 模块。

使用 questionary 提供箭头键导航的终端交互界面。
"""

import os
import sys

import questionary
from questionary import Style

from .auth import get_credentials, force_relogin
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


def _clear() -> None:
    """清屏。"""
    os.system("cls" if sys.platform == "win32" else "clear")

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
                questionary.Choice("🔑 重新登录", value="relogin"),
                questionary.Choice("🚪 退出", value="quit"),
            ],
            style=_STYLE,
        ).ask()

        if choice is None or choice == "quit":
            print("再见~")
            break

        if choice == "relogin":
            _clear()
            result = force_relogin()
            if result:
                credential = result
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
    ep_choices.append(questionary.Choice("← 返回追番列表", value=_BACK))

    choice = questionary.select(
        "选择剧集查看详情",
        choices=ep_choices,
        use_indicator=True,
        style=_STYLE,
    ).ask()

    if _is_back(choice):
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
    """分页显示收藏夹内容。"""
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

    has_more = data.get("has_more", False)
    info = data.get("info", {})
    total = info.get("media_count", 0)
    total_pages = max(1, (total + 19) // 20)  # API 每页固定20

    page_choices = []
    if page > 1:
        page_choices.append(questionary.Choice("◀ 上一页", value=_PREV))
    if has_more or page < total_pages:
        page_choices.append(questionary.Choice("▶ 下一页", value=_NEXT))
    page_choices.append(_SEP)
    page_choices.append(questionary.Choice("换一个收藏夹", value=_BACK))

    action = questionary.select(
        f"第 {page} 页",
        choices=page_choices,
        style=_STYLE,
    ).ask()

    if _is_back(action):
        return _favorites_flow(credential)
    elif action == _NEXT:
        return _favorite_content_pages(credential, media_id, page + 1)
    elif action == _PREV:
        return _favorite_content_pages(credential, media_id, page - 1)


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
        all_or_pick = questionary.select(
            f"共 {len(pages)} 集，如何下载？",
            choices=[
                questionary.Choice("全部下载", value="all"),
                questionary.Choice("选择下载", value="pick"),
            ],
            style=_STYLE,
        ).ask()

        if all_or_pick is None:
            return _download_flow(credential)

        if all_or_pick == "all":
            selected_indices = list(range(len(pages)))
        else:
            page_choices = []
            for i, p in enumerate(pages):
                page_choices.append(
                    questionary.Choice(
                        f"[{i+1}] {p.get('title', '-')}  ({p.get('duration', '-')})",
                        value=i,
                    )
                )
            selected = questionary.checkbox(
                "勾选要下载的分集（Space 勾选，Enter 确认）：",
                choices=page_choices,
                style=_STYLE,
            ).ask()

            if selected is None or len(selected) == 0:
                return _download_flow(credential)

            selected_indices = sorted(selected)
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
    import shutil
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

    # 6. 开始下载
    for idx in selected_indices:
        page = pages[idx]
        page_title = page.get("title", "")
        print()
        if multi_page:
            print(f"正在下载 [{idx+1}/{len(pages)}] {page_title} ...")
        else:
            print("正在下载...")

        try:
            out_path = download_video(
                credential=credential,
                bvid=bvid,
                aid=aid,
                page_index=idx,
                quality_v=qv,
                quality_a=qa,
                ffmpeg_path=ffmpeg,
            )
            print(f"✓ 下载完成: {out_path}")
        except Exception as e:
            print(f"✗ 下载失败: {e}")

    print()
    questionary.press_any_key_to_continue("按任意键返回主菜单...", style=_STYLE).ask()
