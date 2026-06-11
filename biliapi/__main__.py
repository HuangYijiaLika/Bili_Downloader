"""允许通过 python -m biliapi 运行。

默认启动交互式 TUI（箭头键选择菜单）。
使用 python -m biliapi --cli 进入传统命令行模式。
"""

import sys

# 在 Windows 上强制使用 UTF-8 编码输出，避免中文乱码
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 优先使用 httpx 作为 HTTP 后端（curl_cffi 在多次 asyncio.run() 间有 event loop 泄漏问题）
from bilibili_api.utils.network import select_client
select_client("httpx")

# 判断是否使用传统 CLI 模式
if "--cli" in sys.argv:
    sys.argv.remove("--cli")
    from .cli import main as cli_main
    cli_main()
else:
    from .tui import main_loop
    main_loop()
