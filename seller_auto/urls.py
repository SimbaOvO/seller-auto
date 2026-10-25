"""店铺链接、美客多搜索地址和商品编号。"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qs, quote, urljoin, urlsplit, urlunsplit

AMAZON_BASE = "https://www.amazon.com.mx"
ML_HOST = "listado.mercadolibre.com.ar"


class UrlError(ValueError):
    """链接不符合抓取入口的要求。"""


def parse_seller_id(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"}:
        raise UrlError("店铺链接必须是 http 或 https")
    host = parts.netloc.lower()
    if "amazon." not in host:
        raise UrlError("店铺链接必须是亚马逊地址")
    seller = parse_qs(parts.query).get("me", [""])[0].strip()
    if not seller:
        raise UrlError("店铺链接里没有 me 参数")
    return seller


def store_url_for_seller(seller_id: str) -> str:
    return f"{AMAZON_BASE}/s?me={seller_id}&marketplaceID={seller_id}"


def resolve_store_input(value: str) -> tuple[str, str]:
    """从店铺 id、`处理这个店铺id：xxx` 或整段店铺链接得到卖家 id 和标准店铺地址。"""

    text = " ".join((value or "").split())
    if not text:
        raise UrlError("请提供店铺 id")
    if text.startswith(("http://", "https://")):
        seller_id = parse_seller_id(text)
    else:
        labeled = re.search(r"(?:店铺|卖家)\s*id\s*[:：]\s*([A-Za-z0-9]+)", text, flags=re.IGNORECASE)
        embedded = re.search(r"(?:^|[?&])me=([A-Za-z0-9]+)", text)
        if labeled:
            seller_id = labeled.group(1)
        elif embedded:
            seller_id = embedded.group(1)
        elif re.fullmatch(r"[A-Za-z0-9]+", text):
            seller_id = text
        else:
            raise UrlError("店铺 id 只能包含字母和数字")
    return seller_id, store_url_for_seller(seller_id)


def absolute_amazon_url(href: str, base: str = AMAZON_BASE) -> str:
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    return urljoin(base, href)


def extract_asin(url: str) -> str:
    match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)", url, flags=re.I)
    if match:
        return match.group(1).upper()
    match = re.search(r"(?:^|[/?&=])([A-Z0-9]{10})(?:[/?&#]|$)", url)
    if match and re.fullmatch(r"[A-Z0-9]{10}", match.group(1)):
        return match.group(1).upper()
    return ""


def parse_focus_product(value: str) -> str:
    """空字符串表示遍历整个店铺。否则返回要单独处理的 ASIN。"""

    text = " ".join((value or "").split())
    if not text:
        return ""
    labeled = re.search(r"(?:商品|链接|asin)\s*[:：]\s*(\S+)", text, flags=re.IGNORECASE)
    if labeled:
        text = labeled.group(1)
    if text.startswith(("http://", "https://")):
        asin = extract_asin(text)
        if not asin:
            raise UrlError("商品链接里没有 ASIN")
        return asin
    if re.fullmatch(r"[A-Z0-9]{10}", text, flags=re.IGNORECASE):
        return text.upper()
    asin = extract_asin(text)
    if asin:
        return asin
    raise UrlError("指定商品请提供 10 位 ASIN，或亚马逊商品链接")


def canonical_amazon_url(url: str) -> str:
    absolute = absolute_amazon_url(url)
    asin = extract_asin(absolute)
    if not asin:
        parts = urlsplit(absolute)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return f"{AMAZON_BASE}/dp/{asin}"


def clean_keywords(keywords: str) -> str:
    return " ".join((keywords or "").split())


def slugify_keywords(keywords: str) -> str:
    text = clean_keywords(keywords)
    normalized = unicodedata.normalize("NFKD", text)
    without_marks = "".join(char for char in normalized if not unicodedata.combining(char))
    lowered = without_marks.casefold()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return slug


def ml_search_url(keywords: str) -> str:
    text = clean_keywords(keywords)
    slug = slugify_keywords(text)
    if not text or not slug:
        raise UrlError("关键词不能为空")
    encoded = quote(text, safe="")
    return f"https://{ML_HOST}/{slug}?sb=all_mercadolibre#D[A:{encoded}]"


def ml_page2_url(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path or "/"
    if "_Desde_" in path:
        return url
    if path.endswith("/"):
        path = path[:-1]
    path = f"{path}_Desde_49"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def ml_item_key(url: str) -> str:
    match = re.search(r"MLA-?(\d+)", url or "", flags=re.I)
    if match:
        return "MLA" + match.group(1)
    parts = urlsplit(url or "")
    if not parts.netloc:
        return url or ""
    return urlunsplit((parts.scheme or "https", parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def canonical_ml_url(url: str) -> str:
    parts = urlsplit(absolute_amazon_url(url, "https://www.mercadolibre.com.ar"))
    if not parts.netloc:
        return url
    return urlunsplit((parts.scheme or "https", parts.netloc, parts.path.rstrip("/"), "", ""))


def looks_like_verification_url(url: str) -> bool:
    lowered = (url or "").lower()
    markers = (
        "validatecaptcha",
        "/ap/cvf",
        "opfcaptcha",
        "account-verification",
        "/gz/captcha",
        "suspicious-traffic",
        "/captcha/",
    )
    return any(marker in lowered for marker in markers)


def text_looks_blocked(text: str) -> bool:
    folded = (text or "").casefold()
    markers = (
        "hubo un error accediendo a esta pagina",
        "hubo un error accediendo a esta página",
        "no eres un robot",
        "ingresa los caracteres",
        "robot check",
    )
    return any(marker in folded for marker in markers)


def parse_brand(byline: str) -> str:
    text = " ".join((byline or "").split())
    if not text:
        return ""
    patterns = (
        r"Visita la tienda de\s+(.+)",
        r"Visita la Store de\s+(.+)",
        r"(?:Marca|Brand)\s*:\s*(.+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            return match.group(1).strip(" .")
    return text


def choose_amazon_image_url(
    hires: str | None,
    dynamic_json: str | None,
    src: str | None,
) -> str | None:
    if hires:
        return hires
    if dynamic_json:
        import json

        try:
            data = json.loads(dynamic_json)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and data:
            best_url = None
            best_area = -1
            for url, size in data.items():
                area = 0
                if isinstance(size, (list, tuple)) and len(size) >= 2:
                    try:
                        area = int(size[0]) * int(size[1])
                    except (TypeError, ValueError):
                        area = 0
                if area >= best_area:
                    best_area = area
                    best_url = str(url)
            if best_url:
                return best_url
    return src or None
