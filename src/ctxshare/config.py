"""Configuration for ctxshare (no API keys: this tool only moves context)."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from .errors import ConfigError
from .util import atomic_write, config_dir, read_json_file

CONFIG_VERSION = 1

DEFAULT_CONFIG: Dict[str, Any] = {
    "version": CONFIG_VERSION,
    "share": {
        "host": "0.0.0.0",
        "port": 8787,
        "auto_serve": True,
        "ttl_hours": 72,
        "public_base_url": "",
        "target": "chatgpt",
    },
    "fetch": {
        "timeout": 45,
    },
    "import": {
        "max_chars": 0,
    },
}


def config_path() -> Path:
    override = os.environ.get("CTXSHARE_CONFIG")
    if override:
        return Path(override).expanduser()
    return config_dir() / "config.json"


def _fill_missing(target: Dict[str, Any], template: Dict[str, Any]) -> None:
    for key, value in template.items():
        if key not in target:
            target[key] = copy.deepcopy(value)
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            _fill_missing(target[key], value)


def load_config(path: Optional[Path] = None, create: bool = True) -> Dict[str, Any]:
    path = path or config_path()
    raw = read_json_file(path)
    if raw is None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        if create:
            save_config(config, path)
        return config
    if not isinstance(raw, dict):
        raise ConfigError(f"Config {path} harus berupa objek JSON")
    _fill_missing(raw, DEFAULT_CONFIG)
    validate_config(raw)
    return raw


def save_config(config: Dict[str, Any], path: Optional[Path] = None) -> Path:
    path = path or config_path()
    validate_config(config)
    atomic_write(path, json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    return path


def validate_config(config: Dict[str, Any]) -> None:
    share = config.get("share")
    if not isinstance(share, dict):
        raise ConfigError("config.share harus berupa objek")
    try:
        port = int(share.get("port", 8787))
    except (TypeError, ValueError) as exc:
        raise ConfigError("share.port harus berupa angka") from exc
    if not 0 <= port <= 65535:
        raise ConfigError("share.port harus di antara 0 dan 65535")
    share["port"] = port
    try:
        share["ttl_hours"] = float(share.get("ttl_hours", 0))
    except (TypeError, ValueError) as exc:
        raise ConfigError("share.ttl_hours harus berupa angka") from exc
    target = str(share.get("target") or "chatgpt").lower()
    if target not in ("chatgpt", "claude", "gemini", "generic"):
        raise ConfigError("share.target harus chatgpt, claude, gemini, atau generic")
    share["target"] = target
    fetch = config.get("fetch") or {}
    try:
        fetch["timeout"] = float(fetch.get("timeout", 45))
    except (TypeError, ValueError) as exc:
        raise ConfigError("fetch.timeout harus berupa angka") from exc
    if fetch["timeout"] < 5:
        fetch["timeout"] = 5
    config["fetch"] = fetch
    imports = config.get("import") or {}
    try:
        imports["max_chars"] = int(imports.get("max_chars", 0))
    except (TypeError, ValueError) as exc:
        raise ConfigError("import.max_chars harus berupa angka") from exc
    config["import"] = imports
