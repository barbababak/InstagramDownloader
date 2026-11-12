"""What has been downloaded for one profile; lives inside the profile's folder.

Sync model:
- ``frontier``: timestamp of the newest post that a walk from the top has covered.
  Anything newer is a "new post".
- ``resume.json``: the paging position of the walk towards older posts.
- ``backfill_complete``: that walk has reached the oldest post.
"""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Dict, Optional

from .config import write_json_atomic
from .naming import format_date

STATE_FILE = "_state.json"
RESUME_FILE = "_resume.json"
INDEX_FILE = "_posts.csv"
MAX_ATTEMPTS = 5


class StateError(Exception):
    pass


class ProfileState:
    def __init__(self, folder: Path, username: str):
        self.folder = Path(folder)
        self.username = username
        self.posts: Dict[str, dict] = {}
        self.failed: Dict[str, dict] = {}
        self.reserved: Dict[str, str] = {}  # shortcode -> file base name of a post in progress
        self.frontier: Optional[int] = None
        self.backfill_complete = False
        self.total_posts: Optional[int] = None
        self._load()

    @property
    def path(self) -> Path:
        return self.folder / STATE_FILE

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as file:
                data = json.load(file)
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as error:
            # Starting over silently would download everything again under new names.
            raise StateError(f"فایل وضعیت خراب است: {self.path}\n{error}") from error
        self.posts = data.get("posts", {})
        self.failed = data.get("failed", {})
        self.reserved = data.get("reserved", {})
        self.frontier = data.get("frontier")
        self.backfill_complete = data.get("backfill_complete", False)
        self.total_posts = data.get("total_posts")

    def save(self) -> None:
        write_json_atomic(self.path, {
            "version": 1,
            "username": self.username,
            "frontier": self.frontier,
            "backfill_complete": self.backfill_complete,
            "total_posts": self.total_posts,
            "reserved": self.reserved,
            "failed": self.failed,
            "posts": self.posts,
        })

    def load_resume(self) -> Optional[dict]:
        try:
            with open(self.folder / RESUME_FILE, encoding="utf-8") as file:
                return json.load(file)
        except (OSError, json.JSONDecodeError):
            return None

    def save_resume(self, frozen: dict) -> None:
        write_json_atomic(self.folder / RESUME_FILE, frozen)

    def clear_resume(self) -> None:
        (self.folder / RESUME_FILE).unlink(missing_ok=True)

    def known_timestamp(self, shortcode: str) -> Optional[int]:
        entry = self.posts.get(shortcode) or self.failed.get(shortcode) or {}
        return entry.get("timestamp")

    def is_given_up(self, shortcode: str) -> bool:
        return self.failed.get(shortcode, {}).get("attempts", 0) >= MAX_ATTEMPTS

    def record_failure(self, shortcode: str, error: str, timestamp: Optional[int] = None) -> None:
        entry = self.failed.setdefault(shortcode, {"attempts": 0})
        entry["attempts"] += 1
        entry["error"] = error
        if timestamp is not None:
            entry["timestamp"] = timestamp

    def base_name_taken(self, base: str) -> bool:
        base = base.casefold()
        names = [post["base"] for post in self.posts.values()] + list(self.reserved.values())
        return any(name.casefold() == base for name in names)

    def files_intact(self, shortcode: str) -> bool:
        return all(
            (self.folder / item["name"]).is_file()
            and (self.folder / item["name"]).stat().st_size == item["size"]
            for item in self.posts[shortcode]["files"]
        )

    def totals(self) -> dict:
        files = [item for post in self.posts.values() for item in post["files"]]
        videos = sum(item["is_video"] for item in files)
        return {
            "posts": len(self.posts),
            "total_posts": self.total_posts,
            "images": len(files) - videos,
            "videos": videos,
            "bytes": sum(item["size"] for item in files),
            "failed": len(self.failed),
        }

    def write_index(self) -> None:
        """A readable map from every file to its post, for opening in Excel."""
        rows = sorted(self.posts.items(), key=lambda pair: pair[1]["timestamp"])
        temp = self.folder / (INDEX_FILE + ".tmp")
        with open(temp, "w", encoding="utf-8-sig", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(["date", "link", "position", "type", "file", "size_bytes", "caption"])
            for shortcode, post in rows:
                for position, item in enumerate(post["files"], 1):
                    writer.writerow([
                        format_date(post["timestamp"]), f"https://www.instagram.com/p/{shortcode}/", position,
                        "video" if item["is_video"] else "image", item["name"], item["size"], post["caption"],
                    ])
        try:
            os.replace(temp, self.folder / INDEX_FILE)
        except PermissionError:  # open in Excel; the next run refreshes it
            temp.unlink(missing_ok=True)
