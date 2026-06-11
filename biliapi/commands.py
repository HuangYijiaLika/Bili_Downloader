"""命令实现模块。

提供追番列表查询和收藏夹查询的具体逻辑。
"""

import asyncio
import os
import shutil
import subprocess
import sys
import unicodedata
from typing import Optional, List, Dict, Any

from bilibili_api import user, favorite_list, Credential

from . import config
from bilibili_api.user import BangumiType, BangumiFollowStatus
from bilibili_api.favorite_list import FavoriteList, FavoriteListType, FavoriteListContentOrder


def _run_async(coro):
    """同步包装器：检测 event loop 状态，选择合适的方式运行协程。"""
    import concurrent.futures
    try:
        asyncio.get_running_loop()
        # 已有运行中的 loop，在独立线程中跑
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(asyncio.run, coro).result()
    except RuntimeError:
        # 没有运行中的 loop，直接跑
        return asyncio.run(coro)


# ============================================================
# 终端排版工具
# ============================================================

def _wcwidth(ch: str) -> int:
    """单字符显示宽度：中文等全角字符 = 2，ASCII = 1。"""
    ea = unicodedata.east_asian_width(ch)
    return 2 if ea in ("F", "W") else 1


def _swidth(s: str) -> int:
    """字符串在终端中的显示宽度。"""
    return sum(_wcwidth(c) for c in s)


def _cut(s: str, width: int) -> str:
    """按显示宽度截断，超出加 …"""
    if _swidth(s) <= width:
        return s
    target = width - 1  # 为 … 留 1 个 ASCII 宽度
    cur = 0
    result = []
    for c in s:
        cw = _wcwidth(c)
        if cur + cw > target:
            break
        result.append(c)
        cur += cw
    return "".join(result) + "…"


def _lpad(s: str, width: int) -> str:
    """按显示宽度左侧补空格到 width。"""
    need = width - _swidth(s)
    if need <= 0:
        return s
    return s + " " * need


# ============================================================
# 追番相关
# ============================================================

def get_bangumi_list(
    credential: Credential,
    type_: str = "BANGUMI",
    status: str = "ALL",
    page: int = 1,
    page_size: int = 15,
) -> dict:
    """获取用户的追番/追剧列表。

    Args:
        credential: 凭证
        type_: "BANGUMI" (番剧) 或 "DRAMA" (电视剧/纪录片)
        status: "ALL" / "WANT" / "WATCHING" / "WATCHED"
        page: 页码
        page_size: 每页数量
    """
    uid = int(credential.dedeuserid) if credential.dedeuserid else 0
    if uid == 0:
        raise ValueError("无法获取 UID，请检查凭证中的 DedeUserID 字段。")

    u = user.User(uid, credential=credential)

    # 映射参数
    type_map = {
        "BANGUMI": BangumiType.BANGUMI,
        "DRAMA": BangumiType.DRAMA,
    }
    status_map = {
        "ALL": BangumiFollowStatus.ALL,
        "WANT": BangumiFollowStatus.WANT,
        "WATCHING": BangumiFollowStatus.WATCHING,
        "WATCHED": BangumiFollowStatus.WATCHED,
    }

    bt = type_map.get(type_.upper(), BangumiType.BANGUMI)
    fs = status_map.get(status.upper(), BangumiFollowStatus.ALL)

    result = _run_async(u.get_subscribed_bangumi(
        pn=page,
        ps=page_size,
        type_=bt,
        follow_status=fs,
    ))
    return result


def format_bangumi_list(data: dict) -> str:
    """格式化追番列表为表格字符串。"""
    items = data.get("list", [])
    if not items:
        return "（无追番数据）"

    status_label = {1: "想看", 2: "在看", 3: "已看"}

    # 列宽（显示宽度）
    W_NUM = 3
    W_TITLE = 26
    W_STATUS = 4
    W_PROGRESS = 28
    W_FINISH = 4

    lines = []
    header = (f"  {_lpad('#', W_NUM)} {_lpad('标题', W_TITLE)} "
              f"{_lpad('状态', W_STATUS)} {_lpad('进度', W_PROGRESS)} "
              f"{_lpad('完结', W_FINISH)}")
    lines.append(header)
    lines.append("-" * _swidth(header))

    for i, item in enumerate(items, 1):
        title = _cut(item.get("title", "未知"), W_TITLE)
        fs = item.get("follow_status", 0)
        status_str = status_label.get(fs, "未知")
        progress = _cut(item.get("progress", "-") or "-", W_PROGRESS)
        is_finish = "是" if item.get("is_finish") == 1 else "否"

        lines.append(f"  {_lpad(str(i), W_NUM)} {_lpad(title, W_TITLE)} "
                     f"{_lpad(status_str, W_STATUS)} {_lpad(progress, W_PROGRESS)} "
                     f"{_lpad(is_finish, W_FINISH)}")

    lines.append("")
    total = data.get("total", len(items))
    lines.append(f"  共 {total} 部")
    return "\n".join(lines)


# ============================================================
# 番剧详情 & 剧集详情
# ============================================================

from bilibili_api import bangumi as _bangumi_mod


def get_bangumi_detail(credential: Credential, media_id: int, season_id: int) -> dict:
    """获取番剧详情：评分、数据统计、剧集列表。

    Returns:
        {
            'title': str,
            'score': dict | None,
            'stat': {...},
            'evaluate': str,
            'styles': list,
            'episodes': [{'epid': int, 'title': str, 'bvid': str, 'aid': str}, ...],
        }
    """
    b = _bangumi_mod.Bangumi(media_id=media_id, ssid=season_id, credential=credential)

    async def fetch():
        overview, stat = await asyncio.gather(b.get_overview(), b.get_stat())

        score_data = None
        evaluate = ""
        styles = []
        if isinstance(overview, dict):
            score_data = overview.get("rating") or overview.get("score")
            evaluate = overview.get("evaluate", "")
            styles = overview.get("styles", []) or []

        # 获取剧集列表（只用 get_episodes，不调 get_episode_info 避免 412）
        episodes = []
        try:
            eps = await b.get_episodes()
        except Exception:
            eps = []
        for i, ep in enumerate(eps):
            try:
                epid = ep.get_epid()
                bvid = await ep.get_bvid()
                aid = await ep.get_aid()
            except Exception:
                epid = 0
                bvid = ""
                aid = ""
                try:
                    epid = ep.get_epid()
                except Exception:
                    pass
            episodes.append({
                "epid": epid,
                "title": f"第{i+1}集",
                "bvid": str(bvid) if bvid else "",
                "aid": str(aid) if aid else "",
            })

        return {
            "title": "",
            "score": score_data,
            "stat": stat,
            "evaluate": evaluate,
            "styles": styles,
            "episodes": episodes,
        }

    result = _run_async(fetch())
    return result


def _fmt_duration(val) -> str:
    """格式化时长（秒 → h:mm:ss / m:ss）。"""
    if not val:
        return "-"
    try:
        n = int(val)
    except (ValueError, TypeError):
        return str(val)
    if n >= 3600:
        h, r = divmod(n, 3600)
        m, s = divmod(r, 60)
        return f"{h}:{m:02d}:{s:02d}"
    elif n >= 60:
        m, s = divmod(n, 60)
        return f"{m}:{s:02d}"
    else:
        return f"{n}s"


def format_bangumi_detail(data: dict, number: int = 1) -> str:
    """格式化单个番剧详情。"""
    lines = []
    title = data.get("title", "未知")
    lines.append(f"  {'='*50}")
    lines.append(f"  {title}")
    lines.append(f"  {'='*50}")

    score = data.get("score")
    if isinstance(score, dict):
        lines.append(f"  评分: {score.get('score', '-')}  ({score.get('count', 0)}人)")
    elif score:
        lines.append(f"  评分: {score}")

    styles = data.get("styles", [])
    if styles:
        lines.append(f"  风格: {', '.join(str(s) for s in styles)}")

    evaluate = data.get("evaluate", "")
    if evaluate:
        lines.append(f"  简介: {evaluate}")

    stat = data.get("stat", {})
    if stat:
        views = _fmt_num(stat.get("views", 0))
        follow = _fmt_num(stat.get("follow", 0))
        coins = _fmt_num(stat.get("coins", 0))
        danmaku = _fmt_num(stat.get("danmakus", 0))
        lines.append(f"  播放: {views}  |  追番: {follow}  |  硬币: {coins}  |  弹幕: {danmaku}")

    eps = data.get("episodes", [])
    lines.append("")
    lines.append(f"  剧集列表（共 {len(eps)} 集）：")
    lines.append("")

    W_NUM = 4
    W_TITLE = 14
    W_BVID = 16
    W_AID = 16

    header = (f"    {_lpad('#', W_NUM)} {_lpad('标题', W_TITLE)} "
              f"{_lpad('BV号', W_BVID)} {_lpad('AV号', W_AID)}")
    lines.append(header)
    lines.append("  " + "-" * (_swidth(header) - 2))

    for i, ep in enumerate(eps, 1):
        ep_title = _cut(ep.get("title", "-"), W_TITLE)
        bvid = str(ep.get("bvid", "-"))[:14]
        aid = str(ep.get("aid", "-"))[:14]

        lines.append(f"    {_lpad(str(i), W_NUM)} {_lpad(ep_title, W_TITLE)} "
                     f"{_lpad(bvid, W_BVID)} {_lpad(aid, W_AID)}")

    return "\n".join(lines)


def _fmt_num(n: int) -> str:
    """数字格式化（万）。"""
    if n >= 10000:
        return f"{n/10000:.1f}万"
    return str(n)


def get_episode_detail(credential: Credential, epid: int, bangumi_title: str = "") -> dict:
    """获取单个剧集的详细信息。

    Returns:
        {
            'epid': int,
            'title': str,
            'bvid': str,
            'aid': str,
            'cid': str,
            'bangumi_title': str,
        }
    """
    ep = _bangumi_mod.Episode(epid, credential=credential)

    async def fetch():
        bvid, aid, cid = await asyncio.gather(
            ep.get_bvid(), ep.get_aid(), ep.get_cid()
        )
        return {
            "epid": epid,
            "title": f"epid:{epid}",
            "bvid": str(bvid) if bvid else "-",
            "aid": str(aid) if aid else "-",
            "cid": str(cid) if cid else "-",
            "bangumi_title": bangumi_title,
        }

    return _run_async(fetch())


def format_episode_detail(data: dict) -> str:
    """格式化单个剧集的详情。"""
    lines = []
    lines.append(f"  ====== 剧集详情 ======")
    lines.append(f"  所属番剧: {data.get('bangumi_title', '-')}")
    lines.append(f"  EP ID:   {data.get('epid', '-')}")
    lines.append(f"  BV号:    {data.get('bvid', '-')}")
    lines.append(f"  AV号:    {data.get('aid', '-')}")
    lines.append(f"  CID:     {data.get('cid', '-')}")
    return "\n".join(lines)


# ============================================================
# 收藏夹相关
# ============================================================

def get_favorite_lists(
    credential: Credential,
    page: int = 1,
) -> list:
    """获取用户的视频收藏夹列表。

    Returns:
        收藏夹列表（list of dict）
    """
    uid = int(credential.dedeuserid) if credential.dedeuserid else 0
    if uid == 0:
        raise ValueError("无法获取 UID，请检查凭证中的 DedeUserID 字段。")

    # 获取视频收藏夹列表
    result = _run_async(favorite_list.get_video_favorite_list(
        uid=uid,
        credential=credential,
    ))
    return result.get("list", [])


def format_favorite_lists(items: list) -> str:
    """格式化收藏夹列表为表格字符串。"""
    if not items:
        return "（无视频收藏夹）"

    W_ID = 12
    W_TITLE = 24
    W_COUNT = 6
    W_PUB = 4

    lines = []
    header = (f"  {_lpad('ID', W_ID)} {_lpad('名称', W_TITLE)} "
              f"{_lpad('数量', W_COUNT)} {_lpad('公开', W_PUB)}")
    lines.append(header)
    lines.append("-" * _swidth(header))

    for item in items:
        mid = str(item.get("id", "-"))
        title = _cut(item.get("title", "未知"), W_TITLE)
        count = str(item.get("media_count", "-"))
        is_public = "是" if item.get("attr") == 0 or item.get("state", 0) == 0 else "否"

        lines.append(f"  {_lpad(mid, W_ID)} {_lpad(title, W_TITLE)} "
                     f"{_lpad(count, W_COUNT)} {_lpad(is_public, W_PUB)}")

    lines.append("")
    lines.append(f"  共 {len(items)} 个收藏夹")
    return "\n".join(lines)


def get_favorite_content(
    credential: Credential,
    media_id: int,
    page: int = 1,
    keyword: str = "",
    page_size: int = 20,
) -> dict:
    """获取指定收藏夹的内容。

    Args:
        credential: 凭证
        media_id: 收藏夹 ID
        page: 页码
        keyword: 搜索关键词
        page_size: 每页数量
    """
    fl = FavoriteList(
        type_=FavoriteListType.VIDEO,
        media_id=media_id,
        credential=credential,
    )

    result = _run_async(fl.get_content(
        page=page,
    ))
    return result


def format_favorite_content(data: dict) -> str:
    """格式化收藏夹内容为表格字符串。"""
    medias = data.get("medias", [])
    if not medias:
        return "（此收藏夹无内容）"

    lines = []
    info = data.get("info", {})
    title = info.get("title", "未知")
    lines.append(f"  收藏夹: {title}")
    lines.append(f"  共 {info.get('media_count', '?')} 个视频")
    lines.append("")

    W_NUM = 3
    W_TITLE = 34
    W_OWNER = 14
    W_DUR = 6

    header = (f"  {_lpad('#', W_NUM)} {_lpad('标题', W_TITLE)} "
              f"{_lpad('UP主', W_OWNER)} {_lpad('时长', W_DUR)}")
    lines.append(header)
    lines.append("-" * _swidth(header))

    for i, media in enumerate(medias, 1):
        item_title = _cut(media.get("title", "未知"), W_TITLE)
        owner = _cut(media.get("upper", {}).get("name", "-"), W_OWNER)
        duration = media.get("duration", "")
        if duration:
            m, s = divmod(int(duration), 60)
            duration_str = f"{m}:{s:02d}"
        else:
            duration_str = "-"

        lines.append(f"  {_lpad(str(i), W_NUM)} {_lpad(item_title, W_TITLE)} "
                     f"{_lpad(owner, W_OWNER)} {_lpad(duration_str, W_DUR)}")

    lines.append("")
    has_more = data.get("has_more", False)
    if has_more:
        lines.append("  （还有更多）")
    return "\n".join(lines)


import datetime as _dt


def _fmt_ts(ts) -> str:
    """Unix 时间戳 → 日期字符串。"""
    if not ts:
        return "-"
    try:
        return _dt.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError, OverflowError):
        return str(ts)


def format_favorite_video_detail(media: dict, number: int = 0) -> str:
    """格式化单个收藏视频的详细信息。"""
    lines = []
    title = media.get("title", "未知")
    lines.append(f"  {'='*50}")
    lines.append(f"  [{number}] {title}")
    lines.append(f"  {'='*50}")

    # 基本信息
    owner = media.get("upper", {})
    owner_name = owner.get("name", "-") if isinstance(owner, dict) else "-"
    bvid = media.get("bvid", "-")
    lines.append(f"  UP主:     {owner_name}")
    lines.append(f"  BV号:     {bvid}")

    # 时长
    duration = media.get("duration", "")
    if duration:
        lines.append(f"  时长:     {_fmt_duration(duration)}")
    else:
        lines.append(f"  时长:     -")

    # 分集数
    page = media.get("page", 1)
    if page > 1:
        lines.append(f"  分集:     {page} 集")

    # 时间相关
    fav_time = media.get("fav_time", 0)
    ctime = media.get("ctime", 0)
    pubtime = media.get("pubtime", 0)
    lines.append(f"  收藏时间: {_fmt_ts(fav_time)}")
    if ctime:
        lines.append(f"  创建时间: {_fmt_ts(ctime)}")
    if pubtime:
        lines.append(f"  发布时间: {_fmt_ts(pubtime)}")

    # 数据统计
    cnt = media.get("cnt_info", {}) or {}
    if cnt:
        play = _fmt_num(cnt.get("play", 0))
        collect = _fmt_num(cnt.get("collect", 0))
        danmaku = _fmt_num(cnt.get("danmaku", 0))
        lines.append(f"  播放: {play}  |  收藏: {collect}  |  弹幕: {danmaku}")

    # 简介
    intro = media.get("intro", "")
    if intro:
        lines.append(f"  简介: {intro}")

    return "\n".join(lines)


# ============================================================
# 视频下载
# ============================================================

import re as _re
from bilibili_api import video as _video_mod, get_client as _get_client, HEADERS as _HEADERS


def parse_video_id(raw: str) -> tuple:
    """解析用户输入的 BV/AV 号。

    Returns:
        (type: str, id: str) — type 为 'bv' 或 'av', id 为纯编号字符串
        解析失败返回 (None, None)
    """
    raw = raw.strip()
    if not raw:
        return None, None

    # BV 号: BV + 10位字母数字（大小写敏感！保留原始大小写）
    m = _re.match(r'^BV([A-Za-z0-9]{10})$', raw, _re.IGNORECASE)
    if m:
        return "bv", "BV" + m.group(1)

    # AV 号: av + 数字 / AV + 数字 / 纯数字
    m = _re.match(r'^(?:av)?(\d+)$', raw, _re.IGNORECASE)
    if m:
        return "av", m.group(1)

    return None, None


# 清晰度选项（名称 → VideoQuality 枚举）
_QUALITY_OPTIONS = [
    ("8K",     _video_mod.VideoQuality._8K),
    ("4K",     _video_mod.VideoQuality._4K),
    ("1080P60", _video_mod.VideoQuality._1080P_60),
    ("1080P+", _video_mod.VideoQuality._1080P_PLUS),
    ("1080P",  _video_mod.VideoQuality._1080P),
    ("720P",   _video_mod.VideoQuality._720P),
    ("480P",   _video_mod.VideoQuality._480P),
    ("360P",   _video_mod.VideoQuality._360P),
]

# 清晰度回退顺序（高→低）
_VIDEO_QUALITY_FALLBACK = [v for _, v in _QUALITY_OPTIONS]

_DEFAULT_FFMPEG = "ffmpeg"


def get_video_info(credential: Credential, bvid: str = "", aid: str = "") -> dict:
    """获取视频信息（标题、分集、封面等）。

    Returns:
        {'title': str, 'cover': str, 'duration': str, 'owner': str,
         'pages': [{'cid': int, 'title': str, 'duration': str}, ...],
         'bvid': str, 'aid': str}
    """
    kwargs = {"credential": credential}
    if bvid:
        kwargs["bvid"] = bvid
    elif aid:
        kwargs["aid"] = int(aid)
    v = _video_mod.Video(**kwargs)

    async def fetch():
        info, pages = await asyncio.gather(v.get_info(), v.get_pages())
        owner_name = ""
        owner = info.get("owner")
        if isinstance(owner, dict):
            owner_name = owner.get("name", "")
        return {
            "title": info.get("title", "未知"),
            "cover": info.get("pic", ""),
            "duration": _fmt_duration(info.get("duration", 0)),
            "owner": owner_name,
            "bvid": info.get("bvid", bvid),
            "aid": str(info.get("aid", aid)),
            "pages": [
                {
                    "cid": p.get("cid", 0),
                    "title": p.get("part", f"P{i+1}"),
                    "duration": _fmt_duration(p.get("duration", 0)),
                }
                for i, p in enumerate(pages)
            ],
        }

    return _run_async(fetch())


def format_video_info(info: dict) -> str:
    """格式化视频信息。"""
    lines = []
    lines.append(f"  {'='*50}")
    lines.append(f"  {info.get('title', '未知')}")
    lines.append(f"  {'='*50}")
    lines.append(f"  UP主:  {info.get('owner', '-')}")
    lines.append(f"  时长:  {info.get('duration', '-')}")
    lines.append(f"  BV号:  {info.get('bvid', '-')}")
    lines.append(f"  AV号:  {info.get('aid', '-')}")

    pages = info.get("pages", [])
    if len(pages) > 1:
        lines.append("")
        lines.append(f"  分集（共 {len(pages)} 集）：")
        for i, p in enumerate(pages, 1):
            lines.append(f"    [{i}] {p.get('title', '-')}  ({p.get('duration', '-')})")

    return "\n".join(lines)


def download_video(
    credential: Credential,
    bvid: str,
    aid: str,
    page_index: int,
    quality_v: int,
    quality_a: int,
    ffmpeg_path: str = _DEFAULT_FFMPEG,
    show_progress: bool = True,
    progress_callback=None,
    subdir: str = "",
) -> str:
    """下载单个视频分集，MP4 流（视频+音频分离），ffmpeg 混流。

    Args:
        page_index: 分集索引（从 0 开始）
        quality_v: VideoQuality 值
        quality_a: AudioQuality 值
        ffmpeg_path: ffmpeg 路径
        progress_callback: 进度回调 (downloaded_bytes, total_bytes) -> None
        subdir: 输出子目录（如收藏夹名），空则直接放 download/

    Returns:
        输出文件路径
    """
    if not show_progress and progress_callback is None:
        progress_callback = lambda *a: None

    max_retries = 3

    async def fetch():
        client = _get_client()
        kwargs = {"credential": credential}
        if bvid:
            kwargs["bvid"] = bvid
        elif aid:
            kwargs["aid"] = int(aid)
        v = _video_mod.Video(**kwargs)
        # 获取视频 info（含分集标题）
        info = await v.get_info()
        pages = await v.get_pages()
        page_title = ""
        if page_index < len(pages):
            page_title = pages[page_index].get("part", "")

        title = info.get("title", "未知视频")

        # 获取下载链接（含清晰度回退）
        dl_data = await v.get_download_url(page_index=page_index)

        # 找到用户选择清晰度在回退列表中的起始位置
        try:
            start_idx = _VIDEO_QUALITY_FALLBACK.index(quality_v)
        except ValueError:
            start_idx = len(_VIDEO_QUALITY_FALLBACK) - 1

        video_url = None
        audio_url = None
        used_quality = None

        for vq in _VIDEO_QUALITY_FALLBACK[start_idx:]:
            detecter = _video_mod.VideoDownloadURLDataDetecter(data=dl_data)
            streams = detecter.detect_best_streams(
                video_max_quality=vq,
                audio_max_quality=quality_a,
                no_dolby_audio=True,
                no_dolby_video=True,
                no_hdr=True,
                no_hires=True,
            )

            if detecter.check_video_and_audio_stream():
                # MP4: 视频流 + 音频流 分离
                if len(streams) >= 2:
                    video_url = streams[0].url
                    audio_url = streams[1].url
                    used_quality = vq
                    break
            else:
                # FLV: 音视频合流
                if streams:
                    video_url = streams[0].url
                    audio_url = None
                    used_quality = vq
                    break

        if not video_url:
            raise RuntimeError("无法获取下载链接——所有清晰度均不可用")

        if used_quality is not None and used_quality != quality_v:
            qname = next((n for n, v in _QUALITY_OPTIONS if v == used_quality), str(used_quality))
            print(f"  ⚠ 所选清晰度不可用，已自动回退至 {qname}")

        # 输出目录（从配置读取，可选子目录）
        download_dir = config.get_download_dir()
        if subdir:
            download_dir = os.path.join(download_dir, _sanitize_filename(subdir))
        os.makedirs(download_dir, exist_ok=True)

        # 输出文件名
        safe_title = _sanitize_filename(title)
        if page_title:
            safe_page = _sanitize_filename(page_title)
            basename = f"{safe_title}_P{page_index+1}_{safe_page}"
        else:
            basename = safe_title

        out_path = os.path.join(download_dir, f"{basename}.mp4")

        # 临时目录（从配置读取）
        vid_aid = str(info.get("aid", aid))
        temp_work_dir = os.path.join(config.get_temp_dir(), f"{vid_aid}_{page_index}")
        os.makedirs(temp_work_dir, exist_ok=True)

        video_temp = os.path.join(temp_work_dir, "video.m4s")
        audio_temp = os.path.join(temp_work_dir, "audio.m4s")

        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                # 下载视频流
                await _download_file(client, video_url, video_temp, "视频流", progress_callback)

                if audio_url:
                    # 下载音频流
                    await _download_file(client, audio_url, audio_temp, "音频流", progress_callback)
                    # ffmpeg 混流
                    _run_ffmpeg(ffmpeg_path, video_temp, audio_temp, out_path)
                else:
                    # FLV 直接转换
                    await _download_file(client, video_url, video_temp, "FLV流", progress_callback)
                    _run_ffmpeg(ffmpeg_path, video_temp, None, out_path)

                # 成功：记录到下载记录文件
                vid_aid_final = str(info.get("aid", aid))
                record_path = os.path.join(download_dir, ".biliadl")
                try:
                    with open(record_path, "a", encoding="utf-8") as rf:
                        rf.write(f"{vid_aid_final}_{page_index}\n")
                except OSError:
                    pass

                # 成功：清理临时目录
                if os.path.exists(temp_work_dir):
                    try:
                        shutil.rmtree(temp_work_dir)
                    except OSError:
                        pass
                return out_path

            except Exception as e:
                last_error = e
                # 清理残留的临时文件
                if os.path.exists(temp_work_dir):
                    try:
                        shutil.rmtree(temp_work_dir)
                    except OSError:
                        pass
                os.makedirs(temp_work_dir, exist_ok=True)

                if attempt < max_retries:
                    wait_s = attempt * 2
                    if not show_progress or progress_callback:
                        print(f"  ⚠ 下载失败，{wait_s}s 后重试 ({attempt}/{max_retries}): {e}")
                    await asyncio.sleep(wait_s)
                else:
                    raise last_error

    return _run_async(fetch())


async def _download_file(client, url: str, dest: str, label: str, progress_callback) -> None:
    """下载单个文件到指定路径。"""
    dwn_id = await client.download_create(url, _HEADERS)
    total = client.download_content_length(dwn_id)
    downloaded = 0
    with open(dest, "wb") as f:
        while True:
            try:
                chunk = await client.download_chunk(dwn_id)
            except StopAsyncIteration:
                break
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            if progress_callback:
                progress_callback(label, downloaded, total)
            else:
                _draw_progress(label, downloaded, total)
    if not progress_callback:
        _draw_progress(label, total, total)
        print()  # 完成后换行
    await client.download_close(dwn_id)


def _draw_progress(label: str, done: int, total: int) -> None:
    """绘制单行进度条：[████░░░░] xx% (X.XM/X.XM)。"""
    bar_width = 20
    if total > 0:
        pct = min(done / total, 1.0)
        filled = int(bar_width * pct)
    else:
        pct = 0
        filled = 0
    bar = "█" * filled + "░" * (bar_width - filled)
    done_str = _fmt_size(done)
    total_str = _fmt_size(total)
    sys.stdout.write(f"\r  {label:6s} [{bar}] {pct*100:3.0f}% ({done_str}/{total_str})")
    sys.stdout.flush()


def _fmt_size(n: int) -> str:
    """字节大小格式化。"""
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f}M"
    elif n >= 1024:
        return f"{n / 1024:.0f}K"
    else:
        return f"{n}B"


def _run_ffmpeg(ffmpeg: str, video_file: str, audio_file: str | None, output: str) -> None:
    """调用 ffmpeg 混流。"""
    if audio_file:
        cmd = [ffmpeg, "-i", video_file, "-i", audio_file,
               "-vcodec", "copy", "-acodec", "copy", output, "-y"]
    else:
        cmd = [ffmpeg, "-i", video_file,
               "-vcodec", "copy", "-acodec", "copy", output, "-y"]
    ret = subprocess.run(cmd, capture_output=True)
    if ret.returncode != 0:
        err = ret.stderr.decode(errors="replace")[:200]
        raise RuntimeError(f"ffmpeg 混流失败: {err}")


def _sanitize_filename(name: str) -> str:
    """清理文件名中的非法字符。"""
    return _re.sub(r'[\\/:*?"<>|]', '_', name).strip()

