"""Decides which posts to download and in what order, and keeps the state current."""
from __future__ import annotations

import os
import random
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Set

from . import naming
from .config import Settings
from .models import (FatalSourceError, MediaExpired, PostFailed, PostInfo, Source, StopRequested, WalkNode)
from .state import ProfileState

MODE_AUTO = "auto"
MODE_NEW = "new"
MODE_OLD = "old"
MODE_FULL = "full"
MODE_CAPTIONS = "captions"

# Pinned old posts come before the newest post, so a single old post does not mean the
# known part of the feed has been reached. Instagram allows a few pins; this is more than that.
OLD_STREAK_TO_STOP = 6
ANALYZE_LOOKAHEAD = 12
MAX_CONSECUTIVE_FAILURES = 5


class _LimitReached(Exception):
    pass


@dataclass
class Plan:
    first_run: bool
    new_posts: int
    new_posts_more: bool  # there may be more than new_posts
    done: int
    total: Optional[int]
    backfill_complete: bool
    failed: int

    @property
    def remaining_old(self) -> Optional[int]:
        if self.total is None:
            return None
        return max(self.total - self.done - self.new_posts - self.failed, 0)


class SyncEngine:
    def __init__(self, source: Source, state: ProfileState, settings: Settings,
                 emit: Callable[[str, object], None]):
        self.source = source
        self.state = state
        self.settings = settings
        self.emit = emit
        self.session = {"posts": 0, "images": 0, "videos": 0, "bytes": 0}
        self._cache: Dict[str, PostInfo] = {}
        self._attempted: Set[str] = set()
        self._limit = 0
        self._consecutive_failures = 0

    # ---- reporting ----

    def _log(self, text: str) -> None:
        self.emit("log", text)

    def _status(self, text: str) -> None:
        self.emit("status", text)

    def _stats(self) -> None:
        self.emit("stats", {**self.state.totals(), "session": dict(self.session)})

    # ---- public API ----

    def analyze(self) -> Plan:
        """Looks at the first page of the feed to see where things stand."""
        self._status("در حال بررسی پروفایل...")
        profile = self.source.profile(self.state.username)
        if profile.total_posts is not None:
            self.state.total_posts = profile.total_posts
        new_posts = 0
        more = False
        if self.state.frontier is not None:
            streak = seen = 0
            for node in self.source.walk(self.state.username):
                if seen >= ANALYZE_LOOKAHEAD:
                    more = True
                    break
                seen += 1
                timestamp = self._timestamp_of(node)
                if timestamp is None:
                    continue
                if timestamp > self.state.frontier:
                    streak = 0
                    if node.shortcode not in self.state.posts:
                        new_posts += 1
                else:
                    streak += 1
                    if streak >= OLD_STREAK_TO_STOP:
                        break
        plan = Plan(
            first_run=self.state.frontier is None, new_posts=new_posts, new_posts_more=more,
            done=len(self.state.posts), total=self.state.total_posts,
            backfill_complete=self.state.backfill_complete, failed=len(self.state.failed),
        )
        self.state.save()
        self._stats()
        self.emit("plan", plan)
        return plan

    def run(self, mode: str, limit: int = 0) -> str:
        """Returns 'done', 'stopped' or 'limit'. FatalSourceError passes through after saving."""
        self._limit = limit
        try:
            self.analyze()
            if mode == MODE_CAPTIONS:
                self._walk_captions()
            elif mode == MODE_FULL:
                self._walk_full()
            else:
                if mode in (MODE_AUTO, MODE_NEW):
                    self._walk_new()
                self._retry_failed()
                if mode in (MODE_AUTO, MODE_OLD) or self.state.frontier is None:
                    self._walk_backfill()
            return "done"
        except StopRequested:
            self._log("به درخواست شما متوقف شد. دفعهٔ بعد از همین‌جا ادامه می‌یابد.")
            return "stopped"
        except _LimitReached:
            self._log(f"به سقف {limit} پست در این اجرا رسید. دفعهٔ بعد از همین‌جا ادامه می‌یابد.")
            return "limit"
        finally:
            self.state.save()
            self.state.write_index()
            self._stats()

    # ---- walks ----

    def _walk_new(self) -> None:
        """Newest posts down to the frontier. The frontier moves only when the walk completes,
        so an interrupted walk leaves no gap: the next one passes over what it already has."""
        if self.state.frontier is None:
            return
        frontier = newest = self.state.frontier
        streak = 0
        self._log("جست‌وجوی پست‌های جدید...")
        for node in self.source.walk(self.state.username):
            info_time = self._timestamp_of(node)
            if info_time is None:
                continue
            if info_time <= frontier:
                # Unknown old posts belong to the backfill walk, which covers pinned posts too.
                streak += 1
                if streak >= OLD_STREAK_TO_STOP:
                    break
                continue
            streak = 0
            newest = max(newest, info_time)
            if node.shortcode not in self.state.posts and not self.state.is_given_up(node.shortcode):
                self._process(node)
        self.state.frontier = newest
        self.state.save()

    def _walk_backfill(self) -> None:
        """Towards the oldest post, continuing from the saved paging position."""
        if self.state.backfill_complete:
            return
        self._log("ادامهٔ دانلود پست‌های قدیمی‌تر...")
        frozen = self.state.load_resume()
        walker = self.source.walk(self.state.username, frozen)
        if frozen and not walker.resumed:
            self._log("موقعیت قبلی منقضی شده؛ از ابتدا مرور می‌شود و پست‌های موجود نادیده گرفته می‌شوند.")
        try:
            for node in walker:
                known = node.shortcode in self.state.posts or self.state.is_given_up(node.shortcode)
                if not known:
                    self._process(node)
                    self.state.save_resume(walker.freeze())
                self._raise_frontier(self.state.known_timestamp(node.shortcode))
        except (StopRequested, _LimitReached, FatalSourceError):
            self.state.save_resume(walker.freeze())
            raise
        self.state.backfill_complete = True
        self.state.clear_resume()
        self.state.save()
        self._warn_if_short()

    def _walk_full(self) -> None:
        """Passes over the entire feed and restores anything missing, including deleted local files."""
        self._log("بررسی کامل همهٔ پست‌ها و فایل‌های موجود...")
        for entry in self.state.failed.values():
            entry["attempts"] = 0
        for node in self.source.walk(self.state.username):
            shortcode = node.shortcode
            if shortcode in self.state.posts:
                self._raise_frontier(self.state.posts[shortcode]["timestamp"])
                if self.state.files_intact(shortcode):
                    if self.settings.save_captions:
                        self._write_caption_of(shortcode)
                    continue
                self._log(f"فایل‌های پست {shortcode} ناقص است؛ دوباره دانلود می‌شود.")
                entry = self.state.posts.pop(shortcode)
                self.state.reserved[shortcode] = entry["base"]
            self._process(node)
            self._raise_frontier(self.state.known_timestamp(shortcode))
        self.state.backfill_complete = True
        self.state.clear_resume()
        self.state.save()
        self._warn_if_short()

    def _walk_captions(self) -> None:
        """Writes the caption file of every post without downloading any media.
        A post that is not downloaded yet gets its file name reserved, so its media match it later."""
        self._log("ذخیرهٔ کپشن همهٔ پست‌ها، بدون دانلود ویدیو و تصویر...")
        written = 0
        for node in self.source.walk(self.state.username):
            shortcode = node.shortcode
            if shortcode in self.state.posts:
                written += self._write_caption_of(shortcode)
                continue
            if self.state.is_given_up(shortcode):
                continue
            try:
                info = self._resolve(node)
            except PostFailed as error:
                self._note_failure(shortcode, error)
                continue
            self._consecutive_failures = 0
            written += self._write_caption(self._pick_base_name(info), info.caption, info.timestamp)
            low, high = self.settings.delay_min_seconds, self.settings.delay_max_seconds
            self.source.pause(random.uniform(low, max(low, high)))
        self._log(f"{written} فایل کپشن تازه نوشته شد؛ بقیه از قبل وجود داشتند.")

    def _write_caption_of(self, shortcode: str) -> int:
        post = self.state.posts[shortcode]
        return self._write_caption(post["base"], post["caption"], post["timestamp"])

    def _write_caption(self, base: str, caption: str, timestamp: int) -> int:
        """Returns 1 when a file was written. An existing file is left as it is."""
        path = self.state.folder / f"{base}.txt"
        if path.exists():
            return 0
        self._status(f"کپشن: {path.name}")
        temp = path.with_name(path.name + ".part")
        temp.write_text(caption, encoding="utf-8")
        os.replace(temp, path)
        os.utime(path, (timestamp, timestamp))
        return 1

    def _retry_failed(self) -> None:
        pending = [code for code in self.state.failed
                   if code not in self._attempted and not self.state.is_given_up(code)]
        if pending:
            self._log(f"تلاش دوباره برای {len(pending)} پست ناموفق...")
        for shortcode in pending:
            self._download_guarded(shortcode, lambda code=shortcode: self.source.fetch(code))

    def _warn_if_short(self) -> None:
        total, done = self.state.total_posts, len(self.state.posts)
        if total is not None and done + len(self.state.failed) < total:
            self._log(f"اینستاگرام {total} پست اعلام کرده ولی {done} پست پیدا شد. "
                      "ممکن است بقیه حذف یا محدود شده باشند؛ «بررسی کامل» را هم می‌توانید اجرا کنید.")

    # ---- single post ----

    def _raise_frontier(self, timestamp: Optional[int]) -> None:
        if timestamp is not None and (self.state.frontier is None or timestamp > self.state.frontier):
            self.state.frontier = timestamp

    def _timestamp_of(self, node: WalkNode) -> Optional[int]:
        """Without a request when the post is already known. None when the post cannot be read."""
        known = self.state.known_timestamp(node.shortcode)
        if known is not None:
            return known
        if self.state.is_given_up(node.shortcode):
            return None
        try:
            return self._resolve(node).timestamp
        except PostFailed as error:
            self._note_failure(node.shortcode, error)
            return None

    def _resolve(self, node: WalkNode) -> PostInfo:
        info = self._cache.get(node.shortcode)
        if info is None:
            info = self._cache[node.shortcode] = node.resolve()
        return info

    def _process(self, node: WalkNode) -> None:
        if node.shortcode not in self._attempted:
            self._download_guarded(node.shortcode, lambda: self._resolve(node))

    def _download_guarded(self, shortcode: str, resolve: Callable[[], PostInfo]) -> None:
        self._attempted.add(shortcode)
        try:
            info = resolve()
            try:
                self._download_post(info)
            except MediaExpired:
                # Stored pages and old lookups hold links that expire; ask for fresh ones once.
                self._cache.pop(shortcode, None)
                self._download_post(self.source.fetch(shortcode))
        except PostFailed as error:
            self._note_failure(shortcode, error, self._cache.get(shortcode))
            return
        self._consecutive_failures = 0
        self.state.failed.pop(shortcode, None)
        self.state.save()
        self.session["posts"] += 1
        self._stats()
        if self._limit and self.session["posts"] >= self._limit:
            raise _LimitReached()
        low, high = self.settings.delay_min_seconds, self.settings.delay_max_seconds
        self.source.pause(random.uniform(low, max(low, high)))

    def _note_failure(self, shortcode: str, error: Exception, info: Optional[PostInfo] = None) -> None:
        self._attempted.add(shortcode)
        self.state.record_failure(shortcode, str(error), info.timestamp if info else None)
        self.state.save()
        self._log(f"خطا در پست {shortcode}: {error}")
        self._stats()
        self._consecutive_failures += 1
        if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            raise FatalSourceError(
                f"{MAX_CONSECUTIVE_FAILURES} پست پشت سر هم ناموفق بود؛ احتمالاً اینستاگرام موقتاً محدود کرده است. "
                "بعداً دوباره اجرا کنید.")

    def _pick_base_name(self, info: PostInfo) -> str:
        base = self.state.reserved.get(info.shortcode)
        if base:
            return base
        title = naming.extract_title(info.title, info.caption, self.settings.strip_emoji_from_titles)
        base = first = naming.build_base_name(
            info.timestamp, title, str(self.state.folder),
            self.settings.filename_date_format, self.settings.title_max_length)
        number = 2
        while self.state.base_name_taken(base):
            base = f"{first} ({number})"
            number += 1
        self.state.reserved[info.shortcode] = base
        self.state.save()
        return base

    def _download_post(self, info: PostInfo) -> None:
        media = info.media()
        if not media:
            raise PostFailed("پست هیچ رسانه‌ای ندارد")
        base = self._pick_base_name(info)
        if self.settings.save_captions:
            self._write_caption(base, info.caption, info.timestamp)
        files = []
        for index, item in enumerate(media, 1):
            extension = naming.extension_from_url(item.url, item.is_video)
            name = naming.media_file_name(base, index, len(media), extension)
            dest = self.state.folder / name
            label = f"{index}/{len(media)}  {name}"
            self._status(f"دانلود: {label}")
            if dest.is_file():  # finished before an interruption; files only appear once complete
                size = dest.stat().st_size
            else:
                size = self.source.download(
                    item.url, str(dest),
                    lambda done, total, shown=name: self.emit("file_progress", (shown, done, total)))
                os.utime(dest, (info.timestamp, info.timestamp))
            files.append({"name": name, "is_video": item.is_video, "size": size})
            self.session["videos" if item.is_video else "images"] += 1
            self.session["bytes"] += size
        self.state.reserved.pop(info.shortcode, None)
        self.state.posts[info.shortcode] = {
            "timestamp": info.timestamp,
            "base": base,
            "caption": info.caption,
            "files": files,
        }
        self._raise_frontier_after_download(info)
        self._log(f"✓ {base}  ({len(files)} فایل)")
        self.emit("posts", dict(self.state.posts))

    def _raise_frontier_after_download(self, info: PostInfo) -> None:
        # The very first walk starts at the top of the feed, so the newest post it meets is the frontier.
        if self.state.frontier is None:
            self.state.frontier = info.timestamp
