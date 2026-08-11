"""store.py の単体テスト。一時ファイルのSQLiteに対して検証する（標準ライブラリのみ使用）。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from moegi import pipeline, store
from moegi.ingest import Section


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


class SavePipelineResultTest(unittest.TestCase):
    """design.md §6.4・D4「承認UI」向けのChange/Impact/Proposal永続化。"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.conn = store.connect(Path(self.tmpdir.name) / "test.db")
        self.addCleanup(self.conn.close)
        self.run_id = store.new_run_id()

    def _sample_result(self) -> dict:
        change = pipeline.Change(
            id="c",
            old_section=None,
            new_section=Section(notice_id="n", path="第4の4", text="評価を行うこと。", offset=0),
            kind="追加",
            summary="評価の記録が新設された",
        )
        impact = pipeline.Impact(
            id="impact-c-new",
            change_id="c",
            sitedoc_item_id=None,
            verdict="要更新",
            rationale="評価を記録する項目が無い",
            confidence=0.9,
            evidence=[pipeline.Evidence(notice_section_path="第4の4", quote="評価を行うこと。", offset=0)],
        )
        proposal = pipeline.Proposal(
            id="proposal-impact-c-new", impact_id="impact-c-new", before="", after="評価欄を新設する。"
        )
        return {"changes": [change], "requirements": [], "impacts": [impact], "proposals": [proposal]}

    def test_save_and_list_for_review(self):
        store.save_pipeline_result(self.conn, self.run_id, self._sample_result())
        rows = store.list_proposals_for_review(self.conn, self.run_id)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["after"], "評価欄を新設する。")
        self.assertEqual(row["status"], "未承認")
        self.assertEqual(row["verdict"], "要更新")
        self.assertEqual(row["change_label"], "c")
        self.assertEqual(row["change_kind"], "追加")
        self.assertEqual(row["evidence_quote"], "評価を行うこと。")
        self.assertEqual(row["new_text"], "評価を行うこと。")
        self.assertIsNone(row["old_text"])  # 完全新設

    def test_list_for_review_scopes_by_run_id(self):
        store.save_pipeline_result(self.conn, self.run_id, self._sample_result())
        other_run = store.new_run_id()
        self.assertEqual(store.list_proposals_for_review(self.conn, other_run), [])

    def test_update_proposal_status_to_approved(self):
        store.save_pipeline_result(self.conn, self.run_id, self._sample_result())
        row_id = store.list_proposals_for_review(self.conn, self.run_id)[0]["row_id"]
        store.update_proposal_status(self.conn, row_id, "承認")
        row = store.get_proposal_for_review(self.conn, row_id)
        self.assertEqual(row["status"], "承認")

    def test_update_proposal_status_with_edit_changes_after_text(self):
        store.save_pipeline_result(self.conn, self.run_id, self._sample_result())
        row_id = store.list_proposals_for_review(self.conn, self.run_id)[0]["row_id"]
        store.update_proposal_status(self.conn, row_id, "編集済", after="評価欄（達成度・効果）を新設する。")
        row = store.get_proposal_for_review(self.conn, row_id)
        self.assertEqual(row["status"], "編集済")
        self.assertEqual(row["after"], "評価欄（達成度・効果）を新設する。")

    def test_get_proposal_for_review_returns_none_when_missing(self):
        self.assertIsNone(store.get_proposal_for_review(self.conn, 9999))

    def test_handles_impact_without_evidence(self):
        result = self._sample_result()
        result["impacts"][0].evidence = []
        store.save_pipeline_result(self.conn, self.run_id, result)
        row = store.list_proposals_for_review(self.conn, self.run_id)[0]
        self.assertIsNone(row["evidence_quote"])


class FaxDeliveryTest(unittest.TestCase):
    """design.md §5.3・ledger R-76: FAX配信履歴（監査ログ）の永続化。"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.conn = store.connect(Path(self.tmpdir.name) / "test.db")
        self.addCleanup(self.conn.close)
        self.run_id = store.new_run_id()

    def test_save_and_list_fax_delivery(self):
        store.save_fax_delivery(
            self.conn, run_id=self.run_id, proposal_row_ids=[1, 2, 3], file_path="/tmp/x.pdf"
        )
        deliveries = store.list_fax_deliveries(self.conn, self.run_id)
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["proposal_row_ids"], [1, 2, 3])
        self.assertEqual(deliveries[0]["file_path"], "/tmp/x.pdf")
        self.assertIsNotNone(deliveries[0]["generated_at"])

    def test_list_fax_deliveries_orders_newest_first(self):
        store.save_fax_delivery(self.conn, run_id=self.run_id, proposal_row_ids=[1], file_path="/tmp/a.pdf")
        store.save_fax_delivery(self.conn, run_id=self.run_id, proposal_row_ids=[1, 2], file_path="/tmp/b.pdf")
        deliveries = store.list_fax_deliveries(self.conn, self.run_id)
        self.assertEqual([d["file_path"] for d in deliveries], ["/tmp/b.pdf", "/tmp/a.pdf"])

    def test_list_fax_deliveries_scopes_by_run_id(self):
        store.save_fax_delivery(self.conn, run_id=self.run_id, proposal_row_ids=[1], file_path="/tmp/a.pdf")
        other_run = store.new_run_id()
        self.assertEqual(store.list_fax_deliveries(self.conn, other_run), [])


if __name__ == "__main__":
    unittest.main()
