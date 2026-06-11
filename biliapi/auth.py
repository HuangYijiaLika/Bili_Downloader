"""凭证管理模块。

负责：
1. 从用户粘贴的 cookie.json 内容中提取 SESSDATA / bili_jct / buvid3 / DedeUserID
2. 将凭证缓存到本地文件 ~/.biliapi/credentials.json
3. 从本地文件加载凭证
4. 检测凭证是否过期
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional, Dict

from bilibili_api import Credential


CONFIG_DIR = Path.home() / ".biliapi"
CREDENTIALS_FILE = CONFIG_DIR / "credentials.json"


def _safe_print(*args, **kwargs) -> None:
    """安全打印，自动处理 Windows GBK 编码无法显示 Unicode 符号的问题。"""
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        # 降级：替换为 ASCII 等效字符
        safe_args = []
        for a in args:
            if isinstance(a, str):
                a = a.replace("✓", "[OK]").replace("✗", "[ERR]").replace("⚠", "[WARN]")
            safe_args.append(a)
        print(*safe_args, **kwargs)


def get_credentials() -> Optional[Credential]:
    """获取凭证：优先从本地文件加载，若无或过期则提示用户输入。

    Returns:
        Credential 实例，如果用户拒绝输入则返回 None
    """
    # 1. 尝试从本地加载
    cred = _load_from_file()
    if cred is not None:
        return cred

    # 2. 提示用户输入
    _safe_print("=" * 60)
    _safe_print("  首次使用需要配置 B 站凭证")
    _safe_print("  请将 cookie.json 的内容复制粘贴到下方，按 Ctrl+Z 然后按 Enter 结束输入：")
    _safe_print("=" * 60)

    return _prompt_and_save()


def force_relogin() -> Optional[Credential]:
    """强制重新输入凭证。

    Returns:
        Credential 实例
    """
    if CREDENTIALS_FILE.exists():
        CREDENTIALS_FILE.unlink()
    _safe_print("已清除本地凭证，请重新输入：")
    return _prompt_and_save()


def _load_from_file() -> Optional[Credential]:
    """从本地文件加载凭证，同时检查过期。"""
    if not CREDENTIALS_FILE.exists():
        return None

    try:
        data = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, IOError):
        return None

    # 检查 SESSDATA 是否过期（通过 expirationDate 字段）
    expiration = data.get("_expiration")
    if expiration and time.time() > expiration:
        _safe_print("⚠ 凭证已过期，请重新输入。")
        CREDENTIALS_FILE.unlink()
        return None

    required = ["sessdata", "bili_jct", "buvid3", "dedeuserid"]
    if not all(k in data for k in required):
        _safe_print("⚠ 本地凭证数据不完整，请重新输入。")
        CREDENTIALS_FILE.unlink()
        return None

    return Credential(
        sessdata=data["sessdata"],
        bili_jct=data["bili_jct"],
        buvid3=data.get("buvid3", ""),
        dedeuserid=data.get("dedeuserid", ""),
    )


def _prompt_and_save() -> Optional[Credential]:
    """提示用户粘贴 cookie.json 内容，解析并保存。"""
    # 读取多行输入直到 EOF
    lines = []
    try:
        while True:
            line = sys.stdin.readline()
            if not line:  # EOF
                break
            lines.append(line)
    except KeyboardInterrupt:
        _safe_print("\n已取消。")
        return None

    raw = "".join(lines).strip()
    if not raw:
        _safe_print("未检测到输入，已取消。")
        return None

    parsed = _parse_cookie_json(raw)
    if parsed is None:
        return None

    cred, uid, expiration = parsed

    # 保存到本地
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "sessdata": cred.sessdata,
        "bili_jct": cred.bili_jct,
        "buvid3": cred.buvid3,
        "dedeuserid": cred.dedeuserid,
        "uid": uid,
        "_expiration": expiration,
    }
    CREDENTIALS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    _safe_print(f"✓ 凭证已保存到 {CREDENTIALS_FILE}")
    return cred


def _parse_cookie_json(raw: str) -> Optional[tuple[Credential, str, Optional[float]]]:
    """解析 cookie.json 格式的内容，提取关键字段。

    Args:
        raw: cookie.json 的原始文本内容

    Returns:
        (Credential, uid, expiration) 元组，解析失败返回 None
    """
    try:
        cookies = json.loads(raw)
    except json.JSONDecodeError as e:
        _safe_print(f"✗ JSON 解析失败: {e}")
        return None

    if not isinstance(cookies, list):
        # 也可能是 dict 格式（用户直接贴了 credentials 文件）
        if isinstance(cookies, dict):
            return _parse_credential_dict(cookies)
        _safe_print("✗ 格式错误：需要 cookie.json 导出的 Cookie 数组。")
        return None

    # 构建 name -> value 映射
    cookie_map: Dict[str, str] = {}
    expiration_map: Dict[str, float] = {}
    for c in cookies:
        name = c.get("name", "")
        value = c.get("value", "")
        if name and value:
            cookie_map[name] = value
        exp = c.get("expirationDate")
        if name and exp:
            expiration_map[name] = exp

    # 检查必要字段
    missing = []
    for key in ["SESSDATA", "bili_jct", "DedeUserID"]:
        if key not in cookie_map:
            missing.append(key)
    if missing:
        _safe_print(f"✗ 缺少必要的 Cookie 字段: {', '.join(missing)}")
        _safe_print("  请确认 cookie.json 中包含 SESSDATA、bili_jct、DedeUserID")
        return None

    buvid3 = cookie_map.get("buvid3", cookie_map.get("buvid4", ""))

    cred = Credential(
        sessdata=cookie_map["SESSDATA"],
        bili_jct=cookie_map["bili_jct"],
        buvid3=buvid3,
        dedeuserid=cookie_map["DedeUserID"],
    )
    uid = cookie_map["DedeUserID"]
    expiration = expiration_map.get("SESSDATA")  # SESSDATA 的过期时间

    _safe_print(f"✓ 解析成功 — UID: {uid}")
    return cred, uid, expiration


def _parse_credential_dict(data: dict) -> Optional[tuple[Credential, str, Optional[float]]]:
    """解析直接的 credential 字典格式。"""
    cred = Credential(
        sessdata=data.get("sessdata", ""),
        bili_jct=data.get("bili_jct", "") or data.get("bili_jct", ""),
        buvid3=data.get("buvid3", ""),
        dedeuserid=str(data.get("dedeuserid", "") or data.get("uid", "")),
    )
    uid = str(data.get("dedeuserid", data.get("uid", "")))
    if not cred.sessdata or not cred.bili_jct:
        _safe_print("✗ 缺少 sessdata 或 bili_jct 字段。")
        return None
    _safe_print(f"✓ 解析成功 — UID: {uid}")
    return cred, uid, None
