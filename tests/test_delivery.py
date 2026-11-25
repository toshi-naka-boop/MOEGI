"""配布のキュー型チャネル（delivery.py）のテスト（design.md G8・ledger R-89）。

合成データのみ。SMTPの実サーバーには接続しない（送信先はダブルに差し替える）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from moegi import delivery, store

_TODAY = (2026, 8, 13)


def _config(**overrides) -> delivery.DeliveryConfig:
    base = {
        "recipient": "kanri@example.local",
        "sender": "moegi@example.local",
        "smtp_host": "",
        "probe": "127.0.0.1:9",
    }
    base.update(overrides)
    return delivery.DeliveryConfig(**base)


class _RecordingSender:
    def __init__(self, *, fail: bool = False):
        self.sent: list[dict] = []
        self._fail = fail

    def send(self, delivery_row: dict) -> None:
        if self._fail:
            raise OSError("network unreachable")
        self.sent.append(delivery_row)


class EnqueueScreeningTest(unittest.TestCase):
    """外部へ出す前ではなく、**キューに入る前**に止める（fail-closed）。"""

    def setUp(self):
        self.conn = store.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    def _enqueue(self, body: str, subject: str = "【MOEGI】更新案の承認通知") -> int:
        return delivery.enqueue(
            self.conn,
            run_id="r1",
            subject=subject,
            body=body,
            proposal_row_ids=[1],
            config=_config(),
            today=_TODAY,
        )

    def test_normal_proposal_notice_is_accepted(self):
        row_id = self._enqueue(
            "■ 対象項目: バイタルサインの記録\n  変更後: SpO2を含めて記録する\n  根拠条文: 第4の4"
        )
        self.assertEqual(store.count_pending_deliveries(self.conn), 1)
        self.assertTrue(row_id)

    def test_phone_number_in_body_is_refused(self):
        with self.assertRaises(delivery.DeliveryRefused):
            self._enqueue("担当へ連絡してください: 090-1234-5678")
        self.assertEqual(store.count_pending_deliveries(self.conn), 0)

    def test_email_address_in_body_is_refused(self):
        with self.assertRaises(delivery.DeliveryRefused):
            self._enqueue("詳細は kazoku@example.com へ")

    def test_api_key_in_body_is_refused(self):
        with self.assertRaises(delivery.DeliveryRefused):
            self._enqueue("連携キー sk-abcdefghijklmnopqrstuvwx")

    def test_detailed_address_in_body_is_refused(self):
        with self.assertRaises(delivery.DeliveryRefused):
            self._enqueue("訪問先は東京都新宿区西新宿2-8-1です")

    def test_subject_is_screened_too(self):
        with self.assertRaises(delivery.DeliveryRefused):
            self._enqueue("本文は無害です", subject="至急 090-1234-5678 まで")

    def test_refusal_message_does_not_leak_the_value(self):
        try:
            self._enqueue("連絡先 090-1234-5678")
        except delivery.DeliveryRefused as exc:
            self.assertNotIn("090-1234-5678", str(exc))
            self.assertIn("phone", str(exc))
        else:
            self.fail("DeliveryRefused が送出されませんでした")

    def test_vital_numbers_are_not_mistaken_for_identifiers(self):
        # 更新案の文言にバイタルの数値が入ることは普通にある。これで弾かれては使えない。
        self._enqueue("■ 対象項目: バイタルサインの記録\n  変更後: 血圧128/78のように数値で記録する")
        self.assertEqual(store.count_pending_deliveries(self.conn), 1)


class FlushTest(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        self.config = _config()
        delivery.enqueue(
            self.conn,
            run_id="r1",
            subject="件名",
            body="■ 対象項目: 特記事項\n  変更後: 頻回訪問の理由を記録する",
            proposal_row_ids=[1],
            config=self.config,
            today=_TODAY,
        )

    def tearDown(self):
        self.conn.close()

    def test_offline_does_not_attempt_any_send(self):
        sender = _RecordingSender()
        with mock.patch.object(delivery, "is_online", return_value=False):
            result = delivery.flush(self.conn, config=self.config, sender=sender)
        self.assertTrue(result.skipped_offline)
        self.assertEqual(sender.sent, [])
        # 消えたのではなく待っている（G8）
        self.assertEqual(store.count_pending_deliveries(self.conn), 1)

    def test_online_sends_and_marks_sent(self):
        sender = _RecordingSender()
        with mock.patch.object(delivery, "is_online", return_value=True):
            result = delivery.flush(self.conn, config=self.config, sender=sender)
        self.assertEqual(result.sent, 1)
        self.assertEqual(len(sender.sent), 1)
        self.assertEqual(store.count_pending_deliveries(self.conn), 0)
        self.assertEqual(store.list_deliveries(self.conn)[0]["status"], "sent")

    def test_send_failure_keeps_the_item_pending(self):
        sender = _RecordingSender(fail=True)
        with mock.patch.object(delivery, "is_online", return_value=True):
            result = delivery.flush(self.conn, config=self.config, sender=sender)
        self.assertEqual(result.failed, 1)
        self.assertEqual(store.count_pending_deliveries(self.conn), 1)
        row = store.list_deliveries(self.conn)[0]
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["last_error_code"], "send_failed")

    def test_error_code_is_a_fixed_vocabulary_not_an_exception_message(self):
        # 例外メッセージには宛先や本文が混じり得る（design/moegi/privacy.md §10）。
        sender = _RecordingSender(fail=True)
        with mock.patch.object(delivery, "is_online", return_value=True):
            delivery.flush(self.conn, config=self.config, sender=sender)
        self.assertNotIn("unreachable", store.list_deliveries(self.conn)[0]["last_error_code"])

    def test_first_failure_stops_the_batch(self):
        for i in range(2):
            delivery.enqueue(
                self.conn,
                run_id="r1",
                subject=f"件名{i}",
                body="変更後: 記録する",
                proposal_row_ids=[i],
                config=self.config,
                today=_TODAY,
            )
        sender = _RecordingSender(fail=True)
        with mock.patch.object(delivery, "is_online", return_value=True):
            result = delivery.flush(self.conn, config=self.config, sender=sender)
        self.assertEqual(result.failed, 1)  # 3件あるが1件で打ち切る
        self.assertEqual(store.count_pending_deliveries(self.conn), 3)

    def test_queue_is_first_in_first_out(self):
        delivery.enqueue(
            self.conn,
            run_id="r1",
            subject="2件目",
            body="変更後: 記録する",
            proposal_row_ids=[2],
            config=self.config,
            today=_TODAY,
        )
        pending = store.list_pending_deliveries(self.conn)
        self.assertEqual([p["subject"] for p in pending], ["件名", "2件目"])

    def test_offline_then_online_flushes_everything(self):
        """G8の終幕そのもの: オフラインで溜め、復帰でまとめて流れる。"""
        for i in range(2):
            delivery.enqueue(
                self.conn,
                run_id="r1",
                subject=f"追加{i}",
                body="変更後: 記録する",
                proposal_row_ids=[i],
                config=self.config,
                today=_TODAY,
            )
        sender = _RecordingSender()
        with mock.patch.object(delivery, "is_online", return_value=False):
            delivery.flush(self.conn, config=self.config, sender=sender)
        self.assertEqual(store.count_pending_deliveries(self.conn), 3)

        with mock.patch.object(delivery, "is_online", return_value=True):
            result = delivery.flush(self.conn, config=self.config, sender=sender)
        self.assertEqual(result.sent, 3)
        self.assertEqual(store.count_pending_deliveries(self.conn), 0)


class FileSenderTest(unittest.TestCase):
    def test_writes_an_eml_to_the_outbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(outbox_dir=Path(tmp))
            delivery.FileSender(config).send(
                {
                    "row_id": 7,
                    "recipient": "kanri@example.local",
                    "subject": "件名",
                    "body": "本文",
                }
            )
            files = list(Path(tmp).glob("*.eml"))
            self.assertEqual(len(files), 1)
            content = files[0].read_text(encoding="utf-8")
            self.assertIn("kanri@example.local", content)

    def test_channel_is_file_when_smtp_is_not_configured(self):
        self.assertEqual(_config().channel, delivery.CHANNEL_FILE)

    def test_channel_is_email_when_smtp_host_is_set(self):
        self.assertEqual(_config(smtp_host="smtp.example.local").channel, delivery.CHANNEL_EMAIL)


class NoticeBodyTest(unittest.TestCase):
    def test_notice_contains_labels_and_paths_but_not_the_notice_quote(self):
        subject, body = delivery.format_proposal_notice(
            [
                {
                    "sitedoc_item_id": "特記事項",
                    "before": "特記事項",
                    "after": "特記事項（頻回訪問の理由を含む）",
                    "new_path": "第2の2(4)",
                    "evidence_quote": "この長い通達原文の引用は配信本文に入れない。",
                }
            ]
        )
        self.assertIn("1件", subject)
        self.assertIn("特記事項（頻回訪問の理由を含む）", body)
        self.assertIn("第2の2(4)", body)
        self.assertNotIn("この長い通達原文の引用", body)

    def test_new_item_is_labelled(self):
        _, body = delivery.format_proposal_notice(
            [{"sitedoc_item_id": None, "before": "", "after": "評価の記録", "new_path": "第4の4"}]
        )
        self.assertIn("（新規項目）", body)
        self.assertIn("この項目は現在存在しません", body)


class DeliveryConfigTest(unittest.TestCase):
    def test_password_is_not_exposed_by_the_default_repr_of_the_loaded_config(self):
        # 設定オブジェクトをうっかりログへ出しても、少なくとも「password=」の値が
        # そのまま読めないようにはしない——ここでは値を持つこと自体は許し、
        # ログサニタイザー側（logsafe）で落とす設計であることを固定する。
        from moegi.privacy import logsafe

        config = _config(smtp_password="s3cret-value-abcdefghijklmnop")
        self.assertNotIn("s3cret-value-abcdefghijklmnop", logsafe.sanitize(repr(config)))


class DeliveryWebTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        from moegi import webapp

        self.webapp = webapp
        self.client = TestClient(webapp.app)

    def test_page_renders(self):
        response = self.client.get("/deliveries")
        self.assertEqual(response.status_code, 200)
        self.assertIn("配信キュー", response.text)

    def test_no_manual_resend_button(self):
        """R-16: 手動再送ボタンは作らない（人が押さないと届かないなら意味が無い）。"""
        response = self.client.get("/deliveries")
        for forbidden in ("再送", "今すぐ送信", "手動送信"):
            self.assertNotIn(f">{forbidden}<", response.text)
        self.assertNotIn("action=\"/deliveries/flush\"", response.text)

    def test_badge_appears_when_something_is_pending(self):
        with mock.patch.object(self.webapp.store, "count_pending_deliveries", return_value=3):
            response = self.client.get("/deliveries")
        self.assertIn("送信待ち 3 件", response.text)

    def test_badge_is_hidden_when_nothing_is_pending(self):
        with mock.patch.object(self.webapp.store, "count_pending_deliveries", return_value=0):
            response = self.client.get("/")
        self.assertNotIn("送信待ち", response.text)

    def test_approval_enqueues_a_notice(self):
        with (
            mock.patch.object(self.webapp.store, "update_proposal_status"),
            mock.patch.object(
                self.webapp.store,
                "get_proposal_for_review",
                return_value={
                    "sitedoc_item_id": "特記事項",
                    "before": "特記事項",
                    "after": "特記事項（改）",
                    "new_path": "第2の2",
                },
            ),
            mock.patch.object(self.webapp.delivery, "enqueue") as enqueue,
        ):
            conn = mock.MagicMock()
            conn.execute.return_value.fetchone.return_value = ("run-1",)
            self.webapp._enqueue_approved_notice(conn, 1)
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.kwargs["proposal_row_ids"], [1])

    def test_approval_still_succeeds_when_the_notice_is_refused(self):
        # 配信を拒否されても承認自体は成立させる（承認を巻き戻す方が分かりにくい状態になる）。
        with (
            mock.patch.object(
                self.webapp.store,
                "get_proposal_for_review",
                return_value={"sitedoc_item_id": "x", "before": "", "after": "y", "new_path": "z"},
            ),
            mock.patch.object(
                self.webapp.delivery, "enqueue", side_effect=delivery.DeliveryRefused("phone")
            ),
        ):
            conn = mock.MagicMock()
            conn.execute.return_value.fetchone.return_value = ("run-1",)
            self.webapp._enqueue_approved_notice(conn, 1)  # 例外が漏れないこと


if __name__ == "__main__":
    unittest.main()


class NoticeDateScreeningTest(unittest.TestCase):
    """改定通知の適用日は識別子として止めない。生年月日の文脈があれば止める。"""

    def _screen(self, after: str) -> list[str]:
        rows = [{"sitedoc_item_id": "item-1", "before": None, "after": after, "new_path": "第4の4"}]
        return delivery.screen_notice(rows, today=_TODAY)

    def test_effective_date_is_not_blocked(self):
        self.assertEqual(self._screen("令和6年6月1日から適用する"), [])
        self.assertEqual(self._screen("2024年6月1日以降に記録する"), [])

    def test_birthdate_with_context_is_blocked(self):
        self.assertIn("birthdate", self._screen("1943年5月20日生まれの方"))
