"""Instagram access through instaloader, translated into the engine's small vocabulary."""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterator, List, Optional

import instaloader
import requests
from instaloader.exceptions import (BadResponseException, ConnectionException,
                                    InstaloaderException, InvalidArgumentException, LoginRequiredException,
                                    PostChangedException, PrivateProfileNotFollowedException,
                                    ProfileNotExistsException, QueryReturnedForbiddenException,
                                    QueryReturnedNotFoundException, TooManyRequestsException)
from instaloader.nodeiterator import FrozenNodeIterator

from .models import (FatalSourceError, MediaExpired, MediaItem, PostFailed, PostInfo, ProfileInfo, StopRequested)

_CHUNK = 256 * 1024
_DOWNLOAD_ATTEMPTS = 3


def _as_fatal(error: Exception) -> FatalSourceError:
    """Maps an instaloader error that ends the whole run."""
    if isinstance(error, ProfileNotExistsException):
        return FatalSourceError("این پروفایل پیدا نشد.")
    if isinstance(error, PrivateProfileNotFollowedException):
        return FatalSourceError("این پروفایل خصوصی است و حساب واردشده آن را دنبال نمی‌کند.")
    if isinstance(error, LoginRequiredException):
        return FatalSourceError("اینستاگرام برای این کار ورود به حساب می‌خواهد.", needs_login=True)
    if isinstance(error, TooManyRequestsException):
        return FatalSourceError("اینستاگرام تعداد درخواست‌ها را محدود کرده است. بعداً دوباره اجرا کنید.")
    return FatalSourceError(
        f"اینستاگرام درخواست را نپذیرفت؛ ممکن است لازم باشد وارد حساب شوید یا کمی صبر کنید.\n{error}",
        needs_login=True)


class _RateController(instaloader.RateController):
    def __init__(self, context, source: "InstagramSource"):
        super().__init__(context)
        self._source = source

    def sleep(self, secs: float) -> None:
        self._source.pause(secs, "رعایت محدودیت اینستاگرام")


@dataclass
class _Node:
    shortcode: str
    resolve: Callable[[], PostInfo]


class _Walker:
    def __init__(self, source: "InstagramSource", iterator, resumed: bool):
        self.resumed = resumed
        self._source = source
        self._iterator = iterator
        # Logged-out feeds cost one request per wrapped post. Keeping the raw nodes means
        # posts that are already downloaded can be passed over without that request.
        self._wrap = iterator._node_wrapper
        iterator._node_wrapper = lambda node: node
        iterator._is_first = None

    def __iter__(self) -> Iterator[_Node]:
        while True:
            try:
                raw = next(self._iterator)
            except StopIteration:
                return
            except (InstaloaderException, KeyError) as error:
                raise _as_fatal(error) from error
            shortcode = raw.get("code") or raw.get("shortcode")
            if shortcode:
                yield _Node(shortcode, lambda raw=raw: self._source.resolve(lambda: self._wrap(raw)))

    def freeze(self) -> dict:
        return self._iterator.freeze()._asdict()


class InstagramSource:
    def __init__(self, session: Optional[dict] = None,
                 stop: Optional[threading.Event] = None,
                 on_wait: Callable[[str], None] = lambda text: None,
                 on_message: Callable[[str], None] = lambda text: None):
        self._stop = stop or threading.Event()
        self._on_wait = on_wait
        self._loader = instaloader.Instaloader(
            quiet=True, max_connection_attempts=3, request_timeout=60,
            rate_controller=lambda context: _RateController(context, self))
        self._context = self._loader.context
        # By default errors go to stderr, which a windowed app does not have.
        self._context.error = lambda message, repeat_at_end=True: on_message(str(message))
        if session:
            self._context.load_session(session["username"], session["cookies"])

    # ---- login ----

    def verify_login(self) -> Optional[str]:
        return self._context.test_login()

    # ---- pacing ----

    def _check_stop(self) -> None:
        if self._stop.is_set():
            raise StopRequested()

    def pause(self, seconds: float, reason: str = "مکث بین پست‌ها") -> None:
        """Waits so that stop stays responsive. StopRequested passes through instaloader's handlers unharmed."""
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            if seconds > 10:
                self._on_wait(f"{reason}: {int(left // 60)}:{int(left % 60):02d} مانده")
            if self._stop.wait(min(left, 1.0)):
                raise StopRequested()
        if self._stop.is_set():
            raise StopRequested()

    # ---- reading ----

    def profile(self, username: str) -> ProfileInfo:
        try:
            profile = instaloader.Profile.from_username(self._context, username)
            return ProfileInfo(profile.username, profile.mediacount)
        except (InstaloaderException, KeyError) as error:
            raise _as_fatal(error) from error

    def walk(self, username: str, frozen: Optional[dict] = None) -> _Walker:
        try:
            iterator = instaloader.Profile.from_username(self._context, username).get_posts()
        except (InstaloaderException, KeyError) as error:
            raise _as_fatal(error) from error
        resumed = False
        if frozen:
            try:
                saved = FrozenNodeIterator(**frozen)
                if saved.best_before and saved.best_before > datetime.now().timestamp():
                    iterator.thaw(saved)
                    resumed = True
            except (InvalidArgumentException, TypeError):
                pass
        return _Walker(self, iterator, resumed)

    def fetch(self, shortcode: str) -> PostInfo:
        return self.resolve(lambda: instaloader.Post.from_shortcode(self._context, shortcode))

    def resolve(self, make_post: Callable[[], instaloader.Post]) -> PostInfo:
        try:
            return self._to_info(make_post())
        except (QueryReturnedNotFoundException, BadResponseException, PostChangedException,
                KeyError, TypeError, IndexError) as error:
            raise PostFailed(f"پست خوانده نشد: {error}") from error
        except InstaloaderException as error:
            raise _as_fatal(error) from error

    def _to_info(self, post: instaloader.Post) -> PostInfo:
        def media() -> List[MediaItem]:
            try:
                return self._media_of(post)
            except (QueryReturnedNotFoundException, BadResponseException, PostChangedException,
                    KeyError, TypeError, IndexError) as error:
                raise PostFailed(f"نشانی رسانه‌ها خوانده نشد: {error}") from error
            except InstaloaderException as error:
                raise _as_fatal(error) from error

        return PostInfo(
            shortcode=post.shortcode,
            timestamp=int(post.date_utc.replace(tzinfo=timezone.utc).timestamp()),
            # Post.title would make an extra request whenever the feed entry lacks the field.
            title=post._node.get("title"),
            caption=post.caption or "",
            media=media,
        )

    @staticmethod
    def _media_of(post: instaloader.Post) -> List[MediaItem]:
        if post.typename == "GraphSidecar":
            return [MediaItem(node.video_url if node.is_video else node.display_url, node.is_video)
                    for node in post.get_sidecar_nodes()]
        if post.is_video:
            if not post.video_url:
                raise PostFailed("نشانی ویدیو در دسترس نیست")
            return [MediaItem(post.video_url, True)]
        return [MediaItem(post.url, False)]

    # ---- downloading ----

    def download(self, url: str, dest: str, progress: Callable[[int, int], None]) -> int:
        """Writes to '<dest>.part' first, so a file under its final name is always complete."""
        temp = dest + ".part"
        last_error: Optional[Exception] = None
        for attempt in range(_DOWNLOAD_ATTEMPTS):
            if attempt:
                self.pause(2 ** attempt * 3, "تلاش دوباره")
            try:
                written = self._stream(url, temp, progress)
                os.replace(temp, dest)
                return written
            except (QueryReturnedForbiddenException, QueryReturnedNotFoundException) as error:
                raise MediaExpired(f"لینک رسانه پذیرفته نشد: {error}") from error
            except (ConnectionException, requests.RequestException) as error:
                last_error = error
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
        raise PostFailed(f"دانلود فایل ناموفق بود: {last_error}")

    def _stream(self, url: str, temp: str, progress: Callable[[int, int], None]) -> int:
        self._check_stop()
        with self._context.get_raw(url) as response:
            total = int(response.headers.get("Content-Length") or 0)
            written = 0
            with open(temp, "wb") as file:
                for chunk in response.iter_content(_CHUNK):
                    self._check_stop()
                    file.write(chunk)
                    written += len(chunk)
                    progress(written, total)
        if total and written < total:
            raise ConnectionException(f"ناقص دریافت شد ({written} از {total} بایت)")
        return written
