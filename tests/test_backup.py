"""Backup job: consistent snapshot, gzip output, retention pruning."""

import gzip
import sqlite3

from poghiamo.services import backup as backup_mod


def _make_source_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(5)])
    conn.commit()
    conn.close()


def test_backup_creates_valid_snapshot(tmp_path, monkeypatch):
    src = tmp_path / "src.db"
    _make_source_db(src)
    monkeypatch.setattr(backup_mod, "DATABASE_URL", f"sqlite:///{src}")

    out = backup_mod.backup_database(backup_dir=tmp_path / "bk", keep=14)
    assert out is not None and out.exists()

    # The gunzipped file is a valid SQLite DB with the data
    raw = tmp_path / "restored.db"
    raw.write_bytes(gzip.decompress(out.read_bytes()))
    conn = sqlite3.connect(raw)
    assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 5
    conn.close()


def test_backup_skips_in_memory_db(tmp_path, monkeypatch):
    monkeypatch.setattr(backup_mod, "DATABASE_URL", "sqlite://")
    assert backup_mod.backup_database(backup_dir=tmp_path) is None


def test_backup_survives_stale_tmp_from_killed_run(tmp_path, monkeypatch):
    """Regression: a leftover temp file from a killed run must not wedge backups."""
    src = tmp_path / "src.db"
    _make_source_db(src)
    monkeypatch.setattr(backup_mod, "DATABASE_URL", f"sqlite:///{src}")

    bk = tmp_path / "bk"
    bk.mkdir()
    (bk / ".backup-in-progress-999.db").write_bytes(b"this is not a sqlite database")

    out = backup_mod.backup_database(backup_dir=bk, keep=14)
    assert out is not None and out.exists()
    assert not list(bk.glob(".backup-in-progress*"))


def test_backup_rotation(tmp_path, monkeypatch):
    src = tmp_path / "src.db"
    _make_source_db(src)
    monkeypatch.setattr(backup_mod, "DATABASE_URL", f"sqlite:///{src}")

    bk = tmp_path / "bk"
    bk.mkdir()
    for day in ("2020-01-01", "2020-01-02", "2020-01-03"):
        (bk / f"poghiamo-{day}.db.gz").write_bytes(b"old")

    backup_mod.backup_database(backup_dir=bk, keep=2)

    remaining = sorted(p.name for p in bk.glob("poghiamo-*.db.gz"))
    assert len(remaining) == 2
    # The newest two survive: today's backup and the most recent fake
    assert remaining[0] == "poghiamo-2020-01-03.db.gz"
    assert remaining[1].startswith("poghiamo-2")
