"""Crawler routing only. Never reads or changes the user's VPN settings."""

from __future__ import annotations

import os
from urllib.parse import urlsplit


def proxy_url(cfg):
    value = os.environ.get(cfg.proxy_env, "").strip()
    if value:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("采集代理须为 HTTP(S) 地址；不要把订阅链接填作代理")
    return value


def routes(cfg, mode=None):
    mode = mode or cfg.network_mode
    if mode == "proxy":
        if not proxy_url(cfg):
            raise ValueError("未配置采集代理环境变量：" + cfg.proxy_env)
        return ["proxy"]
    if mode == "auto":
        # env remains the original behaviour; a second explicit route is opt-in.
        return ["env", "proxy"] if proxy_url(cfg) else ["env", "direct"]
    return [mode]


def network_context(cfg):
    return {
        "mode": cfg.network_mode,
        "explicit_proxy_configured": bool(os.environ.get(cfg.proxy_env)),
        "proxy_environment_variables": sorted(
            k for k in os.environ if k.lower() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")
        ),
        "note": "direct 仅忽略应用代理，仍可能经过系统 TUN/VPN；本程序不修改系统分流或 TLS 校验。",
    }
