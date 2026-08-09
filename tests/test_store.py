"""store.py の単体テスト。一時ファイルのSQLiteに対して検証する（標準ライブラリのみ使用）。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from moegi import store


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.db_path = Path(self.tmpdir.name) / "test.db"

    def test_connect_creates_schema(self):
        conn = store.connect(self.db_path)
        self.addCleanup(conn.close)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='cost_records'"
        ).fetchone()
        self.assertIsNotNone(row)

    def test_save_and_query_cost_record(self):
        conn = store.connect(self.db_path)
        self.addCleanup(conn.close)
        run_id = store.new_run_id()
        store.save_cost_record(
            conn,
            run_id=run_id,
            stage="diff",
            backend="local:tanuki",
            tokens_in=100,
            tokens_out=50,
            cost_jpy=0.0,
            latency_ms=1234.5,
            degraded=False,
            resolved_model="tanuki",
            fallback_level=0,
        )
        row = conn.execute(
            "SELECT run_id, stage, degraded, fallback_level FROM cost_records"
        ).fetchone()
        self.assertEqual(row, (run_id, "diff", 0, 0))

    def test_record_from_llm_result(self):
        conn = store.connect(self.db_path)
        self.addCleanup(conn.close)
        run_id = store.new_run_id()
        result = {
            "text": "x",
            "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            "cost_jpy": 1.5,
            "backend": "orca:model-x",
            "degraded": True,
            "latency_ms": 500.0,
            "resolved_model": "model-x-v2",
            "fallback_level": 1,
        }
        store.record_from_llm_result(conn, run_id, "requirement", result)
        row = conn.execute(
            "SELECT tokens_in, tokens_out, cost_jpy, degraded, resolved_model, fallback_level "
            "FROM cost_records"
        ).fetchone()
        self.assertEqual(row, (20, 10, 1.5, 1, "model-x-v2", 1))

    def test_new_run_id_is_unique(self):
        self.assertNotEqual(store.new_run_id(), store.new_run_id())

    def test_save_and_query_cost_record_with_timeout_flag(self):
        # design.md §6.6（v10）: timeoutした呼び出しもCostRecordに残す（timeoutフラグ付き）。
        conn = store.connect(self.db_path)
        self.addCleanup(conn.close)
        run_id = store.new_run_id()
        store.save_cost_record(
            conn,
            run_id=run_id,
            stage="draft",
            backend="local:tanuki",
            tokens_in=0,
            tokens_out=0,
            cost_jpy=0.0,
            latency_ms=60000.0,
            degraded=True,
            resolved_model="tanuki",
            fallback_level=0,
            timeout=True,
        )
        row = conn.execute("SELECT stage, timeout FROM cost_records").fetchone()
        self.assertEqual(row, ("draft", 1))

    def test_record_from_llm_result_persists_timeout_flag(self):
        conn = store.connect(self.db_path)
        self.addCleanup(conn.close)
        run_id = store.new_run_id()
        result = {
            "text": "",
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            "cost_jpy": 0.0,
            "backend": "local:tanuki",
            "degraded": True,
            "timeout": True,
            "latency_ms": 60000.0,
            "resolved_model": "tanuki",
            "fallback_level": 0,
        }
        store.record_from_llm_result(conn, run_id, "draft", result)
        row = conn.execute("SELECT timeout FROM cost_records").fetchone()
        self.assertEqual(row, (1,))

    def test_migrates_existing_db_missing_timeout_column(self):
        # timeout列を持たない旧スキーマのDBに対しても、connect()がALTER TABLEで補う。
        conn = store.connect(self.db_path)
        conn.close()
        legacy_conn = sqlite3.connect(self.db_path)
        legacy_conn.execute("DROP TABLE cost_records")
        legacy_conn.execute(
            """
            CREATE TABLE cost_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                backend TEXT NOT NULL,
                tokens_in INTEGER NOT NULL,
                tokens_out INTEGER NOT NULL,
                cost_jpy REAL NOT NULL,
                latency_ms REAL NOT NULL,
                degraded INTEGER NOT NULL,
                resolved_model TEXT NOT NULL,
                fallback_level INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        legacy_conn.commit()
        legacy_conn.close()

        reconnected = store.connect(self.db_path)
        self.addCleanup(reconnected.close)
        columns = {row[1] for row in reconnected.execute("PRAGMA table_info(cost_records)")}
        self.assertIn("timeout", columns)


if __name__ == "__main__":
    unittest.main()
