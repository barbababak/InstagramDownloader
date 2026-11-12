"""Turns post data into safe Windows file names."""
from __future__ import annotations

import os
import random
import re
import unicodedata
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

_HASHTAG = re.compile(r"#[\w\u200c]+")
# A mention is often part of the sentence ("@name is a winner"), so only the "@" goes.
_MENTION = re.compile(r"@([\w.]*\w)")
_ILLEGAL = re.compile(r'[<>:"/\\|?*]')
_ZWNJ = "‌"  # meaningful in Persian words, so it is kept
_EDGE_JUNK = " .-_+|،,;:!؛"
_MAX_PATH = 240


def parse_username(text: str) -> Optional[str]:
    """Accepts a bare name, @name or a profile URL."""
    text = text.strip()
    match = re.search(r"instagram\.com/([^/?#\s]+)", text)
    name = match.group(1) if match else text.lstrip("@")
    name = name.lower()
    return name if re.fullmatch(r"[a-z0-9._]{1,30}", name) else None


def _clean(text: str, strip_emoji: bool) -> str:
    kept = []
    for char in unicodedata.normalize("NFC", text):
        category = unicodedata.category(char)
        if char == _ZWNJ:
            kept.append(char)
        elif category in ("Zl", "Zp", "Cc"):
            kept.append(" ")
        elif category in ("Cf", "Cs", "Co", "Cn"):
            continue
        elif strip_emoji and (category in ("So", "Sk") or char in "️︎⃣"):
            continue
        else:
            kept.append(char)
    text = _ILLEGAL.sub(" ", "".join(kept))
    return re.sub(r"\s+", " ", text).strip(_EDGE_JUNK + _ZWNJ)


def extract_title(title: Optional[str], caption: Optional[str], strip_emoji: bool = True) -> str:
    """The post's own title, else the first caption line with real words; '' when there is none."""
    for line in [title or ""] + (caption or "").splitlines():
        cleaned = _clean(_MENTION.sub(r"\1", _HASHTAG.sub(" ", line)), strip_emoji)
        if any(char.isalnum() for char in cleaned):
            return cleaned
    return ""


def _shorten(title: str, limit: int) -> str:
    if len(title) <= limit:
        return title
    cut = title[:limit]
    if " " in cut[limit // 2:]:
        cut = cut[:cut.rfind(" ")]
    return cut.strip(_EDGE_JUNK + _ZWNJ)


def format_date(timestamp: int, date_format: str = "%Y-%m-%d") -> str:
    # '/' cannot appear in a Windows file name, whatever the configured format says.
    return re.sub(r"[/\\]", "-", datetime.fromtimestamp(timestamp).strftime(date_format))


def build_base_name(timestamp: int, title: str, folder: str, date_format: str = "%Y-%m-%d",
                    title_max: int = 100) -> str:
    """'<date> - <title>', with a random 10-digit number standing in for a missing title."""
    date = format_date(timestamp, date_format)
    # Reserve room for the path, the separators, an item number and an extension.
    room = _MAX_PATH - len(os.path.abspath(folder)) - len(date) - 20
    title = _shorten(title, max(20, min(title_max, room)))
    return f"{date} - {title or random.randint(10 ** 9, 10 ** 10 - 1)}"


def extension_from_url(url: str, is_video: bool) -> str:
    extension = os.path.splitext(urlparse(url).path)[1].lower()
    if re.fullmatch(r"\.[a-z0-9]{2,5}", extension):
        return extension
    return ".mp4" if is_video else ".jpg"


def media_file_name(base: str, index: int, count: int, extension: str) -> str:
    """Files of a multi-item post get their position in the post: '... - 01.jpg', '... - 02.mp4'."""
    return f"{base} - {index:02d}{extension}" if count > 1 else f"{base}{extension}"
