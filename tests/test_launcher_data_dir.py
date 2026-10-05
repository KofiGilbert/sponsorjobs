"""The product was renamed Tailor -> SponsorJobs (2026-10). The packaged launcher keeps the
person's data in a per-user folder named after the product, so the rename must NOT strand an
existing install's profile, CVs and API key under the old name: the first launch adopts the
old folder once, and never touches a folder the new version already owns."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "sponsorjobs_launcher", Path(__file__).resolve().parent.parent / "packaging" / "launcher.py")
launcher = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(launcher)


def test_names_follow_the_rename():
    assert launcher.DATA_DIR_NAME == "SponsorJobs"
    assert launcher.LEGACY_DATA_DIR_NAME == "Tailor"


def test_old_tailor_folder_is_adopted_once(tmp_path: Path):
    old = tmp_path / "Tailor"
    (old / "cvs").mkdir(parents=True)
    (old / "credentials.env").write_text("ANTHROPIC_API_KEY=sk-test\n", encoding="utf-8")
    (old / "cvs" / "one.pdf").write_bytes(b"%PDF")

    d = launcher._user_data_dir(tmp_path)

    assert d == tmp_path / "SponsorJobs"
    assert not old.exists(), "the legacy folder must be moved, not copied"
    assert (d / "credentials.env").read_text(encoding="utf-8") == "ANTHROPIC_API_KEY=sk-test\n"
    assert (d / "cvs" / "one.pdf").read_bytes() == b"%PDF"
    # Second launch: nothing left to migrate, same answer.
    assert launcher._user_data_dir(tmp_path) == d
    assert not old.exists()


def test_fresh_install_just_creates_the_new_folder(tmp_path: Path):
    d = launcher._user_data_dir(tmp_path)
    assert d == tmp_path / "SponsorJobs" and d.is_dir()
    assert not (tmp_path / "Tailor").exists()


def test_existing_sponsorjobs_folder_wins_and_old_is_left_alone(tmp_path: Path):
    (tmp_path / "SponsorJobs").mkdir()
    (tmp_path / "SponsorJobs" / "new.txt").write_text("new", encoding="utf-8")
    (tmp_path / "Tailor").mkdir()
    (tmp_path / "Tailor" / "old.txt").write_text("old", encoding="utf-8")

    assert launcher._migrate_legacy_data_dir(tmp_path) is False
    d = launcher._user_data_dir(tmp_path)
    assert (d / "new.txt").read_text(encoding="utf-8") == "new"
    assert not (d / "old.txt").exists()
    assert (tmp_path / "Tailor" / "old.txt").exists()


def test_a_stray_file_or_symlink_named_tailor_is_ignored(tmp_path: Path):
    (tmp_path / "Tailor").write_text("not a folder", encoding="utf-8")
    assert launcher._migrate_legacy_data_dir(tmp_path) is False
    assert launcher._user_data_dir(tmp_path).is_dir()
    assert (tmp_path / "Tailor").read_text(encoding="utf-8") == "not a folder"

    base2 = tmp_path / "b2"
    base2.mkdir()
    target = tmp_path / "elsewhere"
    target.mkdir()
    try:
        (base2 / "Tailor").symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable here")
    assert launcher._migrate_legacy_data_dir(base2) is False
    assert (base2 / "Tailor").is_symlink()
