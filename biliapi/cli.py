"""命令行接口模块。

使用 argparse 提供子命令：
    biliapi bangumi     — 查看追番列表
    biliapi favorites   — 查看收藏夹列表
    biliapi favorites <id> — 查看收藏夹内容
    biliapi login       — 强制重新登录
"""

import argparse
import sys
from typing import Optional

from .auth import get_credentials, force_relogin
from .commands import (
    get_bangumi_list,
    format_bangumi_list,
    get_favorite_lists,
    format_favorite_lists,
    get_favorite_content,
    format_favorite_content,
)


def main(argv: Optional[list[str]] = None) -> None:
    """CLI 主入口。"""
    parser = argparse.ArgumentParser(
        prog="biliapi",
        description="B站追番与收藏管理工具",
    )
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # ---- bangumi ----
    bangumi_parser = subparsers.add_parser("bangumi", help="查看追番/追剧列表")
    bangumi_parser.add_argument(
        "--type", "-t",
        choices=["BANGUMI", "DRAMA"],
        default="BANGUMI",
        help="番剧类型: BANGUMI(番剧) 或 DRAMA(电视剧/纪录片) [默认: BANGUMI]",
    )
    bangumi_parser.add_argument(
        "--status", "-s",
        choices=["ALL", "WANT", "WATCHING", "WATCHED"],
        default="ALL",
        help="追番状态 [默认: ALL]",
    )
    bangumi_parser.add_argument(
        "--page", "-p",
        type=int,
        default=1,
        help="页码 [默认: 1]",
    )
    bangumi_parser.add_argument(
        "--page-size",
        type=int,
        default=15,
        help="每页数量 [默认: 15]",
    )

    # ---- favorites (list) ----
    fav_parser = subparsers.add_parser("favorites", help="查看收藏夹")
    fav_parser.add_argument(
        "media_id",
        nargs="?",
        type=int,
        default=None,
        help="收藏夹 ID — 提供则查看该收藏夹内容，不提供则列出所有收藏夹",
    )
    fav_parser.add_argument(
        "--page", "-p",
        type=int,
        default=1,
        help="页码 [默认: 1]",
    )
    fav_parser.add_argument(
        "--keyword", "-k",
        type=str,
        default="",
        help="搜索收藏夹内容的关键词",
    )

    # ---- login ----
    subparsers.add_parser("login", help="强制重新输入凭证")

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return

    # login 命令 — 不需要先获取凭据
    if args.command == "login":
        force_relogin()
        return

    # 其他命令都需要凭据
    credential = get_credentials()
    if credential is None:
        print("未配置凭证，无法继续。")
        sys.exit(1)

    try:
        if args.command == "bangumi":
            _cmd_bangumi(credential, args)
        elif args.command == "favorites":
            _cmd_favorites(credential, args)
    except Exception as e:
        error_msg = str(e)
        # 检测认证错误
        if any(kw in error_msg.lower() for kw in ("auth", "unauthorized", "403", "401", "credential", "login")):
            print(f"✗ 凭证可能已失效: {e}")
            print("  请运行 biliapi login 重新配置凭证。")
        else:
            print(f"✗ 错误: {e}")
        sys.exit(1)


def _cmd_bangumi(credential, args) -> None:
    """执行追番查询命令。"""
    print(f"正在查询追番列表（类型: {args.type}, 状态: {args.status}, 第 {args.page} 页）...\n")
    data = get_bangumi_list(
        credential=credential,
        type_=args.type,
        status=args.status,
        page=args.page,
        page_size=args.page_size,
    )
    print(format_bangumi_list(data))


def _cmd_favorites(credential, args) -> None:
    """执行收藏夹查询命令。"""
    if args.media_id is not None:
        # 查看指定收藏夹内容
        print(f"正在查询收藏夹 {args.media_id} 的内容...\n")
        data = get_favorite_content(
            credential=credential,
            media_id=args.media_id,
            page=args.page,
            keyword=args.keyword,
        )
        print(format_favorite_content(data))
    else:
        # 列出所有收藏夹
        print("正在查询收藏夹列表...\n")
        items = get_favorite_lists(credential=credential)
        print(format_favorite_lists(items))


if __name__ == "__main__":
    main()
