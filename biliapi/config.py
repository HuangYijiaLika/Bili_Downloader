"""配置管理模块。

管理用户可自定义的设置项：
- download_dir: 下载输出目录（默认 ./download）
- temp_dir: 临时文件目录（默认 ./temp）

配置存储在 ~/.biliapi/config.json。
"""

import json
import os
from pathlib import Path
from typing import Optional


CONFIG_DIR = Path.home() / ".biliapi"
CONFIG_FILE = CONFIG_DIR / "config.json"

_DEFAULTS = {
    "download_dir": "./download",
    "temp_dir": "./temp",
    "max_parallel": "3",
}

_config_cache: Optional[dict] = None


def load_config() -> dict:
    """加载配置，未设置的项使用默认值。"""
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    config = dict(_DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                config.update(data)
        except (json.JSONDecodeError, IOError):
            pass

    _config_cache = config
    return config


def save_config(config: dict) -> None:
    """保存配置到文件。"""
    global _config_cache
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps(config, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    _config_cache = dict(config)


def _invalidate_cache() -> None:
    """清除缓存，强制下次重新读取。"""
    global _config_cache
    _config_cache = None


def get_download_dir() -> str:
    """获取下载目录的绝对路径。"""
    cfg = load_config()
    path = cfg.get("download_dir", _DEFAULTS["download_dir"])
    if not os.path.isabs(path):
        path = os.path.join(os.getcwd(), path)
    return os.path.normpath(path)


def get_temp_dir() -> str:
    """获取临时文件目录的绝对路径。"""
    cfg = load_config()
    path = cfg.get("temp_dir", _DEFAULTS["temp_dir"])
    if not os.path.isabs(path):
        path = os.path.join(os.getcwd(), path)
    return os.path.normpath(path)


def get(key: str, default=None):
    """获取单个配置项（原始值，不做路径解析）。"""
    cfg = load_config()
    return cfg.get(key, default)


def set_(key: str, value: str) -> None:
    """设置单个配置项并保存。"""
    cfg = load_config()
    cfg[key] = value
    save_config(cfg)
