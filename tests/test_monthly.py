"""月報（monthly.py）と日報の永続化（store.py）のテスト（ledger R-84）。

⚠️ 合成データのみ。実在する患者・職員・医療機関の情報は使わない。

月報はLLMを一切使わないので、このテストにもLLMのダブルが出てこない——
「集計が決定論的である」ことがそのままテストの単純さに出ている。
"""

from __future__ import annotations

import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from moegi import monthly, store
from moegi.privacy import dictionary as dict_module
from moegi.privacy import vault

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DICTIONARY_PATH = _REPO_ROOT / "config" / "pseudonym_dictionary_sample.toml"
_TEST_VAULT_KEY = base64.urlsafe_b64encode(b"2" * 32).decode()


def _record(
    visited_on: str,
    *,
    procedures: list[str] | None = None,
    items: list[dict] | None = None,
    evaluation: dict | None = None,
    soap: dict | None = None,
    gate_status: str = "passed",
    policy_version: str = "test-policy-1",
) -> dict:
    return {
        "visited_on": visited_on,
        "gate_status": gate_status,
        "policy_version": policy_version,
        "procedures": procedures or [],
        "items": items or [],
        "evaluation": evaluation,
        "soap": soap,
    }


def _vital_item(value: str) -> dict:
    return {"checklist_item_id": "バイタルサインの記録", "value": value, "filled": True}


class StablePseudonymTest(unittest.TestCase):
    """月報の集計単位。日をまたいで安定していないと1ヶ月分がまとまらない。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.key_path = Path(self._tmp.name) / ".vault_key"

    def tearDown(self):
        self._tmp.cleanup()

    def test_same_code_yields_same_pseudonym(self):
        first = vault.stable_pseudonym("P-100234", key_path=self.key_path)
        second = vault.stable_pseudonym("P-100234", key_path=self.key_path)
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("PATIENT_"))

    def test_different_codes_yield_different_pseudonyms(self):
        # 同姓同名（鈴木 一郎）はコードで区別されるので、仮名も別になる
        self.assertNotEqual(
            vault.stable_pseudonym("P-100555", key_path=self.key_path),
            vault.stable_pseudonym("P-100556", key_path=self.key_path),
        )

    def test_pseudonym_does_not_contain_the_code(self):
        pseudonym = vault.stable_pseudonym("P-100234", key_path=self.key_path)
        self.assertNotIn("100234", pseudonym)
        self.assertNotIn("P-", pseudonym.removeprefix("PATIENT_"))

    def test_pseudonym_depends_on_the_key(self):
        # 鍵を知らない者はコードから仮名を作れない（平文ハッシュとの違い）。
        other_key = Path(self._tmp.name) / "other.key"
        self.assertNotEqual(
            vault.stable_pseudonym("P-100234", key_path=self.key_path),
            vault.stable_pseudonym("P-100234", key_path=other_key),
        )


class PatientCodeTest(unittest.TestCase):
    def test_codes_are_listed_without_names(self):
        dictionary = dict_module.load_dictionary(_DICTIONARY_PATH)
        codes = dict_module.patient_codes(dictionary)
        self.assertIn("P-100234", codes)
        self.assertIn("P-100555", codes)
        self.assertIn("P-100556", codes)
        for code in codes:
            self.assertNotIn("山田", code)
            self.assertNotIn("鈴木", code)


class NippouPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    def _save(self, visited_on: str, subject: str = "PATIENT_abc123", **overrides):
        payload = {
            "request_id": "req-1",
            "subject_pseudonym": subject,
            "visited_on": visited_on,
            "gate_status": "passed",
            "checklist_version": "sitedoc-8items",
            "policy_version": "test-policy-1",
            "items": [_vital_item("血圧128/78、体温36.5度")],
            "procedures": ["吸引"],
            "evaluation": {"goal_ref": "在宅療養の継続", "achievement": "順調", "effect": "疼痛軽減", "filled": True},
            "soap": {"subjective": "楽になった", "objective": "血圧128/78", "assessment": "安定", "plan": "継続"},
        }
        payload.update(overrides)
        return store.save_nippou_record(self.conn, **payload)

    def test_roundtrip_for_a_month(self):
        self._save("2026-08-03")
        self._save("2026-08-17")
        self._save("2026-09-01")  # 対象外の月
        records = store.list_nippou_records_for_month(
            self.conn, subject_pseudonym="PATIENT_abc123", year=2026, month=8
        )
        self.assertEqual([r["visited_on"] for r in records], ["2026-08-03", "2026-08-17"])
        self.assertEqual(records[0]["procedures"], ["吸引"])
        self.assertEqual(records[0]["soap"]["objective"], "血圧128/78")

    def test_records_of_other_subjects_are_not_mixed_in(self):
        self._save("2026-08-03", subject="PATIENT_aaa")
        self._save("2026-08-04", subject="PATIENT_bbb")
        records = store.list_nippou_records_for_month(
            self.conn, subject_pseudonym="PATIENT_aaa", year=2026, month=8
        )
        self.assertEqual(len(records), 1)

    def test_null_evaluation_and_soap_survive_the_roundtrip(self):
        self._save("2026-08-03", evaluation=None, soap=None)
        record = store.list_nippou_records_for_month(
            self.conn, subject_pseudonym="PATIENT_abc123", year=2026, month=8
        )[0]
        self.assertIsNone(record["evaluation"])
        self.assertIsNone(record["soap"])

    def test_available_months_are_listed_newest_first(self):
        self._save("2026-07-30")
        self._save("2026-08-03")
        self.assertEqual(
            store.list_nippou_months(self.conn, "PATIENT_abc123"), ["2026-08", "2026-07"]
        )

    def test_table_has_no_column_for_raw_transcript(self):
        # §「原文は保存しない」。列そのものが無いことで構造的に保証する。
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(nippou_records)")}
        for forbidden in ("transcript", "raw_text", "utterance_text", "sanitized_text", "audio"):
            self.assertNotIn(forbidden, columns)

    def test_subject_code_is_not_stored(self):
        # 保存するのは安定仮名だけ。DB単体からは利用者コードを逆引きできない。
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(nippou_records)")}
        self.assertIn("subject_pseudonym", columns)
        self.assertNotIn("subject_code", columns)
        self._save("2026-08-03")
        dumped = "".join(str(row) for row in self.conn.execute("SELECT * FROM nippou_records"))
        self.assertNotIn("P-100234", dumped)


class MonthlyAggregationTest(unittest.TestCase):
    def test_empty_month(self):
        report = monthly.build([], subject_pseudonym="PATIENT_x", year=2026, month=8)
        self.assertTrue(report.is_empty)
        self.assertEqual(report.visit_count, 0)

    def test_visit_count_and_visit_days(self):
        records = [_record("2026-08-03"), _record("2026-08-03"), _record("2026-08-05")]
        report = monthly.build(records, subject_pseudonym="PATIENT_x", year=2026, month=8)
        self.assertEqual(report.visit_count, 3)  # 同日2回訪問も回数には数える
        self.assertEqual(report.visit_days, 2)

    def test_procedure_counts(self):
        records = [
            _record("2026-08-03", procedures=["吸引", "バイタル測定"]),
            _record("2026-08-05", procedures=["吸引"]),
        ]
        report = monthly.build(records, subject_pseudonym="PATIENT_x", year=2026, month=8)
        self.assertEqual(report.procedure_counts, {"吸引": 2, "バイタル測定": 1})

    def test_item_fill_rates(self):
        records = [
            _record(
                "2026-08-03",
                items=[
                    {"checklist_item_id": "特記事項", "value": "", "filled": False},
                    {"checklist_item_id": "バイタルサインの記録", "value": "x", "filled": True},
                ],
            ),
            _record(
                "2026-08-05",
                items=[
                    {"checklist_item_id": "特記事項", "value": "転倒あり", "filled": True},
                    {"checklist_item_id": "バイタルサインの記録", "value": "y", "filled": True},
                ],
            ),
        ]
        report = monthly.build(records, subject_pseudonym="PATIENT_x", year=2026, month=8)
        rates = {r.label: (r.filled, r.total) for r in report.item_fill_rates}
        self.assertEqual(rates["特記事項"], (1, 2))
        self.assertEqual(rates["バイタルサインの記録"], (2, 2))
        self.assertAlmostEqual(
            next(r for r in report.item_fill_rates if r.label == "特記事項").rate, 0.5
        )

    def test_fill_rates_group_by_immutable_id_across_a_label_change(self):
        """ledger R-88 の本題。月の途中で項目の文言が変わっても二重計上しないこと。

        不変ID導入前は checklist_item_id（ラベル）で束ねていたため、更新案が承認された
        月に**同じ項目が別項目として2行に割れて**記入率が実態より低く出ていた。
        """
        records = [
            _record(
                "2026-08-03",
                items=[{"checklist_item_id": "特記事項", "checklist_item_key": "remarks", "filled": True}],
            ),
            _record(
                "2026-08-20",  # 承認でラベルが書き換わった後の訪問
                items=[
                    {
                        "checklist_item_id": "特記事項（頻回訪問の理由を含む）",
                        "checklist_item_key": "remarks",
                        "filled": True,
                    }
                ],
            ),
        ]
        report = monthly.build(records, subject_pseudonym="PATIENT_x", year=2026, month=8)
        self.assertEqual(len(report.item_fill_rates), 1)
        rate = report.item_fill_rates[0]
        self.assertEqual((rate.filled, rate.total), (2, 2))
        self.assertEqual(rate.key, "remarks")
        # 表示は最新のラベルを使う
        self.assertEqual(rate.label, "特記事項（頻回訪問の理由を含む）")

    def test_fill_rates_fall_back_to_label_for_legacy_records(self):
        # 不変ID導入前に保存された日報（key なし）でも従来どおり集計できること。
        records = [
            _record("2026-08-03", items=[{"checklist_item_id": "特記事項", "filled": True}]),
            _record("2026-08-20", items=[{"checklist_item_id": "特記事項", "filled": False}]),
        ]
        report = monthly.build(records, subject_pseudonym="PATIENT_x", year=2026, month=8)
        self.assertEqual(len(report.item_fill_rates), 1)
        self.assertEqual((report.item_fill_rates[0].filled, report.item_fill_rates[0].total), (1, 2))

    def test_vitals_are_found_by_immutable_id_even_if_the_label_changed(self):
        records = [
            _record(
                "2026-08-03",
                items=[
                    {
                        "checklist_item_id": "生体情報の記録",  # ラベルから「バイタル」が消えている
                        "checklist_item_key": "vital-signs",
                        "value": "血圧128/78、体温36.5度",
                        "filled": True,
                    }
                ],
            )
        ]
        report = monthly.build(records, subject_pseudonym="PATIENT_x", year=2026, month=8)
        self.assertEqual((report.vitals[0].systolic, report.vitals[0].diastolic), (128, 78))

    def test_evaluation_counts(self):
        records = [
            _record("2026-08-03", evaluation={"filled": True}),
            _record("2026-08-05", evaluation={"filled": False}),
            _record("2026-08-06", evaluation=None),  # 評価対象の項目が無かった訪問
        ]
        report = monthly.build(records, subject_pseudonym="PATIENT_x", year=2026, month=8)
        self.assertEqual((report.evaluation_filled, report.evaluation_total), (1, 2))

    def test_vitals_are_extracted_from_the_vital_item(self):
        records = [_record("2026-08-03", items=[_vital_item("血圧128/78、体温36.5度、脈拍70、SpO2 98%")])]
        report = monthly.build(records, subject_pseudonym="PATIENT_x", year=2026, month=8)
        point = report.vitals[0]
        self.assertEqual((point.systolic, point.diastolic), (128, 78))
        self.assertEqual(point.temperature, 36.5)
        self.assertEqual(point.pulse, 70)
        self.assertEqual(point.spo2, 98)
        self.assertEqual(report.unparsed_vital_visits, 0)

    def test_vitals_fall_back_to_the_soap_objective_section(self):
        records = [_record("2026-08-03", soap={"objective": "血圧142/88、体温37.2度"})]
        report = monthly.build(records, subject_pseudonym="PATIENT_x", year=2026, month=8)
        self.assertEqual((report.vitals[0].systolic, report.vitals[0].diastolic), (142, 88))

    def test_unreadable_vitals_are_counted_not_silently_zeroed(self):
        # 「読めなかった」を0として混ぜると、月報の数字が実態より良く/悪く見える。
        records = [
            _record("2026-08-03", items=[_vital_item("特に問題なし")]),
            _record("2026-08-05", items=[_vital_item("血圧128/78")]),
        ]
        report = monthly.build(records, subject_pseudonym="PATIENT_x", year=2026, month=8)
        self.assertEqual(report.unparsed_vital_visits, 1)
        self.assertEqual(len(report.vitals), 1)

    def test_visit_times_are_not_mistaken_for_blood_pressure(self):
        # 訪問時間「10:30-11:00」等が血圧として拾われないこと（バイタル欄以外は見ない）。
        records = [
            _record(
                "2026-08-03",
                items=[
                    {"checklist_item_id": "訪問開始・終了時刻の記録", "value": "10:30/11:00", "filled": True},
                    _vital_item("測定できず"),
                ],
            )
        ]
        report = monthly.build(records, subject_pseudonym="PATIENT_x", year=2026, month=8)
        self.assertEqual(report.vitals, [])
        self.assertEqual(report.unparsed_vital_visits, 1)

    def test_implausible_blood_pressure_is_rejected(self):
        # 収縮期 <= 拡張期 の組は血圧として採らない。
        records = [_record("2026-08-03", items=[_vital_item("78/128")])]
        report = monthly.build(records, subject_pseudonym="PATIENT_x", year=2026, month=8)
        self.assertEqual(report.unparsed_vital_visits, 1)

    def test_non_passed_records_are_flagged(self):
        records = [
            _record("2026-08-03", gate_status="passed"),
            _record("2026-08-05", gate_status="review_required"),
        ]
        report = monthly.build(records, subject_pseudonym="PATIENT_x", year=2026, month=8)
        self.assertEqual(report.non_passed_records, 1)

    def test_mixed_policy_versions_are_surfaced(self):
        records = [
            _record("2026-08-03", policy_version="p1"),
            _record("2026-08-05", policy_version="p2"),
        ]
        report = monthly.build(records, subject_pseudonym="PATIENT_x", year=2026, month=8)
        self.assertEqual(report.policy_versions, ["p1", "p2"])

    def test_aggregation_is_deterministic(self):
        records = [
            _record("2026-08-03", procedures=["吸引"], items=[_vital_item("血圧128/78")]),
            _record("2026-08-05", procedures=["点滴・注射"], items=[_vital_item("血圧130/80")]),
        ]
        first = monthly.build(records, subject_pseudonym="PATIENT_x", year=2026, month=8)
        second = monthly.build(records, subject_pseudonym="PATIENT_x", year=2026, month=8)
        self.assertEqual(first, second)

    def test_monthly_module_does_not_use_an_llm(self):
        # ユーザー決定（2026-08-12）: 月報は決定論的な集計のみ。LLMを呼ばない。
        source = (_REPO_ROOT / "src" / "moegi" / "monthly.py").read_text(encoding="utf-8")
        for forbidden in ("LLMAdapter", "adapter.complete", "import llm"):
            self.assertNotIn(forbidden, source)

    def test_report_holds_no_real_name_or_subject_code(self):
        records = [_record("2026-08-03", procedures=["吸引"])]
        report = monthly.build(records, subject_pseudonym="PATIENT_x", year=2026, month=8)
        self.assertNotIn("code", repr(report))
        self.assertTrue(report.subject_pseudonym.startswith("PATIENT_"))


class MonthlyWebTest(unittest.TestCase):
    """承認UIの月報画面。利用者コードをURLに載せないことを含めて固定する。"""

    def setUp(self):
        from fastapi.testclient import TestClient

        from moegi import webapp

        self.webapp = webapp
        self.client = TestClient(webapp.app)

    def test_form_page_renders(self):
        response = self.client.get("/monthly")
        self.assertEqual(response.status_code, 200)
        self.assertIn("P-100234", response.text)

    def test_report_is_requested_by_post_not_get(self):
        # 利用者コードがURLのクエリ文字列・ブラウザ履歴・アクセスログに残らないようにする。
        routes = {
            (route.path, tuple(sorted(route.methods)))
            for route in self.webapp.app.routes
            if getattr(route, "path", "") == "/monthly"
        }
        self.assertIn(("/monthly", ("POST",)), routes)

    def test_empty_month_reports_no_records(self):
        with mock.patch.dict(os.environ, {vault.KEY_ENV_VAR: _TEST_VAULT_KEY}):
            response = self.client.post(
                "/monthly", data={"subject_code": "P-100987", "month": "1999-01"}
            )
        self.assertEqual(response.status_code, 200)
        # handoff §⑦ の空状態文言（旧:「…の日報がありません」）
        self.assertIn("日報はまだありません", response.text)

    def test_invalid_month_is_rejected_with_a_japanese_message(self):
        # 黙って当月に置き換えると、別の月の月報を見て「記録が無い」と勘違いする
        with mock.patch.dict(os.environ, {vault.KEY_ENV_VAR: _TEST_VAULT_KEY}):
            for bad in ("not-a-month", "2026-13"):
                response = self.client.post(
                    "/monthly", data={"subject_code": "P-100987", "month": bad}
                )
                self.assertEqual(response.status_code, 422, bad)
                self.assertIn("対象月の形式が正しくありません", response.text)


if __name__ == "__main__":
    unittest.main()
