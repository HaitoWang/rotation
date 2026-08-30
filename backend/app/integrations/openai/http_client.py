"""
HTTP 客户端 - 使用 curl_cffi 实现 TLS 指纹模拟
支持 Cloudflare 绕过，降级到 requests
"""
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 尝试使用 curl_cffi（推荐，自带 TLS 指纹模拟）
try:
    from curl_cffi.requests import Session as CffiSession

    _HAS_CFFI = True
    logger.debug("curl_cffi 可用，使用 TLS 指纹模拟")
except ImportError:
    _HAS_CFFI = False
    logger.debug("curl_cffi 不可用，降级到 requests")

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# 通用 UA（fallback，优先使用 fingerprint.generate_fingerprint() 生成的值）
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)

# 代理出口是美国：所有会话级兜底语言必须是美国值。
# curl_cffi 的 impersonate 会为我们没显式设置的头注入自己的默认值
# （实测 chrome146 会发 sec-ch-ua-platform: "macOS"），因此凡是绕过
# _common_headers 的请求都会带上与 Windows UA 矛盾的提示。
# 已实测：Session(headers=...) 能覆盖 impersonate 内建值，单请求 headers 再覆盖 Session。
DEFAULT_ACCEPT_LANGUAGE = "en-US,en;q=0.9"

# 全项目统一的 impersonate 兜底。凡是打 openai/chatgpt 的请求都必须用它，
# 不能再出现 chrome110/chrome136 这类和 Chrome/146 UA 对不上的旧配方。
DEFAULT_IMPERSONATE = "chrome146"


def us_chrome_headers() -> dict:
    """返回一套「美国 Windows Chrome 146」的完整环境头（HAR 真值）。

    给那些不走 auth_flow._common_headers 的旁路请求用（服务检测、
    exporter token 刷新、邮箱中转拉取…）。以前这些地方各自写死
    Chrome/145、Chrome/136 且完全不带 Client Hints / Accept-Language，
    curl_cffi 就会补上自己内建的 macOS 提示 —— UA 说 Windows、
    提示说 macOS，跨层自相矛盾，等于自曝（见 fingerprint-death-rootcause）。
    """
    return {
        "User-Agent": USER_AGENT,
        "Accept-Language": DEFAULT_ACCEPT_LANGUAGE,
        "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-full-version": '"146.0.7680.178"',
        "sec-ch-ua-full-version-list": (
            '"Chromium";v="146.0.7680.178", "Not-A.Brand";v="24.0.0.0", '
            '"Google Chrome";v="146.0.7680.178"'
        ),
        "sec-ch-ua-arch": '"x86"',
        "sec-ch-ua-bitness": '"64"',
        "sec-ch-ua-model": '""',
        "sec-ch-ua-platform-version": '"10.0.0"',
    }


def resolve_ca_bundle() -> str:
    """返回可读 CA bundle；优先显式环境变量，再用 certifi 和系统路径。"""
    candidates = [
        os.getenv("CURL_CA_BUNDLE", ""),
        os.getenv("SSL_CERT_FILE", ""),
        os.getenv("REQUESTS_CA_BUNDLE", ""),
    ]
    try:
        import certifi
        candidates.append(certifi.where())
    except ImportError:
        pass
    candidates.extend([
        "/etc/ssl/certs/ca-certificates.crt",
        "/usr/lib/ssl/cert.pem",
    ])
    for candidate in candidates:
        path = str(candidate or "").strip()
        if path and Path(path).is_file() and os.access(path, os.R_OK):
            return path
    raise RuntimeError("未找到可读的 TLS CA bundle")


def create_http_session(
    proxy: Optional[str] = None,
    impersonate: str = "safari18_0",
    user_agent: Optional[str] = None,
    accept_language: Optional[str] = None,
    client_hints: Optional[dict] = None,
):
    """
    创建 HTTP 会话。优先使用 curl_cffi 模拟浏览器 TLS 指纹，
    不可用时降级到 requests。

    ``user_agent`` / ``accept_language`` / ``client_hints`` 会写进 Session 级默认头，
    用来压掉 curl_cffi impersonate 的内建值（否则裸请求会漏出 macOS 提示）。
    单请求传入的 headers 仍然优先级更高。
    """
    if _HAS_CFFI:
        session = CffiSession(impersonate=impersonate)
        session.verify = resolve_ca_bundle()

        # Session 级兜底头：覆盖 impersonate 内建的 macOS/语言默认值
        base_headers = {"Accept-Language": accept_language or DEFAULT_ACCEPT_LANGUAGE}
        if user_agent:
            base_headers["User-Agent"] = user_agent
        for key, value in (client_hints or {}).items():
            if value:
                base_headers[key] = value
        try:
            session.headers.update(base_headers)
        except Exception:  # pragma: no cover - 老版本 curl_cffi 兜底
            logger.debug("Session 级默认头设置失败，退回单请求头", exc_info=True)
        # 使用显式配置，避免被系统 HTTP(S)_PROXY 隐式污染。
        session.trust_env = False
        if proxy:
            # curl_cffi 在 SOCKS 代理下建议使用 socks5h，让 DNS 走代理端解析。
            # 这能减少本地 DNS/链路导致的 TLS 握手异常。
            normalized_proxy = proxy
            if proxy.startswith("socks5://"):
                normalized_proxy = "socks5h://" + proxy[len("socks5://"):]
                logger.info("代理协议已标准化: socks5:// -> socks5h://")
            session.proxies = {"https": normalized_proxy, "http": normalized_proxy}
        else:
            # 显式设置空代理，覆盖系统环境变量 (trust_env=False 对 libcurl 不够)
            session.proxies = {"https": "", "http": ""}
        return session
    else:
        session = requests.Session()
        session.trust_env = False
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "POST"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        if proxy:
            session.proxies = {"https": proxy, "http": proxy}
        session.headers["User-Agent"] = user_agent or USER_AGENT
        session.headers["Accept-Language"] = accept_language or DEFAULT_ACCEPT_LANGUAGE
        for key, value in (client_hints or {}).items():
            if value:
                session.headers[key] = value
        return session
