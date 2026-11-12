"""Gets Instagram login cookies from a browser or from pasted text."""
from __future__ import annotations

import re
import secrets
from typing import Dict
from urllib.parse import unquote

BROWSERS = {"firefox": "Firefox", "edge": "Edge", "chrome": "Chrome", "brave": "Brave", "opera": "Opera"}


class AuthError(Exception):
    pass


def _complete(cookies: Dict[str, str]) -> Dict[str, str]:
    if not cookies.get("sessionid"):
        raise AuthError("کوکی sessionid پیدا نشد؛ مطمئن شوید در این مرورگر وارد اینستاگرام شده‌اید.")
    # instaloader needs one; Instagram only checks that it matches the header sent with it.
    cookies.setdefault("csrftoken", secrets.token_hex(16))
    return cookies


def cookies_from_browser(browser: str) -> Dict[str, str]:
    import browser_cookie3

    try:
        jar = getattr(browser_cookie3, browser)(domain_name="instagram.com")
    except Exception as error:  # the library raises many types, depending on browser and version
        raise AuthError(
            f"خواندن کوکی از {BROWSERS.get(browser, browser)} ممکن نشد. مرورگر را ببندید و دوباره امتحان کنید. "
            "نسخه‌های جدید Chrome و Edge رمزنگاری‌ای دارند که اجازهٔ خواندن نمی‌دهد؛ "
            f"در این صورت Firefox یا روش چسباندن کوکی را به کار ببرید.\n{error}") from error
    return _complete({cookie.name: cookie.value for cookie in jar if cookie.value})


def cookies_from_text(text: str) -> Dict[str, str]:
    """Accepts a bare sessionid value or a whole 'name=value; name=value' cookie header."""
    text = text.strip()
    if text.lower().startswith("cookie:"):
        text = text[7:].strip()
    if "=" in text:
        pairs = (part.split("=", 1) for part in re.split(r";\s*", text) if "=" in part)
        return _complete({name.strip(): value.strip().strip('"') for name, value in pairs})
    # Copied from the browser's cookie list, the value is sometimes still URL-encoded.
    return _complete({"sessionid": unquote(text)})
