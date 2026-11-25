"""採点指摘（rubric v1 r1）への修正の回帰テスト。合成データのみ・LLM/ネットワーク不使用。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from moegi import delivery, llm, pipeline, privacy, store, webapp
from moegi.privacy import llm_detect


def _seed(conn, *, run_id="run-t", item_label="特記事項", item_key="remarks", after="特記事項（改）", status="未承認"):
    change = conn.execute(
        "INSERT INTO changes (run_id, label, new_path, new_text, kind, summary)"
        " VALUES (?, 'd', '第2', 't', '改正', 's')",
        (run_id,),
    ).lastrowid
    impact = conn.execute(
        "INSERT INTO impacts (run_id, change_row_id, sitedoc_item_id, sitedoc_item_key,"
        " verdict, rationale, confidence) VALUES (?, ?, ?, ?, '要更新', 'x', 0.9)",
        (run_id, change, item_label, item_key),
    ).lastrowid
    row = conn.execute(
        "INSERT INTO proposals (run_id, impact_row_id, before, after, status) VALUES (?, ?, 'b', ?, ?)",
        (run_id, impact, after, status),
    ).lastrowid
    conn.commit()
    return row


class _WebCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "t.db"
        outbox = Path(self._tmp.name) / "outbox"
        self.config = delivery.DeliveryConfig(
            recipient="kanri@example.local", sender="m@example.local", outbox_dir=outbox
        )
        patches = [
            mock.patch.object(webapp, "_db_conn", lambda: store.connect(self.db)),
            mock.patch.object(webapp, "DELIVERY_CONFIG", self.config),
            mock.patch.object(webapp, "_FAX_OUTPUT_DIR", Path(self._tmp.name)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        self.client = TestClient(webapp.app, follow_redirects=False)
        self.conn = store.connect(self.db)
        self.addCleanup(self.conn.close)


class ProposalFlowTest(_WebCase):
    def test_edit_is_distributed_like_an_approval(self):
        row = _seed(self.conn)
        r = self.client.post(f"/proposals/{row}/edit", data={"after": "編集後の文言"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(store.count_pending_deliveries(self.conn), 1)
        run_page = self.client.get("/runs/run-t").text
        self.assertIn("編集済", run_page)
        pdf = self.client.get("/runs/run-t/fax-preview")
        self.assertEqual(pdf.status_code, 200)
        history = store.list_fax_deliveries(self.conn, "run-t")
        self.assertEqual(history[0]["proposal_row_ids"], [row])

    def test_reject_after_approve_cancels_pending_notice(self):
        row = _seed(self.conn)
        self.client.post(f"/proposals/{row}/approve")
        self.assertEqual(store.count_pending_deliveries(self.conn), 1)
        r = self.client.post(f"/proposals/{row}/reject")
        self.assertIn("notice=reject_cancelled", r.headers["location"])
        self.assertEqual(store.count_pending_deliveries(self.conn), 0)
        page = self.client.get(r.headers["location"]).text
        self.assertIn("取り消しました", page)

    def test_reject_after_sent_warns_instead_of_pretending(self):
        row = _seed(self.conn)
        self.client.post(f"/proposals/{row}/approve")
        delivery.flush(self.conn, config=self.config)
        self.assertEqual(store.count_pending_deliveries(self.conn), 0)
        r = self.client.post(f"/proposals/{row}/reject")
        self.assertIn("notice=reject_after_sent", r.headers["location"])

    def test_edit_replaces_pending_notice_with_new_text(self):
        row = _seed(self.conn)
        self.client.post(f"/proposals/{row}/approve")
        self.client.post(f"/proposals/{row}/edit", data={"after": "直した文言"})
        pending = store.list_pending_deliveries(self.conn)
        self.assertEqual(len(pending), 1)
        self.assertIn("直した文言", pending[0]["body"])

    def test_approval_with_identifier_is_refused_and_not_applied_anywhere(self):
        # 識別子入りの文言は、承認そのものを止める（配信だけ止めて FAX・日報には載る、を作らない）
        row = _seed(self.conn, after="連絡先は 090-1234-5678 です")
        r = self.client.post(f"/proposals/{row}/approve")
        self.assertIn("notice=approve_refused", r.headers["location"])
        self.assertEqual(store.count_pending_deliveries(self.conn), 0)
        self.assertIn("承認していません", self.client.get(r.headers["location"]).text)
        status = self.conn.execute("SELECT status FROM proposals WHERE row_id = ?", (row,)).fetchone()[0]
        self.assertEqual(status, "未承認")
        self.assertEqual(store.list_applied_proposals(self.conn), [])

    def test_unknown_ids_return_japanese_404(self):
        for path in ("/proposals/99999/approve", "/proposals/99999/reject"):
            r = self.client.post(path)
            self.assertEqual(r.status_code, 404, path)
            self.assertIn("見つかりません", r.text)
        r = self.client.post("/proposals/99999/edit", data={"after": "x"})
        self.assertEqual(r.status_code, 404)

    def test_fax_for_unknown_run_is_404_and_leaves_no_history(self):
        r = self.client.get("/runs/nope/fax-preview")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(store.list_fax_deliveries(self.conn, "nope"), [])

    def test_empty_edit_is_not_saved(self):
        row = _seed(self.conn)
        r = self.client.post(f"/proposals/{row}/edit", data={"after": "   "})
        self.assertIn("edit_empty", r.headers["location"])
        self.assertEqual(store.get_proposal_for_review(self.conn, row)["status"], "未承認")


class DeliveryOfflineTest(unittest.TestCase):
    def test_file_channel_without_explicit_probe_needs_no_network(self):
        config = delivery.DeliveryConfig(recipient="a@example.local")
        with mock.patch.object(delivery.socket, "create_connection", side_effect=OSError):
            self.assertTrue(delivery.is_online(config))

    def test_explicit_probe_is_still_honoured(self):
        config = delivery.DeliveryConfig(recipient="a@example.local", probe="127.0.0.1:9")
        self.assertFalse(delivery.is_online(config, timeout=0.2))


class LocalLLMFailureTest(unittest.TestCase):
    def _adapter(self, handler):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        return llm.LLMAdapter(http_client=client)

    def test_connection_refused_is_reported_not_raised(self):
        def handler(request):
            raise httpx.ConnectError("refused")

        adapter = self._adapter(handler)
        result = adapter.complete("nippou", "x")
        self.assertTrue(result["timeout"])
        self.assertTrue(result["unavailable"])
        self.assertEqual(adapter.local_unavailable_count, 1)

    def test_http_error_status_is_reported_not_raised(self):
        adapter = self._adapter(lambda request: httpx.Response(500, text="boom"))
        result = adapter.complete("nippou", "x")
        self.assertTrue(result["unavailable"])

    def test_malformed_body_is_reported_not_raised(self):
        adapter = self._adapter(lambda request: httpx.Response(200, json={"unexpected": 1}))
        result = adapter.complete("nippou", "x")
        self.assertTrue(result["unavailable"])
        self.assertEqual(result["text"], "")

    def test_timeout_is_not_counted_as_unavailable(self):
        def handler(request):
            raise httpx.ReadTimeout("slow")

        adapter = self._adapter(handler)
        result = adapter.complete("nippou", "x")
        self.assertTrue(result["timeout"])
        self.assertFalse(result["unavailable"])
        self.assertEqual(adapter.local_unavailable_count, 0)

    def test_privacy_llm_detect_reports_unavailable_code(self):
        adapter = self._adapter(lambda request: (_ for _ in ()).throw(httpx.ConnectError("x")))
        outcome = llm_detect.detect("入力", adapter)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_code, "llm_unavailable")

    def test_orca_malformed_200_falls_back_to_local(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            if "11434" in str(request.url):
                return httpx.Response(200, json={"choices": [{"message": {"content": "local"}}]})
            return httpx.Response(200, json={"oops": True})

        with mock.patch.dict("os.environ", {"ORCA_API_KEY": "dummy-not-a-real-key"}):
            adapter = self._adapter(handler)
            result = adapter.complete("diff", "x")
        self.assertEqual(result["text"], "local")
        self.assertTrue(result["degraded"])


class RunsPageTest(_WebCase):
    def test_missing_notice_pdfs_gives_japanese_guidance(self):
        with mock.patch.object(webapp, "_missing_notice_pdfs", return_value=["old.pdf"]):
            r = self.client.post("/runs", data={"label": "x"})
        self.assertEqual(r.status_code, 503)
        self.assertIn("通達PDFが置かれていません", r.text)
        self.assertIn("fetch_notices.sh", r.text)
        self.assertNotIn("Internal Server Error", r.text)


class _DownAdapter:
    local_unavailable_count = 0

    def __init__(self, *a, **k):
        pass

    def complete(self, stage, prompt, opts=None):
        return {
            "text": "", "usage": {"prompt_tokens": 0, "completion_tokens": 0}, "cost_jpy": 0.0,
            "backend": "local:x", "degraded": True, "timeout": True, "unavailable": True,
            "latency_ms": 1.0, "resolved_model": "x", "fallback_level": 0,
        }

    def close(self):
        pass


class NippouFailureTest(_WebCase):
    def _gate_passed(self):
        return privacy.GateResult(
            request_id="abcdef123456", status=privacy.PrivacyGateStatus.PASSED, mode="enforce",
            sanitized_text="血圧128の78", policy_version="t",
        )

    def test_undecidable_nippou_is_not_saved(self):
        with (
            mock.patch.object(webapp, "_run_privacy_gate", return_value=self._gate_passed()),
            mock.patch.object(webapp, "_save_privacy_audit"),
            mock.patch.object(webapp.llm, "LLMAdapter", _DownAdapter),
            mock.patch.object(webapp, "_validated_subject_code", lambda c: c),
        ):
            r = self.client.post(
                "/nippou/structure", data={"utterance_text": "血圧128の78", "subject_code": "P-001"}
            )
        self.assertEqual(r.status_code, 503)
        self.assertIn("日報は保存していません", r.text)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM nippou_records").fetchone()[0], 0)

    def test_unregistered_subject_code_is_dropped(self):
        self.assertEqual(webapp._validated_subject_code("not-a-registered-code"), "")
        self.assertEqual(webapp._validated_subject_code(""), "")

    def test_empty_input_message_names_the_real_cause(self):
        result = privacy.GateResult(
            request_id="a" * 12, status=privacy.PrivacyGateStatus.FAILED, mode="enforce",
            sanitized_text="", policy_version="t", reason_codes=["empty_input"],
        )
        self.assertIn("入力が空", webapp._gate_message(result))

    def test_llm_down_message_is_not_a_pii_warning(self):
        result = privacy.GateResult(
            request_id="a" * 12, status=privacy.PrivacyGateStatus.REVIEW_REQUIRED, mode="enforce",
            sanitized_text="x", policy_version="t", reason_codes=["llm_unavailable"],
        )
        message = webapp._gate_message(result)
        self.assertIn("Ollama", message)
        self.assertNotIn("個人を特定し得る", message)

    def test_oversized_audio_is_rejected(self):
        gate = mock.Mock()
        with (
            mock.patch.object(webapp, "MAX_AUDIO_BYTES", 10),
            mock.patch.object(webapp, "_run_privacy_gate", gate),
        ):
            r = self.client.post(
                "/nippou/structure", files={"audio": ("a.webm", b"x" * 100, "audio/webm")}
            )
        self.assertEqual(r.status_code, 413)
        gate.assert_not_called()


class ApprovedChecklistInNippouTest(_WebCase):
    def test_approved_proposals_change_the_checklist_used_for_nippou(self):
        base = pipeline.load_sitedoc()
        target = base.items[0]
        row = _seed(self.conn, item_label=target.label, item_key=target.key, after="改定後の項目文言")
        sitedoc, changes = webapp._current_sitedoc()
        self.assertEqual(changes, 0)  # 未承認では変わらない
        self.assertEqual(sitedoc.items[0].label, target.label)
        store.update_proposal_status(self.conn, row, "承認")
        sitedoc, changes = webapp._current_sitedoc()
        self.assertEqual(changes, 1)
        self.assertEqual(sitedoc.items[0].label, "改定後の項目文言")

    def test_rejected_proposals_are_not_applied(self):
        base = pipeline.load_sitedoc()
        _seed(self.conn, item_label=base.items[0].label, item_key=base.items[0].key, status="却下")
        _, changes = webapp._current_sitedoc()
        self.assertEqual(changes, 0)


class AccessGuardTest(_WebCase):
    def test_cross_origin_post_is_refused(self):
        row = _seed(self.conn)
        r = self.client.post(
            f"/proposals/{row}/approve", headers={"Origin": "http://evil.example", "Host": "localhost:8420"}
        )
        self.assertEqual(r.status_code, 403)
        self.assertEqual(store.get_proposal_for_review(self.conn, row)["status"], "未承認")

    def test_same_origin_post_is_allowed(self):
        row = _seed(self.conn)
        r = self.client.post(
            f"/proposals/{row}/approve", headers={"Origin": "http://localhost:8420", "Host": "localhost:8420"}
        )
        self.assertEqual(r.status_code, 303)

    def test_token_required_when_configured(self):
        with mock.patch.dict("os.environ", {"MOEGI_ACCESS_TOKEN": "s3cret-for-test"}):
            self.assertEqual(self.client.get("/").status_code, 401)
            self.assertEqual(self.client.get("/", auth=("anyone", "wrong")).status_code, 401)
            self.assertEqual(self.client.get("/", auth=("anyone", "s3cret-for-test")).status_code, 200)

    def test_no_token_means_open_for_local_use(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("MOEGI_ACCESS_TOKEN", None)
            self.assertEqual(self.client.get("/").status_code, 200)


class SiteNameAndVaultTest(unittest.TestCase):
    def test_site_name_is_configurable(self):
        with mock.patch.dict("os.environ", {"MOEGI_SITE_NAME": "テスト事業所"}):
            self.assertEqual(webapp._site_name(), "テスト事業所")

    def test_purge_never_raises(self):
        with mock.patch.object(webapp.privacy_vault, "connect", side_effect=OSError):
            self.assertEqual(webapp.purge_expired_vault(), 0)
