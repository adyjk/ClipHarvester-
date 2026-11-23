"""配置加载。

所有路径（数据库、缓存）都相对项目根目录 d:\\video-finder 解析，
不会往硬盘其它地方写文件。
"""

from __future__ import annotations

import copy
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")

DEFAULTS = {
    "db": "data/videos.db",
    "server": {"host": "127.0.0.1", "port": 8765},
    "ingest": {
        # 串行 + 间隔抖动，宁可慢也别触发风控
        "request_interval": 1.5,
        "jitter": 0.8,
        "max_per_run": 120,
        "daily_limit": 900,
        "pages_per_query": 1,
        "queries": [
            "Fabric 模组开发",
            "Minecraft 生存建筑",
            "独立游戏 开发日志",
        ],
        "ranking_rids": [0, 4, 36],
        "related_depth": 1,
        "related_seeds": 6,
        "enable_search": True,
        "enable_ranking": True,
        "enable_related": True,
        # 浏览器登录后的 Cookie 串（可选）：SESSDATA=xxx; bili_jct=xxx
        "cookie": "",
    },
    "ai": {
        "base_url": "http://127.0.0.1:11434",
        "model": "qwen2.5:3b",
        "timeout": 180,
        "max_per_run": 40,
        "num_predict": 420,
    },
    "scoring": {
        "weights": {
            "interest": 1.0,
            "recency": 0.8,
            "engagement": 0.9,
            "duration": 0.5,
            "ai_quality": 1.2,
            "keyword": 1.0,
        },
        "recency_days": 14,
        "duration_prefer": [180, 3600],
        "exclude_keywords": ["广告", "带货", "抽奖"],
        "min_duration": 30,
    },
}


def _merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load() -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            _merge(cfg, json.load(f))
    else:
        save(cfg)
    return cfg


def save(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def path(cfg: dict, key: str = "db") -> str:
    """把配置里的相对路径解析到项目根目录下。"""
    p = cfg.get(key) or DEFAULTS[key]
    return p if os.path.isabs(p) else os.path.join(ROOT, p)
