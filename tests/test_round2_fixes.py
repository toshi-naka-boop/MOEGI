"""採点指摘（rubric v1 r2）への修正の回帰テスト。合成データのみ・LLM/ネットワーク不使用。"""

from __future__ import annotations

import os
import smtplib
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from moegi import checklist, delivery, demo, nippou, pipeline, privacy, store, webapp
from tests.test_review_fixes import _seed, _WebCase


class ApprovalIdentifierTest(_WebCase):
    def test_edit_with_identifier_is_refused_and_changes_nothing(self):
        row = _seed(self.conn)
        r = self.client.post(f"/proposals/{row}/edit", data={"after": "山田花子 090-1111-2222 に連絡"})
        self.assertIn("notice=edit_refused", r.headers["location"])
        status, after = self.conn.execute("SELECT status, after FROM proposals WHERE row_id = ?", (row,)).fetchone()
        self.assertEqual(status, "未承認")
        self.assertNotIn("090", after)
        self.assertEqual(store.list_applied_proposals(self.conn), [])
        self.assertEqual(store.count_pending_deliveries(self.conn), 0)

    def test_edited_proposal_has_no_approve_button_and_reapprove_is_idempotent(self):
        row = _seed(self.conn)
        self.client.post(f"/proposals/{row}/edit", data={"after": "直した文言"})
        page = self.client.get("/runs/run-t").text
        self.assertIn("編集して承認済み", page)
        self.assertNotIn(f"/proposals/{row}/approve", page)
        for _ in range(3):
            r = self.client.post(f"/proposals/{row}/approve")
        self.assertIn("already_approved", r.headers["location"])
        self.assertEqual(store.count_pending_deliveries(self.conn), 1)

    def test_double_approve_does_not_queue_twice(self):
        row = _seed(self.conn)
        for _ in range(3):
            self.client.post(f"/proposals/{row}/approve")
        self.assertEqual(store.count_pending_deliveries(self.conn), 1)

    def test_review_page_links_to_before_after(self):
        _seed(self.conn)
        self.assertIn("/runs/run-t/before-after", self.client.get("/runs/run-t").text)

    def test_unknown_run_is_404_with_home_link(self):
        r = self.client.get("/runs/nope")
        self.assertEqual(r.status_code, 404)
        self.assertIn("ホームの", r.text)


class DeliveryRobustnessTest(_WebCase):
    def _queue(self, body="本文"):
        return delivery.enqueue(
            self.conn, run_id="r", subject="s", body=body, proposal_row_ids=[1],
            config=self.config, today=(2026, 11, 25),
        )

    def test_enqueue_same_notice_twice_returns_same_row(self):
        self.assertEqual(self._queue(), self._queue())
        self.assertEqual(store.count_pending_deliveries(self.conn), 1)

    def test_cancelled_delivery_is_not_marked_sent_or_sent(self):
        row = self._queue()
        sent = []

        class Sender:
            def send(self, d):
                sent.append(d)

        pending = store.list_pending_deliveries(self.conn)
        store.cancel_pending_deliveries_for_proposal(self.conn, 1)
        self.assertFalse(store.mark_delivery_sent(self.conn, row))
        with mock.patch.object(delivery, "is_online", return_value=True):
            with mock.patch.object(store, "list_pending_deliveries", return_value=pending):
                delivery.flush(self.conn, config=self.config, sender=Sender())
        self.assertEqual(sent, [])

    def test_permanent_rejection_gives_up_and_does_not_block_the_next(self):
        first = self._queue("A")
        second = self._queue("B")

        class Sender:
            def send(self, d):
                if d["body"] == "A":
                    raise smtplib.SMTPRecipientsRefused({})

        with mock.patch.object(delivery, "is_online", return_value=True):
            for _ in range(store.MAX_DELIVERY_ATTEMPTS):
                delivery.flush(self.conn, config=self.config, sender=Sender())
        statuses = dict(self.conn.execute("SELECT row_id, status FROM delivery_queue").fetchall())
        self.assertEqual(statuses[first], "failed")
        self.assertEqual(statuses[second], "sent")


class GateTest(unittest.TestCase):
    def test_unopenable_vault_fails_closed_when_required(self):
        cfg = privacy.load_privacy_config()
        text = "山田太郎さんはバイタル安定"
        required = privacy.run_gate(text, config=cfg, vault_conn=None, require_vault=True)
        self.assertEqual(required.status, privacy.PrivacyGateStatus.FAILED)
        self.assertIn("vault_save_failed", required.reason_codes)
        self.assertFalse(privacy.cleared_for_downstream(required))

    def test_llm_down_with_risk_threshold_code_still_says_ai_is_down(self):
        result = privacy.GateResult(
            request_id="a" * 12, status=privacy.PrivacyGateStatus.REVIEW_REQUIRED, mode="enforce",
            sanitized_text="x", policy_version="t",
            reason_codes=["llm_unavailable", "residual_risk_above_threshold"],
        )
        message = webapp._gate_message(result)
        self.assertIn("Ollama", message)
        self.assertNotIn("個人を特定し得る", message)


class InputValidationTest(_WebCase):
    def test_unsupported_audio_extension_is_415_in_japanese(self):
        gate = mock.Mock()
        with mock.patch.object(webapp, "_run_privacy_gate", gate):
            r = self.client.post("/nippou/structure", files={"audio": ("a.xyz", b"x", "audio/x-foo")})
        self.assertEqual(r.status_code, 415)
        self.assertIn("扱えません", r.text)
        gate.assert_not_called()

    def test_transcribe_rejects_non_audio_path(self):
        agent = nippou.NippouAgent(None)
        with self.assertRaises(nippou.UnsupportedAudioError):
            agent.transcribe(Path("/tmp/tmpabc.aac.txt"))

    def test_transcription_failure_is_a_japanese_503(self):
        with mock.patch.object(nippou.NippouAgent, "transcribe", side_effect=RuntimeError("boom")):
            r = self.client.post("/nippou/structure", files={"audio": ("a.webm", b"x", "audio/webm")})
        self.assertEqual(r.status_code, 503)
        self.assertIn("文字に起こせませんでした", r.text)
        self.assertNotIn("boom", r.text)

    def test_unknown_procedure_and_bad_date_are_422(self):
        r = self.client.post("/nippou/structure", data={"utterance_text": "x", "procedures": "でたらめ"})
        self.assertEqual(r.status_code, 422)
        r = self.client.post("/nippou/structure", data={"utterance_text": "x", "visited_on": "2026-99-99"})
        self.assertEqual(r.status_code, 422)
        self.assertIn("訪問日", r.text)

    def test_405_and_500_are_japanese(self):
        r = self.client.get("/proposals/1/approve")
        self.assertEqual(r.status_code, 405)
        self.assertNotIn("Method Not Allowed", r.text)
        quiet = TestClient(webapp.app, raise_server_exceptions=False)
        with mock.patch.object(webapp, "_missing_notice_pdfs", side_effect=RuntimeError("x")):
            r = quiet.post("/runs", data={"label": "d"})
        self.assertEqual(r.status_code, 500)
        self.assertIn("予期しないエラー", r.text)
        self.assertNotIn("Internal Server Error", r.text)


class NippouRecordTest(_WebCase):
    def _save(self, items=None):
        ctx = webapp._RecordContext(procedures=[], want_soap=True, subject_code="P-100234", visited_on="2026-11-20")
        result = privacy.GateResult(
            request_id="r" * 12, status=privacy.PrivacyGateStatus.PASSED, mode="enforce",
            sanitized_text="x", policy_version="t",
        )
        record = nippou.NippouRecord(
            checklist_version="v", utterance_id="u",
            items=items or [nippou.NippouItem("項目", "値", True, "k")],
        )
        soap = nippou.SoapRecord(utterance_id=os.urandom(3).hex(), subjective="s")
        return webapp._save_nippou_record(result, ctx, record, soap), ctx

    def test_identical_record_is_not_saved_twice_but_different_one_is(self):
        self.assertEqual(self._save()[0], "saved")
        self.assertEqual(self._save()[0], "duplicate")
        self.assertEqual(self._save([nippou.NippouItem("項目", "別の値", True, "k")])[0], "saved")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM nippou_records").fetchone()[0], 2)

    def test_record_can_be_deleted_only_for_the_matching_subject(self):
        _, ctx = self._save()
        row_id = self.conn.execute("SELECT row_id FROM nippou_records").fetchone()[0]
        r = self.client.post(f"/nippou/records/{row_id}/delete", data={"subject_code": "P-100987"})
        self.assertEqual(r.status_code, 404)
        r = self.client.post(f"/nippou/records/{row_id}/delete", data={"subject_code": ctx.subject_code})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM nippou_records").fetchone()[0], 0)


class MiscTest(unittest.TestCase):
    def test_same_new_item_approved_in_two_runs_is_added_once(self):
        base = pipeline.load_sitedoc()
        props = [
            {"status": "承認", "after": "評価の記録", "sitedoc_item_id": None, "sitedoc_item_key": None},
            {"status": "承認", "after": "評価の記録", "sitedoc_item_id": None, "sitedoc_item_key": None},
        ]
        diff = checklist.apply_approved(base, props)
        self.assertEqual(diff.added, ["評価の記録"])
        self.assertEqual(len([i for i in diff.after.items if i.label == "評価の記録"]), 1)

    def test_new_database_file_is_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x" / "m.db"
            store.connect(path).close()
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


class DemoTest(_WebCase):
    def test_demo_run_flows_without_any_ai(self):
        with mock.patch.dict(os.environ, {"MOEGI_DEMO": "1"}):
            r = self.client.post("/runs", data={"label": "d"})
            self.assertEqual(r.status_code, 303)
            page = self.client.get(r.headers["location"]).text
            self.assertIn("デモモードです", page)
            run_id = r.headers["location"].rsplit("/", 1)[1]
            rows = store.list_proposals_for_review(self.conn, run_id)
            self.assertEqual(len(rows), 2)
            for row in rows:  # 合成データは識別子検査を通る＝承認できる
                self.assertEqual(self.client.post(f"/proposals/{row['row_id']}/approve").status_code, 303)
            self.assertEqual(len(store.list_applied_proposals(self.conn)), 2)

    def test_demo_adapter_structures_a_nippou_with_the_real_agent(self):
        agent = nippou.NippouAgent(demo.DemoAdapter())
        agent.set_checklist(pipeline.load_sitedoc().items)
        record = agent.structure("血圧128の78、体温36.5度。ご本人は痛みが和らいだとおっしゃっています。")
        vital = next(i for i in record.items if "バイタル" in i.checklist_item_id)
        self.assertTrue(vital.filled)
        self.assertFalse(record.undecidable)
        soap = agent.structure_soap("血圧128の78。ご本人は痛みが和らいだとおっしゃっています。")
        self.assertIn("血圧", soap.objective)

    def test_not_demo_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MOEGI_DEMO", None)
            self.assertFalse(demo.is_demo())


if __name__ == "__main__":
    unittest.main()
