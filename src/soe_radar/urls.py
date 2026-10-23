from __future__ import annotations

import ipaddress
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def public_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("仅接受无凭证的 HTTP(S) 公告地址")
    host = parts.hostname.lower()
    if host in ("localhost", "metadata.google.internal") or host.endswith((".local", ".internal")):
        raise ValueError("公告地址不能指向本机或内部网络")
    try:
        if not ipaddress.ip_address(host).is_global:
            raise ValueError("公告地址不能指向私有或保留地址")
    except ValueError as exc:
        if "私有" in str(exc):
            raise
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


def on_domains(url: str, domains: list[str]) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def canonical(url: str) -> str:
    parts = urlsplit(public_url(url))
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.startswith("utm_") and k not in ("from", "from_source", "spm", "eqid", "CT6T", "CT6TS")
    ]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(query), ""))
