"""Sync scenarios against a fake feed, including the '100 posts, 50 done, 2 new' case."""
import os
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional

import pytest

from igdl.config import Settings
from igdl.engine import MODE_AUTO, MODE_CAPTIONS, MODE_FULL, MODE_NEW, MODE_OLD, SyncEngine
from igdl.models import (FatalSourceError, MediaExpired, MediaItem, PostFailed, PostInfo, ProfileInfo, StopRequested)
from igdl.state import ProfileState

DAY = 86400
BASE_TIME = 1_600_000_000


def make_post(number: int, caption: Optional[str] = None, kinds: str = "v") -> SimpleNamespace:
    return SimpleNamespace(
        shortcode=f"P{number:03d}", timestamp=BASE_TIME + number * DAY,
        caption=f"پست {number}" if caption is None else caption, kinds=kinds, version=0,
    )


class FakeWalker:
    def __init__(self, source, start, resumed):
        self.source, self.position, self.resumed = source, start, resumed

    def __iter__(self):
        while self.position < len(self.source.feed):
            post = self.source.feed[self.position]
            self.position += 1
            yield SimpleNamespace(shortcode=post.shortcode, resolve=lambda post=post: self.source.info(post))

    def freeze(self):
        # Like NodeIterator.freeze(): rewinds one item. Positions count from the feed's end,
        # because new posts at the top must not shift a saved position.
        return {"from_end": len(self.source.feed) - max(self.position - 1, 0)}


class FakeSource:
    def __init__(self, feed: List[SimpleNamespace], report_total=True):
        self.feed = feed  # newest first
        self.report_total = report_total
        self.resolved: List[str] = []
        self.downloaded: List[str] = []
        self.stop_after: Optional[int] = None
        self.fail_download: set = set()
        self.expire_once: set = set()
        self.fatal_after: Optional[int] = None
        self.resume_valid = True

    def profile(self, username):
        return ProfileInfo(username, len(self.feed) if self.report_total else None)

    def walk(self, username, frozen=None):
        if frozen and self.resume_valid:
            return FakeWalker(self, len(self.feed) - frozen["from_end"], True)
        return FakeWalker(self, 0, False)

    def info(self, post):
        self.resolved.append(post.shortcode)
        return PostInfo(post.shortcode, post.timestamp, None, post.caption, lambda: [
            MediaItem(f"https://cdn/{post.shortcode}_{i}.{'mp4' if kind == 'v' else 'jpg'}", kind == "v")
            for i, kind in enumerate(post.kinds)
        ])

    def fetch(self, shortcode):
        return self.info(next(post for post in self.feed if post.shortcode == shortcode))

    def download(self, url, dest, progress):
        key = url.rsplit("/", 1)[1]
        code = key.split("_")[0]
        if self.stop_after is not None and len(self.downloaded) >= self.stop_after:
            raise StopRequested()
        if self.fatal_after is not None and len(self.downloaded) >= self.fatal_after:
            raise FatalSourceError("blocked", needs_login=True)
        if code in self.fail_download:
            raise PostFailed("boom")
        if code in self.expire_once:
            self.expire_once.discard(code)
            raise MediaExpired("expired")
        Path(dest).write_bytes(b"x" * 10)
        progress(10, 10)
        self.downloaded.append(code)
        return 10

    def pause(self, seconds):
        pass


def settings():
    return Settings(delay_min_seconds=0, delay_max_seconds=0)


def run(source, folder, mode=MODE_AUTO, limit=0):
    events = []
    state = ProfileState(folder, "someone")
    engine = SyncEngine(source, state, settings(), lambda kind, data: events.append((kind, data)))
    return engine.run(mode, limit), state, events


def feed_of(count):
    return [make_post(number) for number in range(count, 0, -1)]


def test_first_run_downloads_everything_and_marks_complete(tmp_path):
    source = FakeSource(feed_of(5))
    result, state, _ = run(source, tmp_path)
    assert result == "done"
    assert source.downloaded == ["P005", "P004", "P003", "P002", "P001"]
    assert state.backfill_complete
    assert state.frontier == source.feed[0].timestamp
    assert (tmp_path / "_posts.csv").exists()


def test_second_run_makes_no_per_post_requests(tmp_path):
    source = FakeSource(feed_of(5))
    run(source, tmp_path)
    source.resolved.clear()
    source.downloaded.clear()
    result, _, _ = run(source, tmp_path)
    assert result == "done"
    assert source.resolved == [] and source.downloaded == []


def test_100_posts_50_done_then_2_new(tmp_path):
    """The scenario from the brief: both ends are outstanding after a pause."""
    source = FakeSource(feed_of(100))
    result, state, _ = run(source, tmp_path, limit=50)
    assert result == "limit"
    assert len(state.posts) == 50
    source.feed[:0] = [make_post(102), make_post(101)]
    source.downloaded.clear()

    events = []
    state = ProfileState(tmp_path, "someone")
    engine = SyncEngine(source, state, settings(), lambda kind, data: events.append((kind, data)))
    plan = engine.analyze()
    assert plan.new_posts == 2
    assert plan.remaining_old == 50

    result = engine.run(MODE_AUTO)
    assert result == "done"
    assert source.downloaded[:2] == ["P102", "P101"], "new posts come first"
    assert source.downloaded[2:] == [f"P{n:03d}" for n in range(50, 0, -1)], "then the older half"
    assert len(state.posts) == 102 and state.backfill_complete


def test_only_new_leaves_old_untouched_and_only_old_leaves_new(tmp_path):
    source = FakeSource(feed_of(20))
    run(source, tmp_path, limit=10)
    source.feed[:0] = [make_post(21)]

    source.downloaded.clear()
    _, state, _ = run(source, tmp_path, MODE_NEW)
    assert source.downloaded == ["P021"]
    assert not state.backfill_complete

    source.feed[:0] = [make_post(22)]
    source.downloaded.clear()
    _, state, _ = run(source, tmp_path, MODE_OLD)
    assert "P022" not in source.downloaded
    assert source.downloaded == [f"P{n:03d}" for n in range(10, 0, -1)]
    assert state.backfill_complete

    source.downloaded.clear()
    _, _, _ = run(source, tmp_path, MODE_AUTO)
    assert source.downloaded == ["P022"]


def test_interrupted_new_walk_leaves_no_gap(tmp_path):
    source = FakeSource(feed_of(5))
    run(source, tmp_path)
    source.feed[:0] = [make_post(n) for n in range(9, 5, -1)]
    source.downloaded.clear()
    source.stop_after = 2
    result, state, _ = run(source, tmp_path)
    assert result == "stopped"
    assert source.downloaded == ["P009", "P008"]

    source.stop_after = None
    source.downloaded.clear()
    result, state, _ = run(source, tmp_path)
    assert result == "done"
    assert source.downloaded == ["P007", "P006"]
    assert len(state.posts) == 9


def test_stop_mid_carousel_keeps_finished_files_and_name(tmp_path):
    post = make_post(1, caption="", kinds="ivi")
    source = FakeSource([post])
    source.stop_after = 1
    result, state, _ = run(source, tmp_path)
    assert result == "stopped"
    first = [name for name in os.listdir(tmp_path) if name.endswith(" - 01.jpg")]
    assert len(first) == 1, "random-number name was reserved"

    source.stop_after = None
    source.downloaded.clear()
    result, state, _ = run(source, tmp_path)
    assert result == "done"
    assert len(source.downloaded) == 2, "the finished first item is not downloaded again"
    names = [item["name"] for item in state.posts["P001"]["files"]]
    assert names[0] == first[0]
    assert [name.rsplit(" - ", 1)[1] for name in names] == ["01.jpg", "02.mp4", "03.jpg"]
    assert not state.reserved


def test_mixed_counts_and_file_dates(tmp_path):
    source = FakeSource([make_post(2, kinds="ivv"), make_post(1, kinds="i")])
    _, state, events = run(source, tmp_path)
    totals = state.totals()
    assert (totals["images"], totals["videos"], totals["bytes"]) == (2, 2, 40)
    single = state.posts["P001"]["files"][0]["name"]
    assert int((tmp_path / single).stat().st_mtime) == source.feed[1].timestamp
    assert events[-1][0] == "stats" and events[-1][1]["session"]["posts"] == 2


def test_same_day_same_title_do_not_overwrite(tmp_path):
    first, second = make_post(1, caption="مشابه"), make_post(1, caption="مشابه")
    second.shortcode = "P999"
    _, state, _ = run(FakeSource([second, first]), tmp_path)
    names = sorted(item["name"] for post in state.posts.values() for item in post["files"])
    assert len(set(names)) == 2
    assert any(name.endswith("مشابه (2).mp4") for name in names)


def test_pinned_old_posts_do_not_end_the_new_walk(tmp_path):
    source = FakeSource(feed_of(10))
    run(source, tmp_path)
    source.feed[:0] = [make_post(n) for n in range(13, 10, -1)]
    pinned = [source.feed.pop(), source.feed.pop()]  # the two oldest get pinned to the top
    source.feed[:0] = pinned
    source.downloaded.clear()
    _, state, _ = run(source, tmp_path, MODE_NEW)
    assert source.downloaded == ["P013", "P012", "P011"]


def test_failed_post_is_retried_on_next_run_without_blocking_frontier(tmp_path):
    source = FakeSource(feed_of(3))
    source.fail_download = {"P002"}
    result, state, _ = run(source, tmp_path)
    assert result == "done"
    assert "P002" in state.failed and state.failed["P002"]["attempts"] == 1
    assert state.frontier == source.feed[0].timestamp

    source.fail_download = set()
    source.downloaded.clear()
    _, state, _ = run(source, tmp_path)
    assert source.downloaded == ["P002"]
    assert not state.failed


def test_expired_links_are_refreshed_once(tmp_path):
    source = FakeSource(feed_of(1))
    source.expire_once = {"P001"}
    _, state, _ = run(source, tmp_path)
    assert "P001" in state.posts and not state.failed


def test_fatal_error_saves_progress_for_resume(tmp_path):
    source = FakeSource(feed_of(6))
    source.fatal_after = 3
    with pytest.raises(FatalSourceError):
        run(source, tmp_path)
    assert len(ProfileState(tmp_path, "someone").posts) == 3

    source.fatal_after = None
    source.downloaded.clear()
    source.resolved.clear()
    result, state, _ = run(source, tmp_path)
    assert result == "done"
    assert source.downloaded == ["P003", "P002", "P001"]
    assert len(state.posts) == 6


def test_expired_resume_position_falls_back_without_redownloading(tmp_path):
    source = FakeSource(feed_of(6))
    run(source, tmp_path, limit=3)
    source.resume_valid = False
    source.downloaded.clear()
    source.resolved.clear()
    result, state, events = run(source, tmp_path)
    assert result == "done"
    assert source.downloaded == ["P003", "P002", "P001"]
    assert source.resolved == ["P003", "P002", "P001"], "known posts cost no request"
    assert any("منقضی" in str(data) for kind, data in events if kind == "log")


def test_full_check_restores_deleted_file_under_same_name(tmp_path):
    source = FakeSource(feed_of(3))
    _, state, _ = run(source, tmp_path)
    name = state.posts["P002"]["files"][0]["name"]
    (tmp_path / name).unlink()

    source.downloaded.clear()
    _, state, _ = run(source, tmp_path)
    assert source.downloaded == [], "a normal run leaves deleted files alone"

    _, state, _ = run(source, tmp_path, MODE_FULL)
    assert source.downloaded == ["P002"]
    assert (tmp_path / name).exists()


def test_stop_during_pause_between_posts_is_a_clean_stop(tmp_path):
    source = FakeSource(feed_of(3))

    def stop_now(seconds):
        raise StopRequested()

    source.pause = stop_now
    result, state, _ = run(source, tmp_path)
    assert result == "stopped"
    assert list(state.posts) == ["P003"]
    assert (tmp_path / "_resume.json").exists()


def test_repeated_failures_abort_the_run(tmp_path):
    source = FakeSource(feed_of(10))
    source.fail_download = {post.shortcode for post in source.feed}
    with pytest.raises(FatalSourceError):
        run(source, tmp_path)
    assert len(ProfileState(tmp_path, "someone").failed) == 5


def test_caption_file_is_written_with_the_media(tmp_path):
    source = FakeSource([make_post(2, caption="خط اول\n#تگ", kinds="iv"), make_post(1, caption="")])
    _, state, _ = run(source, tmp_path)
    base = state.posts["P002"]["base"]
    assert (tmp_path / f"{base}.txt").read_text(encoding="utf-8") == "خط اول\n#تگ"
    assert (tmp_path / f"{base} - 01.jpg").exists()
    assert int((tmp_path / f"{base}.txt").stat().st_mtime) == source.feed[0].timestamp
    assert (tmp_path / f"{state.posts['P001']['base']}.txt").read_text(encoding="utf-8") == ""


def test_captions_only_mode_downloads_no_media_and_keeps_names_for_later(tmp_path):
    source = FakeSource(feed_of(4))
    run(source, tmp_path, limit=2)
    for txt in tmp_path.glob("*.txt"):
        txt.unlink()  # as for posts downloaded before caption files existed
    source.downloaded.clear()
    result, state, _ = run(source, tmp_path, MODE_CAPTIONS)
    assert result == "done"
    assert source.downloaded == []
    assert len(list(tmp_path.glob("*.txt"))) == 4
    assert len(list(tmp_path.glob("*.mp4"))) == 2, "only the two earlier videos exist"
    reserved = dict(state.reserved)
    assert set(reserved) == {"P002", "P001"}

    _, state, _ = run(source, tmp_path, MODE_OLD)
    assert source.downloaded == ["P002", "P001"]
    for code, base in reserved.items():
        assert state.posts[code]["base"] == base
        assert (tmp_path / f"{base}.mp4").exists() and (tmp_path / f"{base}.txt").exists()
    assert len(list(tmp_path.glob("*.txt"))) == 4


def test_captions_are_optional(tmp_path):
    source = FakeSource(feed_of(1))
    state = ProfileState(tmp_path, "someone")
    SyncEngine(source, state, Settings(delay_min_seconds=0, delay_max_seconds=0, save_captions=False),
               lambda kind, data: None).run(MODE_AUTO)
    assert not list(tmp_path.glob("*.txt"))
