import os

import pytest

from jobbot.__main__ import open_storage
from jobbot.storage import Storage, StorageError


def test_creates_missing_parent_folders(tmp_path):
    path = tmp_path / "a" / "b" / "jobbot.sqlite3"
    Storage(str(path)).close()
    assert path.exists()


def test_unopenable_path_raises_clear_error(tmp_path):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")  # a file where a folder is expected
    with pytest.raises(StorageError, match="Can't open the database") as exc:
        Storage(str(blocker / "jobbot.sqlite3"))
    assert "DATABASE_PATH" in str(exc.value)


def test_open_storage_falls_back_to_temp(tmp_path, monkeypatch, caplog):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    storage = open_storage(str(blocker / "jobbot.sqlite3"))
    storage.close()
    assert os.path.exists(tmp_path / "jobbot.sqlite3")
    assert "Falling back" in caplog.text
