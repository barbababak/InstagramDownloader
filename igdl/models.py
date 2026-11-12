"""Types shared by the sync engine and the Instagram source."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterator, List, Optional, Protocol


class StopRequested(Exception):
    """The user pressed stop."""


class FatalSourceError(Exception):
    """The whole run cannot continue (blocked, login needed, private profile...)."""

    def __init__(self, message: str, needs_login: bool = False):
        super().__init__(message)
        self.needs_login = needs_login


class PostFailed(Exception):
    """A single post could not be read or downloaded; the run can go on."""


class MediaExpired(PostFailed):
    """A media URL was refused; the post has to be fetched again for fresh URLs."""


@dataclass
class MediaItem:
    url: str
    is_video: bool


@dataclass
class PostInfo:
    shortcode: str
    timestamp: int  # UTC seconds
    title: Optional[str]
    caption: str
    media: Callable[[], List[MediaItem]]


@dataclass
class ProfileInfo:
    username: str
    total_posts: Optional[int]  # Instagram does not always report this


class WalkNode(Protocol):
    """A feed entry whose shortcode is known before any per-post request is made."""

    shortcode: str

    def resolve(self) -> PostInfo: ...


class Walker(Protocol):
    """Iterates a profile's feed, newest first (pinned posts come first)."""

    resumed: bool

    def __iter__(self) -> Iterator[WalkNode]: ...

    def freeze(self) -> dict: ...


class Source(Protocol):
    def profile(self, username: str) -> ProfileInfo: ...

    def walk(self, username: str, frozen: Optional[dict] = None) -> Walker: ...

    def fetch(self, shortcode: str) -> PostInfo: ...

    def download(self, url: str, dest: str, progress: Callable[[int, int], None]) -> int: ...

    def pause(self, seconds: float) -> None: ...
