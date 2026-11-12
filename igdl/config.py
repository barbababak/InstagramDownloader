"""Application settings (settings.json next to the app) and the login session file."""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import List, Optional

_REPLACE_ATTEMPTS = 25
FROZEN = getattr(sys, "frozen", False)
# Inside the .exe, __file__ points at a temporary unpack folder that disappears on exit.
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent.parent
ICON_PATH = (Path(sys._MEIPASS) if FROZEN else APP_DIR) / "assets" / "icon.ico"
SETTINGS_PATH = APP_DIR / "settings.json"
# Login cookies are as sensitive as a password, so they stay out of settings.json and the project folder.
SESSION_PATH = Path(os.environ.get("APPDATA", Path.home())) / "InstagramDownloader" / "session.json"


def write_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with open(temp, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(temp, path)
            return
        except PermissionError:  # Windows: briefly held open by an antivirus, indexer or backup tool
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(0.2)


@dataclass
class Settings:
    download_root: str = str(Path.home() / "Downloads" / "Instagram-Movies")
    profiles: List[str] = field(default_factory=list)
    last_profile: str = ""
    sync_mode: str = "auto"
    max_posts_per_run: int = 0  # 0 = no limit
    delay_min_seconds: float = 2.0
    delay_max_seconds: float = 6.0
    filename_date_format: str = "%Y-%m-%d"
    title_max_length: int = 100
    strip_emoji_from_titles: bool = True
    save_captions: bool = True  # a .txt with the full caption next to each post's media
    login_browser: str = "firefox"

    @classmethod
    def load(cls, path: Path = SETTINGS_PATH) -> "Settings":
        known = {item.name for item in fields(cls)}
        try:
            with open(path, encoding="utf-8") as file:
                settings = cls(**{key: value for key, value in json.load(file).items() if key in known})
        except FileNotFoundError:
            settings = cls()
        except (json.JSONDecodeError, TypeError, AttributeError):
            os.replace(path, path.with_name(path.name + ".bad"))
            settings = cls()
        settings.save(path)
        return settings

    def save(self, path: Path = SETTINGS_PATH) -> None:
        write_json_atomic(path, asdict(self))


def load_session() -> Optional[dict]:
    try:
        with open(SESSION_PATH, encoding="utf-8") as file:
            session = json.load(file)
    except (OSError, json.JSONDecodeError):
        return None
    return session if session.get("username") and session.get("cookies") else None


def save_session(username: str, cookies: dict) -> None:
    write_json_atomic(SESSION_PATH, {"username": username, "cookies": cookies})


def clear_session() -> None:
    SESSION_PATH.unlink(missing_ok=True)
