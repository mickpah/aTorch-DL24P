"""Tests for the tests.db schema migration framework (PRAGMA user_version)."""

import sqlite3

import pytest

from load_test_bench.data.database import Database

# Verbatim v0 schema, for building a pre-migration fixture database.
V0_SESSIONS = """
    CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        start_time TEXT NOT NULL,
        end_time TEXT,
        battery_name TEXT,
        battery_capacity_mah REAL,
        notes TEXT,
        test_type TEXT,
        settings TEXT
    )
"""
V0_READINGS = """
    CREATE TABLE IF NOT EXISTS readings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        timestamp TEXT NOT NULL,
        voltage REAL NOT NULL,
        current REAL NOT NULL,
        power REAL NOT NULL,
        energy_wh REAL NOT NULL,
        capacity_mah REAL NOT NULL,
        temperature_c INTEGER NOT NULL,
        ext_temperature_c INTEGER,
        fan_speed_rpm INTEGER DEFAULT 0,
        load_r_ohm REAL,
        battery_r_ohm REAL,
        runtime_seconds INTEGER NOT NULL,
        FOREIGN KEY (session_id) REFERENCES sessions (id)
    )
"""


def make_v0_db(path):
    """Hand-build a pre-migration database as deployed installs have it."""
    conn = sqlite3.connect(str(path))
    conn.execute(V0_SESSIONS)
    conn.execute(V0_READINGS)
    conn.execute(
        "INSERT INTO sessions (name, start_time, end_time) VALUES (?, ?, ?)",
        ("finished run", "2026-01-01T10:00:00", "2026-01-01T11:00:00"),
    )
    conn.execute(
        "INSERT INTO sessions (name, start_time, end_time) VALUES (?, ?, NULL)",
        ("crashed run", "2026-01-02T10:00:00"),
    )
    conn.commit()
    conn.close()


def table_names(db):
    rows = db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {row[0] for row in rows}


def column_names(db, table):
    return {row[1] for row in db._conn.execute(f"PRAGMA table_info({table})")}


class TestMigrations:
    def test_fresh_database_reaches_version_1(self, tmp_path):
        """A brand-new database gets the full current schema."""
        db = Database(tmp_path / "tests.db")
        assert db._conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert {"jobs", "job_phases", "scheduled_jobs"} <= table_names(db)
        assert {"status", "job_phase_id"} <= column_names(db, "sessions")
        assert "aux_voltage_v" in column_names(db, "readings")
        db.close()

    def test_v0_database_migrates_in_place(self, tmp_path):
        """A deployed v0 database gains the new tables/columns with data intact."""
        path = tmp_path / "tests.db"
        make_v0_db(path)
        db = Database(path)
        assert db._conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert {"jobs", "job_phases", "scheduled_jobs"} <= table_names(db)
        rows = db._conn.execute(
            "SELECT name, status FROM sessions ORDER BY id"
        ).fetchall()
        assert [tuple(r) for r in rows] == [
            ("finished run", "completed"),
            ("crashed run", "completed"),  # backfill default; recovery flips orphans
        ]
        db.close()

    def test_reopening_is_idempotent(self, tmp_path):
        """Opening an already-migrated database runs no migration twice."""
        path = tmp_path / "tests.db"
        Database(path).close()
        db = Database(path)
        assert db._conn.execute("PRAGMA user_version").fetchone()[0] == 1
        db.close()

    def test_failed_migration_rolls_back_completely(self, tmp_path):
        """A mid-migration failure must leave no partial schema behind, so
        reopening retries cleanly instead of crashing forever."""
        from load_test_bench.data import database as db_module

        def bad_migration(conn):
            conn.execute("CREATE TABLE half_done (id INTEGER)")
            raise RuntimeError("boom")

        db_module._MIGRATIONS.append(bad_migration)
        try:
            with pytest.raises(RuntimeError):
                Database(tmp_path / "tests.db")
        finally:
            db_module._MIGRATIONS.pop()
        db = Database(tmp_path / "tests.db")
        assert db._conn.execute("PRAGMA user_version").fetchone()[0] == 1
        names = {
            row[0]
            for row in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "half_done" not in names
        assert "jobs" in names
        db.close()


class TestSessionStateHelpers:
    def test_find_open_session_ids(self, tmp_path):
        path = tmp_path / "tests.db"
        make_v0_db(path)
        db = Database(path)
        open_ids = db.find_open_session_ids()
        assert len(open_ids) == 1
        db.close()

    def test_close_session_as_interrupted_uses_last_reading_time(self, tmp_path):
        path = tmp_path / "tests.db"
        make_v0_db(path)
        conn = sqlite3.connect(str(path))
        conn.execute(
            """INSERT INTO readings (session_id, timestamp, voltage, current, power,
               energy_wh, capacity_mah, temperature_c, runtime_seconds)
               VALUES (2, '2026-01-02T10:30:00', 3.7, 1.0, 3.7, 1.0, 500, 30, 1800)"""
        )
        conn.commit()
        conn.close()
        db = Database(path)
        (session_id,) = db.find_open_session_ids()
        db.close_session_as_interrupted(session_id)
        row = db._conn.execute(
            "SELECT end_time, status FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        assert row[0] == "2026-01-02T10:30:00"
        assert row[1] == "interrupted"
        assert db.find_open_session_ids() == []
        db.close()

    def test_close_session_without_readings_falls_back_to_start_time(self, tmp_path):
        path = tmp_path / "tests.db"
        make_v0_db(path)
        db = Database(path)
        (session_id,) = db.find_open_session_ids()
        db.close_session_as_interrupted(session_id)
        row = db._conn.execute(
            "SELECT end_time FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        assert row[0] == "2026-01-02T10:00:00"
        db.close()

    def test_set_status_and_link_phase(self, tmp_path):
        db = Database(tmp_path / "tests.db")
        db._conn.execute(
            "INSERT INTO sessions (name, start_time) VALUES ('s', '2026-01-01T00:00:00')"
        )
        db._conn.commit()
        db.set_session_status(1, "faulted")
        db.link_session_to_phase(1, 42)
        row = db._conn.execute(
            "SELECT status, job_phase_id FROM sessions WHERE id = 1"
        ).fetchone()
        assert tuple(row) == ("faulted", 42)
        db.close()
