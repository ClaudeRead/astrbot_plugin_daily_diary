"""QQ 空间发布模块：使用 QQ 空间 Web 版「说说」接口 + Cookie 鉴权。

原理：浏览器登录 QQ 空间后，Web 端通过 p_skey 计算 b_qq/g_tk 令牌，
再调用 taotao.qq.com 的说说发布接口。插件复刻该流程，让 Bot 用自己的
QQ Cookie 自动发布日记。

无需第三方库，aiohttp 为 AstrBot 内置依赖。
"""

import ipaddress
from urllib.parse import urlparse

import aiohttp

from astrbot.api import logger

# QQ 空间 Web 版固定的发布端点与公开 appkey（所有浏览器客户端一致）
PUBLISH_URL = (
    "https://user.qzone.qq.com/proxy-domain/taotao.qq.com"
    "/cgi-bin/emotion_cgi_publish_v6"
)
APP_KEY = "333cf3a41e4642ac8e0c496a4334db06"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# 说说可见范围：1 公开、2 仅好友、3 仅自己
PRIVACY_MAP = {"公开": 1, "仅好友": 2, "仅自己": 3}
# 这些返回码通常意味着 Cookie 过期或未登录
_EXPIRED_CODES = (-3000, -3001, -3002, -4003, 100012)


def _assert_safe_url(url: str) -> None:
    """请求前安全校验：仅允许 http/https，且拒绝本机/内网/保留地址。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"不支持的 URL 协议: {parsed.scheme!r}")
    host = (parsed.hostname or "").lower()
    try:
        ip = ipaddress.ip_address(host)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_reserved
            or ip.is_link_local
            or ip.is_unspecified
        ):
            raise ValueError(f"禁止请求内网/保留地址: {host}")
    except ipaddress.AddressValueError:
        # 非 IP 字面量，检查常见本机域名
        if host in ("localhost",) or host.endswith(".local"):
            raise ValueError(f"禁止请求本机地址: {host}")


def calc_gtk(skey: str) -> int:
    """由 p_skey（或 skey）计算 b_qq/g_tk 令牌（DJBX33A 变种）。"""
    h = 5381
    for ch in skey:
        h += (h << 5) + ord(ch)
    return h & 0x7FFFFFFF


def parse_cookie(cookie_str: str) -> dict:
    """把浏览器复制的 Cookie 字符串解析为字典。"""
    cookies = {}
    for pair in cookie_str.split(";"):
        key, sep, value = pair.strip().partition("=")
        if sep and key:
            cookies[key] = value
    return cookies


async def publish_diary(text: str, uin: str, cookie_str: str, privacy_label: str = "公开"):
    """发布一条说说。返回 (是否成功, 说明信息)。

    Args:
        text:         日记正文
        uin:          Bot 的 QQ 号
        cookie_str:   浏览器复制的完整 Cookie 字符串（需含 p_skey）
        privacy_label: 可见范围，见 PRIVACY_MAP
    """
    if not uin or not cookie_str:
        return False, "未配置 QQ 号或 Cookie"

    cookies = parse_cookie(cookie_str)
    # p_skey 是 QQ 空间域的令牌，缺失时退回 skey 尝试
    skey = cookies.get("p_skey") or cookies.get("skey")
    if not skey:
        return False, "Cookie 中缺少 p_skey 字段，请按 README 重新获取"

    _assert_safe_url(PUBLISH_URL)
    gtk = calc_gtk(skey)
    privacy = PRIVACY_MAP.get(privacy_label, 1)

    params = {
        "uin": str(uin),
        "g_tk": str(gtk),
        "b_qq": str(gtk),
        "qzreferrer": f"https://user.qzone.qq.com/{uin}/main",
    }
    data = {
        "synthesis_type": "1",
        "richtype": "",
        "richval": "",
        "special_url": "",
        "subrichtype": "",
        "who": "1",
        "con": text,                       # 说说正文
        "feed_type": "1",
        "policy_type": "2",
        "ver": "1",
        "appkey": APP_KEY,
        "privacy": str(privacy),
        "appid": "311",
        "format": "json",
    }
    headers = {
        "Referer": f"https://user.qzone.qq.com/{uin}/main",
        "User-Agent": USER_AGENT,
    }

    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                PUBLISH_URL, params=params, data=data, headers=headers, cookies=cookies
            ) as resp:
                try:
                    payload = await resp.json(content_type=None)
                except Exception:
                    return False, f"接口返回异常（HTTP {resp.status}）"

                code = payload.get("code")
                if code == 0:
                    return True, "已发布为新说说"

                msg = payload.get("message") or payload.get("msg") or ""
                hint = "，Cookie 可能已失效，请更新" if code in _EXPIRED_CODES else ""
                return False, f"接口返回 code={code}{('：' + msg) if msg else ''}{hint}"
    except aiohttp.ClientError as e:
        logger.error(f"[每日一记] QQ 空间发布网络异常: {e}")
        return False, f"网络异常：{e}"
