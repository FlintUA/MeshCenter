"""FX1 Phase B: the `files` settings section (auto-accept on, auto-cleanup off by default) and the comment in the projection."""
from api.api_settings import DEFAULT_SETTINGS, normalize_settings


def test_defaults_are_auto_accept_on_and_auto_cleanup_off():
    assert DEFAULT_SETTINGS["files"]["auto_accept"] is True and DEFAULT_SETTINGS["files"]["auto_cleanup_days"] == 0
    files = normalize_settings({})["files"]
    assert (files["auto_accept"], files["auto_cleanup_days"]) == (True, 0)
    assert normalize_settings(None)["files"]["auto_cleanup_days"] == 0


def test_valid_values_are_kept():
    files = normalize_settings({"files": {"auto_accept": False, "auto_cleanup_days": 90}})["files"]
    assert (files["auto_accept"], files["auto_cleanup_days"]) == (False, 90)


def test_invalid_values_fall_back_to_the_safe_default():
    for bad in ({"auto_accept": "yes"}, {"auto_accept": 0}, {"auto_accept": None}):
        assert normalize_settings({"files": bad})["files"]["auto_accept"] is True
    for bad in (7, -1, "x", None, 3650, True, "30days"):
        assert normalize_settings({"files": {"auto_cleanup_days": bad}})["files"]["auto_cleanup_days"] in (0, 1) or bad is True
    garbage = normalize_settings({"files": "garbage"})["files"]
    assert (garbage["auto_accept"], garbage["auto_cleanup_days"]) == (True, 0)
    assert normalize_settings({"files": {"auto_cleanup_days": 7}})["files"]["auto_cleanup_days"] == 0, "only the offered choices"


def test_the_files_sort_is_remembered_per_kind_of_device_and_defaults_to_newest():
    assert normalize_settings({})["files"]["sort_desktop"] == "newest"
    assert normalize_settings({})["files"]["sort_phone"] == "newest"
    files = normalize_settings({"files": {"sort_desktop": "size", "sort_phone": "name"}})["files"]
    assert (files["sort_desktop"], files["sort_phone"]) == ("size", "name")
    for bad in ("", "random", None, 5, "NEWEST"):
        assert normalize_settings({"files": {"sort_desktop": bad, "sort_phone": bad}})["files"]["sort_phone"] == "newest"
    # another setting in the same section does not reset the sort
    assert normalize_settings({"files": {"sort_phone": "oldest", "auto_accept": False}})["files"]["sort_phone"] == "oldest"
