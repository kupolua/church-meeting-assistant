"""The self-update endpoint picks the highest build number and nothing else."""

from pathlib import Path

from church_assistant.web.routes.api_app import APK_NAME, latest_apk


def test_latest_is_the_highest_build_not_the_highest_version_string(tmp_path: Path):
    for name in ("cma-app-0.6.4+16.apk", "cma-app-0.10.0+18.apk", "cma-app-0.9.9+17.apk",
                 "cma-app-0.6.5+17-dev.apk", "notes.txt", "cma-app-1.0.0+99.apk.part"):
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "cma-app-5.0.0+500.apk").mkdir()  # a directory with the right name does not count
    assert latest_apk(tmp_path).name == "cma-app-0.10.0+18.apk"


def test_empty_or_missing_dir(tmp_path: Path):
    assert latest_apk(tmp_path) is None
    assert latest_apk(tmp_path / "nope") is None


def test_only_release_names_are_served():
    assert APK_NAME.match("cma-app-0.6.5+17.apk")
    for bad in ("../.env", "cma-app-0.6.5+17-dev.apk", "cma-app-0.6.5.apk", "cma-app-0.6.5+17.apk/../x"):
        assert not APK_NAME.match(bad)
