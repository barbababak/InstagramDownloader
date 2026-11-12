from datetime import datetime

from igdl import naming


def test_parse_username():
    assert naming.parse_username("https://www.instagram.com/some.profile/") == "some.profile"
    assert naming.parse_username("@Some.Profile") == "some.profile"
    assert naming.parse_username("instagram.com/some.user_1?igsh=abc") == "some.user_1"
    assert naming.parse_username("not a name") is None


def test_title_uses_first_line_without_emoji_or_tags():
    caption = "وان لگ تو لگ+الگو وی+نقدینیگی🥇\n#ترید #فارکس"
    assert naming.extract_title(None, caption) == "وان لگ تو لگ+الگو وی+نقدینیگی"


def test_title_skips_lines_with_only_tags_and_prefers_post_title():
    assert naming.extract_title(None, "#a #b\n🔥\nخط واقعی") == "خط واقعی"
    assert naming.extract_title("عنوان ریلز", "کپشن") == "عنوان ریلز"


def test_mentions_keep_the_name():
    assert naming.extract_title(None, "@billie is a three-time winner") == "billie is a three-time winner"
    assert naming.extract_title(None, "@some.one's advice: go #music") == "some.one's advice go"


def test_no_title():
    assert naming.extract_title(None, "#only #tags 🔥 @") == ""
    assert naming.extract_title(None, None) == ""


def test_title_removes_illegal_characters_and_keeps_zwnj():
    assert naming.extract_title(None, 'می‌خواهم: a/b?‏ "c".') == "می‌خواهم a b c"


def test_base_name_has_date_and_random_number_when_untitled(tmp_path):
    timestamp = int(datetime(2024, 3, 15, 12).timestamp())
    assert naming.build_base_name(timestamp, "عنوان", str(tmp_path)) == "2024-03-15 - عنوان"
    untitled = naming.build_base_name(timestamp, "", str(tmp_path))
    assert untitled.startswith("2024-03-15 - ")
    assert len(untitled.split(" - ")[1]) == 10 and untitled.split(" - ")[1].isdigit()


def test_slash_in_date_format_is_made_legal(tmp_path):
    timestamp = int(datetime(2024, 3, 15, 12).timestamp())
    assert naming.build_base_name(timestamp, "x", str(tmp_path), "%Y/%m/%d") == "2024-03-15 - x"


def test_long_title_is_cut_at_a_word(tmp_path):
    base = naming.build_base_name(0, "کلمه " * 100, str(tmp_path), title_max=50)
    assert len(base.split(" - ", 1)[1]) <= 50
    assert not base.endswith(" ")


def test_media_names_carry_position():
    assert naming.media_file_name("b", 1, 1, ".mp4") == "b.mp4"
    assert naming.media_file_name("b", 2, 3, ".jpg") == "b - 02.jpg"


def test_extension():
    assert naming.extension_from_url("https://x/y/a.WEBP?stp=1", False) == ".webp"
    assert naming.extension_from_url("https://x/y/a", True) == ".mp4"
    assert naming.extension_from_url("https://x/y/a", False) == ".jpg"
