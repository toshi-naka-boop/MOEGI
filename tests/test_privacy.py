"""仮名化・Privacy Gate の単体テスト（design/moegi/privacy.md §13）。

⚠️ ここで使うのはすべて合成データ。実在する患者・家族・職員・医療機関の情報は
   テストデータ・fixture・アサーションメッセージのどこにも書かない。

ローカルLLM・ORCA・Ollamaの実サービスには一切依存しない（_FakeAdapter / _FakeClient）。
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from moegi import llm, nippou, privacy, store
from moegi.privacy import dictionary as dict_module
from moegi.privacy import logsafe, recheck, rules, vault
from moegi.privacy.config import PrivacyConfig

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DICTIONARY_PATH = _REPO_ROOT / "config" / "pseudonym_dictionary_sample.toml"

#: テストの再現性のため、生年月日→年代の基準日を固定する。
_TODAY = (2026, 8, 12)

_TEST_VAULT_KEY = base64.urlsafe_b64encode(b"0" * 32).decode()


def _config(**overrides) -> PrivacyConfig:
    base = {
        "policy_version": "test-policy-1",
        "mode": "enforce",
        "max_input_chars": 2000,
        "llm_detection": False,
        "max_residual_risk": "low",
        "max_removal_ratio": 0.5,
        "retention_days": 90,
        "dictionary_path": _DICTIONARY_PATH,
    }
    base.update(overrides)
    return PrivacyConfig(**base)


class _FakeAdapter:
    """llm.LLMAdapter.complete(stage, prompt, opts) の契約を満たすダブル。"""

    def __init__(self, responses: list, *, timeout: bool = False, raises: bool = False):
        self._responses = list(responses)
        self._timeout = timeout
        self._raises = raises
        self.calls: list[tuple[str, str]] = []

    def complete(self, stage: str, prompt: str, opts: dict | None = None) -> dict:
        self.calls.append((stage, prompt))
        if self._raises:
            raise RuntimeError("ollama unreachable")
        if self._timeout:
            return {"text": "", "timeout": True, "backend": "local:test", "latency_ms": 1.0}
        return {
            "text": self._responses.pop(0),
            "timeout": False,
            "backend": "local:test",
            "latency_ms": 1.0,
        }


def _llm_payload(sanitized: str, **overrides) -> str:
    payload = {
        "sanitized_text": sanitized,
        "detected_entities": [],
        "residual_risk": "low",
        "needs_human_review": False,
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# §13 検出・仮名化
# ---------------------------------------------------------------------------


class RuleDetectionTest(unittest.TestCase):
    def _sanitize(self, text: str) -> str:
        return rules.apply_detections(text, rules.detect(text, today=_TODAY))

    def test_phone_number_is_tagged(self):
        self.assertIn(rules.TAG_PHONE, self._sanitize("連絡先は090-1234-5678です"))
        self.assertIn(rules.TAG_PHONE, self._sanitize("固定は03-5321-9999"))

    def test_vital_numbers_are_not_mistaken_for_phone_numbers(self):
        # §6「バイタル・処置は原則保持」。血圧・体温・脈拍が電話番号扱いされたら記録が壊れる。
        text = "血圧128/78、体温36.5度、脈拍70、呼吸数16、SpO2 98%"
        self.assertEqual(self._sanitize(text), text)

    def test_email_is_tagged(self):
        self.assertIn(rules.TAG_EMAIL, self._sanitize("kazoku@example.com へ連絡"))

    def test_postal_code_is_tagged(self):
        self.assertIn(rules.TAG_POSTAL, self._sanitize("〒160-0023 へ郵送"))

    def test_detailed_address_is_generalized_to_prefecture(self):
        result = self._sanitize("東京都新宿区西新宿2-8-1へ訪問した")
        self.assertIn("東京都", result)
        self.assertIn(rules.TAG_ADDRESS, result)
        self.assertNotIn("西新宿", result)

    def test_prefecture_alone_is_not_treated_as_detailed_address(self):
        # 既に地域レベルなので一般化の必要が無い。過剰に消すと記録の意味が落ちる。
        text = "東京都の制度改正について説明した"
        self.assertEqual(self._sanitize(text), text)

    def test_birthdate_becomes_age_bucket(self):
        self.assertIn("(80代)", self._sanitize("1943年5月3日生まれ"))
        self.assertIn("(80代)", self._sanitize("昭和18年5月3日生まれ"))

    def test_insurance_number_is_detected_as_blocking(self):
        detections = rules.detect("被保険者証番号 12345678 を確認", today=_TODAY)
        types = {d.type for d in detections}
        self.assertIn("insurance_number", types)
        self.assertTrue(any(d.action == rules.ACTION_BLOCK for d in detections))

    def test_medical_card_number_is_detected_as_blocking(self):
        detections = rules.detect("診察券番号 A-99881 を控えた", today=_TODAY)
        self.assertIn("medical_card_number", {d.type for d in detections})

    def test_patient_id_is_tagged(self):
        self.assertIn(rules.TAG_PATIENT_ID, self._sanitize("利用者ID P-100234 の記録"))

    def test_ip_address_is_tagged(self):
        self.assertIn(rules.TAG_IP, self._sanitize("端末は192.168.10.24からアクセス"))

    def test_url_credentials_are_detected_as_blocking(self):
        detections = rules.detect("https://user:secretpass@example.com/api", today=_TODAY)
        self.assertIn("url_credential", {d.type for d in detections})

    def test_api_key_is_detected_as_blocking(self):
        for sample in ("sk-abcdefghijklmnopqrstuvwx", "AKIAABCDEFGHIJKLMNOP"):
            with self.subTest(sample=sample):
                detections = rules.detect(f"キーは {sample} です", today=_TODAY)
                self.assertIn("api_key", {d.type for d in detections})

    def test_jwt_is_detected_as_blocking(self):
        token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1g"
        self.assertIn("jwt", {d.type for d in rules.detect(token, today=_TODAY)})

    def test_detections_are_deterministic(self):
        text = "090-1234-5678 と kazoku@example.com と 東京都新宿区西新宿2-8-1"
        self.assertEqual(self._sanitize(text), self._sanitize(text))


class DictionaryDetectionTest(unittest.TestCase):
    def setUp(self):
        self.dictionary = dict_module.load_dictionary(_DICTIONARY_PATH)

    def _sanitize(self, text: str):
        allocator = dict_module.PseudonymAllocator()
        detections = dict_module.detect(text, self.dictionary, allocator)
        return rules.apply_detections(text, detections), detections, allocator

    def test_patient_name_becomes_patient_pseudonym(self):
        result, _, _ = self._sanitize("山田太郎さんの訪問を実施")
        self.assertIn("PATIENT_001", result)
        self.assertNotIn("山田太郎", result)

    def test_family_name_becomes_relation(self):
        result, _, _ = self._sanitize("山田花子から連絡があった")
        self.assertIn("長女", result)
        self.assertNotIn("花子", result)

    def test_staff_name_becomes_nurse_pseudonym(self):
        result, _, _ = self._sanitize("森美咲が訪問した")
        self.assertIn("NURSE_001", result)

    def test_doctor_name_becomes_doctor_pseudonym(self):
        result, _, _ = self._sanitize("高橋誠先生に報告した")
        self.assertIn("DOCTOR_001", result)

    def test_facility_name_becomes_facility_pseudonym(self):
        result, _, _ = self._sanitize("みどり内科クリニックへ連絡")
        self.assertIn("FACILITY_001", result)

    def test_asr_garbled_name_is_detected_via_alias(self):
        # 音声認識で氏名が「やまだたろう」と平仮名で書き起こされた場合。
        result, _, _ = self._sanitize("やまだたろうさんは食欲良好")
        self.assertIn("PATIENT_001", result)
        self.assertNotIn("やまだたろう", result)

    def test_common_noun_colliding_with_surname_is_not_replaced(self):
        # 「森」は職員の姓だが、敬称が無い一般名詞としての「森」は置換しない。
        text = "自宅の裏の森まで散歩に出た"
        result, detections, _ = self._sanitize(text)
        self.assertEqual(result, text)
        self.assertEqual(detections, [])

    def test_surname_with_honorific_is_replaced(self):
        result, _, _ = self._sanitize("森さんが同行した")
        self.assertIn("NURSE_001", result)
        self.assertIn("さん", result)  # 敬称は残す

    def test_duplicate_full_names_require_human_review(self):
        # 同姓同名は一意に決められないので、検出はしても人の確認を必須にする。
        _, detections, _ = self._sanitize("鈴木一郎さんの記録")
        self.assertTrue(any(d.review_required for d in detections))

    def test_same_person_gets_same_pseudonym_within_one_request(self):
        result, _, _ = self._sanitize("山田太郎さんと話した。山田太郎さんは笑顔だった。")
        self.assertEqual(result.count("PATIENT_001"), 2)

    def test_registered_address_and_phone_are_tagged(self):
        result, _, _ = self._sanitize("東京都新宿区西新宿2-8-1 / 03-5321-1111")
        self.assertIn(rules.TAG_ADDRESS, result)
        self.assertIn(rules.TAG_PHONE, result)

    def test_mapping_records_pseudonym_to_real_name(self):
        _, _, allocator = self._sanitize("山田太郎さんと森美咲")
        mapping = allocator.mapping
        self.assertEqual(mapping["PATIENT_001"], "山田 太郎")
        self.assertEqual(mapping["NURSE_001"], "森 美咲")


class ClinicalInformationPreservationTest(unittest.TestCase):
    """§6「病名・症状・服薬・バイタル・処置は原則保持」。"""

    def test_clinical_content_survives_the_gate(self):
        text = (
            "褥瘡の処置を実施。ロキソニン60mgを内服。体温36.8度、血圧132/80。"
            "疼痛スケールは3。次回も体位交換を指導する。"
        )
        result = privacy.run_gate(text, config=_config(), today=_TODAY)
        self.assertEqual(result.status, privacy.PrivacyGateStatus.PASSED)
        for keyword in ("褥瘡", "ロキソニン", "36.8度", "132/80", "疼痛スケール", "体位交換"):
            self.assertIn(keyword, result.sanitized_text)


# ---------------------------------------------------------------------------
# §13 fail-closed
# ---------------------------------------------------------------------------


class GateFailClosedTest(unittest.TestCase):
    def test_empty_input_fails(self):
        result = privacy.run_gate("   ", config=_config(), today=_TODAY)
        self.assertEqual(result.status, privacy.PrivacyGateStatus.FAILED)
        self.assertIn("empty_input", result.reason_codes)
        self.assertFalse(privacy.cleared_for_downstream(result))

    def test_too_long_input_is_blocked(self):
        result = privacy.run_gate("あ" * 3000, config=_config(max_input_chars=100), today=_TODAY)
        self.assertEqual(result.status, privacy.PrivacyGateStatus.BLOCKED)
        self.assertIn("input_too_long", result.reason_codes)

    def test_mojibake_input_fails(self):
        result = privacy.run_gate("訪問し�た記録", config=_config(), today=_TODAY)
        self.assertEqual(result.status, privacy.PrivacyGateStatus.FAILED)
        self.assertIn("mojibake_input", result.reason_codes)

    def test_dictionary_load_failure_requires_review(self):
        config = _config(dictionary_path=Path("/nonexistent/dictionary.toml"))
        result = privacy.run_gate("訪問を実施した", config=config, today=_TODAY)
        self.assertIn("dictionary_unavailable", result.reason_codes)
        self.assertNotEqual(result.status, privacy.PrivacyGateStatus.PASSED)
        self.assertFalse(privacy.cleared_for_downstream(result))

    def test_secret_in_input_blocks_the_gate(self):
        result = privacy.run_gate(
            "連携用の sk-abcdefghijklmnopqrstuvwx を控えた", config=_config(), today=_TODAY
        )
        self.assertEqual(result.status, privacy.PrivacyGateStatus.BLOCKED)
        self.assertFalse(privacy.cleared_for_downstream(result))

    def test_insurance_number_blocks_the_gate(self):
        result = privacy.run_gate("被保険者証番号 12345678 を確認した", config=_config(), today=_TODAY)
        self.assertEqual(result.status, privacy.PrivacyGateStatus.BLOCKED)

    def test_llm_timeout_requires_review(self):
        adapter = _FakeAdapter([], timeout=True)
        result = privacy.run_gate(
            "訪問を実施した", config=_config(llm_detection=True), adapter=adapter, today=_TODAY
        )
        self.assertIn("llm_timeout", result.reason_codes)
        self.assertEqual(result.status, privacy.PrivacyGateStatus.REVIEW_REQUIRED)
        self.assertFalse(privacy.cleared_for_downstream(result))

    def test_llm_connection_failure_requires_review(self):
        adapter = _FakeAdapter([], raises=True)
        result = privacy.run_gate(
            "訪問を実施した", config=_config(llm_detection=True), adapter=adapter, today=_TODAY
        )
        self.assertIn("llm_unavailable", result.reason_codes)
        self.assertFalse(privacy.cleared_for_downstream(result))

    def test_llm_invalid_json_requires_review(self):
        adapter = _FakeAdapter(["これはJSONではありません"])
        result = privacy.run_gate(
            "訪問を実施した", config=_config(llm_detection=True), adapter=adapter, today=_TODAY
        )
        self.assertIn("llm_invalid_json", result.reason_codes)
        self.assertFalse(privacy.cleared_for_downstream(result))

    def test_llm_schema_violation_requires_review(self):
        # residual_risk が enum 外。スキーマ違反は自動で先へ進めない。
        adapter = _FakeAdapter([_llm_payload("訪問を実施した", residual_risk="とても低い")])
        result = privacy.run_gate(
            "訪問を実施した", config=_config(llm_detection=True), adapter=adapter, today=_TODAY
        )
        self.assertIn("schema_enum_residual_risk", result.reason_codes)
        self.assertFalse(privacy.cleared_for_downstream(result))

    def test_llm_missing_key_requires_review(self):
        adapter = _FakeAdapter([json.dumps({"sanitized_text": "x"})])
        result = privacy.run_gate(
            "訪問を実施した", config=_config(llm_detection=True), adapter=adapter, today=_TODAY
        )
        self.assertIn("schema_missing_key", result.reason_codes)

    def test_llm_reintroducing_identifier_is_caught_by_recheck(self):
        # ローカルLLMが（幻覚などで）電話番号を復活させたケース。再検査は
        # LLMから独立しているので、この事故を捕まえられる。
        adapter = _FakeAdapter([_llm_payload("連絡先は090-1234-5678です")])
        result = privacy.run_gate(
            "連絡先を確認した", config=_config(llm_detection=True), adapter=adapter, today=_TODAY
        )
        self.assertIn("residual_phone", result.reason_codes)
        self.assertFalse(privacy.cleared_for_downstream(result))

    def test_llm_leaving_registered_name_is_caught_by_recheck(self):
        adapter = _FakeAdapter([_llm_payload("山田太郎さんは食欲良好")])
        result = privacy.run_gate(
            "利用者は食欲良好", config=_config(llm_detection=True), adapter=adapter, today=_TODAY
        )
        self.assertIn("residual_registered_name", result.reason_codes)
        self.assertFalse(privacy.cleared_for_downstream(result))

    def test_excessive_removal_requires_review(self):
        # 臨床上重要な情報まで削除された可能性を検知する（§5.3の停止条件）。
        adapter = _FakeAdapter([_llm_payload("訪問")])
        long_text = "褥瘡の処置を実施し、バイタルは安定していた。次回も継続して観察する。"
        result = privacy.run_gate(
            long_text, config=_config(llm_detection=True), adapter=adapter, today=_TODAY
        )
        self.assertIn("excessive_removal", result.reason_codes)
        self.assertFalse(privacy.cleared_for_downstream(result))

    def test_llm_replacing_the_body_with_the_prompt_example_is_caught(self):
        # ledger R-86（実機実測で発見）: ローカル7Bモデルが、プロンプト内の出力例の文を
        # そのまま sanitized_text として返すことがある。文字数が近いと max_removal_ratio では
        # 捕まらないので、臨床情報の中核である数値が消えたことで検知する。
        adapter = _FakeAdapter([_llm_payload("PATIENT_001さんは昨日から発熱があります。長女から連絡がありました。")])
        result = privacy.run_gate(
            "血圧128/78、体温36.5度、脈拍70。ロキソニン60mgを内服中。膝の痛みは軽減。",
            config=_config(llm_detection=True),
            adapter=adapter,
            today=_TODAY,
        )
        self.assertIn("clinical_numbers_dropped", result.reason_codes)
        self.assertFalse(privacy.cleared_for_downstream(result))

    def test_pseudonym_numbers_do_not_trigger_the_clinical_number_check(self):
        # PATIENT_001 の 001 や [[PHONE]] は臨床情報ではないので、増減しても止めない。
        adapter = _FakeAdapter([_llm_payload("PATIENT_007さんの連絡先は[[PHONE]]。血圧128/78、体温36.5度。")])
        result = privacy.run_gate(
            "PATIENT_001さんの連絡先は090-1234-5678。血圧128/78、体温36.5度。",
            config=_config(llm_detection=True),
            adapter=adapter,
            today=_TODAY,
        )
        self.assertNotIn("clinical_numbers_dropped", result.reason_codes)

    def test_clinical_numbers_are_preserved_in_the_happy_path(self):
        adapter = _FakeAdapter([_llm_payload("PATIENT_001さん。血圧128/78、体温36.5度、脈拍70。")])
        result = privacy.run_gate(
            "山田太郎さん。血圧128/78、体温36.5度、脈拍70。",
            config=_config(llm_detection=True),
            adapter=adapter,
            today=_TODAY,
        )
        self.assertEqual(result.status, privacy.PrivacyGateStatus.PASSED)

    def test_high_residual_risk_requires_review(self):
        adapter = _FakeAdapter([_llm_payload("訪問を実施した", residual_risk="high")])
        result = privacy.run_gate(
            "訪問を実施した", config=_config(llm_detection=True), adapter=adapter, today=_TODAY
        )
        self.assertIn("residual_risk_above_threshold", result.reason_codes)

    def test_llm_needs_human_review_flag_is_honored(self):
        adapter = _FakeAdapter([_llm_payload("訪問を実施した", needs_human_review=True)])
        result = privacy.run_gate(
            "訪問を実施した", config=_config(llm_detection=True), adapter=adapter, today=_TODAY
        )
        self.assertEqual(result.status, privacy.PrivacyGateStatus.REVIEW_REQUIRED)

    def test_ambiguous_name_requires_review(self):
        result = privacy.run_gate("鈴木一郎さんの記録", config=_config(), today=_TODAY)
        self.assertIn("ambiguous_name", result.reason_codes)
        self.assertFalse(privacy.cleared_for_downstream(result))

    def test_vault_save_failure_fails_the_gate(self):
        closed = vault.connect(":memory:")
        closed.close()  # 閉じた接続へ書き込ませて保存失敗を作る
        with mock.patch.dict(os.environ, {vault.KEY_ENV_VAR: _TEST_VAULT_KEY}):
            result = privacy.run_gate(
                "山田太郎さんの訪問", config=_config(), vault_conn=closed, today=_TODAY
            )
        self.assertEqual(result.status, privacy.PrivacyGateStatus.FAILED)
        self.assertIn("vault_save_failed", result.reason_codes)
        self.assertFalse(privacy.cleared_for_downstream(result))


class GateModeTest(unittest.TestCase):
    """§7 段階導入。observe / shadow は止めないが、外部送信の判定は緩めない。"""

    def _blocked_result(self, mode: str) -> privacy.GateResult:
        return privacy.run_gate(
            "被保険者証番号 12345678", config=_config(mode=mode), today=_TODAY
        )

    def test_enforce_blocks_downstream(self):
        self.assertFalse(privacy.cleared_for_downstream(self._blocked_result("enforce")))

    def test_observe_allows_downstream_but_records_would_block(self):
        result = self._blocked_result("observe")
        self.assertTrue(privacy.cleared_for_downstream(result))
        self.assertTrue(result.would_block)

    def test_shadow_allows_downstream_but_records_would_block(self):
        result = self._blocked_result("shadow")
        self.assertTrue(privacy.cleared_for_downstream(result))
        self.assertTrue(result.would_block)

    def test_external_transmission_is_never_relaxed_by_mode(self):
        for mode in ("observe", "shadow", "enforce"):
            with self.subTest(mode=mode):
                result = self._blocked_result(mode)
                self.assertFalse(privacy.cleared_for_external_transmission(result))
                with self.assertRaises(privacy.ExternalTransmissionBlocked):
                    privacy.assert_cleared_for_external_transmission(result)

    def test_passed_result_clears_external_boundary(self):
        result = privacy.run_gate("バイタルは安定していた", config=_config(), today=_TODAY)
        self.assertEqual(result.status, privacy.PrivacyGateStatus.PASSED)
        self.assertTrue(privacy.cleared_for_external_transmission(result))
        privacy.assert_cleared_for_external_transmission(result)  # 例外が出ないこと

    def test_mode_cannot_be_set_from_request_parameters(self):
        # run_gate は mode 引数を受け取らない。リクエスト由来の値でモードを
        # 上書きする経路が存在しないことを、シグネチャで固定する。
        import inspect

        params = inspect.signature(privacy.run_gate).parameters
        self.assertNotIn("mode", params)


class RecheckTest(unittest.TestCase):
    def setUp(self):
        self.dictionary = dict_module.load_dictionary(_DICTIONARY_PATH)

    def test_clean_text_passes(self):
        result = recheck.run(
            "PATIENT_001は食欲良好。血圧128/78。", dictionary=self.dictionary, today=_TODAY
        )
        self.assertTrue(result.passed)

    def test_residual_phone_fails(self):
        result = recheck.run("連絡先090-1234-5678", dictionary=self.dictionary, today=_TODAY)
        self.assertFalse(result.passed)
        self.assertIn("phone", result.residual_counts)

    def test_residual_registered_name_fails(self):
        result = recheck.run("山田太郎さん", dictionary=self.dictionary, today=_TODAY)
        self.assertFalse(result.passed)
        self.assertEqual(result.residual_dictionary_hits, 1)

    def test_pseudonyms_and_tags_do_not_trigger_recheck(self):
        text = "PATIENT_001さん、NURSE_001が訪問。連絡先は[[PHONE]]、住所は東京都[[ADDRESS]]。"
        self.assertTrue(recheck.run(text, dictionary=self.dictionary, today=_TODAY).passed)

    def test_recheck_runs_even_without_dictionary(self):
        result = recheck.run("090-1234-5678", dictionary=None, today=_TODAY)
        self.assertFalse(result.passed)


# ---------------------------------------------------------------------------
# §13 ログ・外部通信
# ---------------------------------------------------------------------------


class LogSanitizerTest(unittest.TestCase):
    def test_phone_and_email_are_masked(self):
        masked = logsafe.sanitize("連絡先 090-1234-5678 / kazoku@example.com")
        self.assertNotIn("090-1234-5678", masked)
        self.assertNotIn("kazoku@example.com", masked)

    def test_api_key_is_masked(self):
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwx", logsafe.sanitize("key=sk-abcdefghijklmnopqrstuvwx"))

    def test_long_body_is_truncated(self):
        masked = logsafe.sanitize("あ" * 5000)
        self.assertLessEqual(len(masked), logsafe.MAX_LOG_CHARS + 32)

    def test_filter_masks_log_record_message(self):
        stream = io.StringIO()
        logger = logging.getLogger("moegi.test.sanitizer")
        logger.handlers = []
        logger.filters = []
        logger.propagate = False
        logger.setLevel(logging.INFO)
        handler = logging.StreamHandler(stream)
        logger.addHandler(handler)
        logger.addFilter(logsafe.SanitizingFilter())

        logger.info("発話: 連絡先は090-1234-5678です")
        self.assertNotIn("090-1234-5678", stream.getvalue())

    def test_filter_masks_exception_message(self):
        # §13「例外に原文が含まれない」。例外メッセージに発話が載っても外へ出さない。
        stream = io.StringIO()
        logger = logging.getLogger("moegi.test.sanitizer.exc")
        logger.handlers = []
        logger.filters = []
        logger.propagate = False
        logger.setLevel(logging.ERROR)
        logger.addHandler(logging.StreamHandler(stream))
        logger.addFilter(logsafe.SanitizingFilter())

        try:
            raise ValueError("解析失敗: 090-1234-5678 / kazoku@example.com")
        except ValueError:
            logger.exception("構造化に失敗しました")

        output = stream.getvalue()
        self.assertNotIn("090-1234-5678", output)
        self.assertNotIn("kazoku@example.com", output)

    def test_frontend_does_not_log_to_the_browser_console(self):
        # §13「フロントエンドコンソールへ本文が出ない」。録音UIのJSに console.* を置かない
        # （録音データ・発話テキストがブラウザのコンソールへ流れる経路を作らない）。
        source = (_REPO_ROOT / "src" / "moegi" / "webapp.py").read_text(encoding="utf-8")
        for call in ("console.log", "console.debug", "console.info", "console.error"):
            self.assertNotIn(call, source)

    def test_audit_fields_drops_unknown_keys(self):
        fields = logsafe.audit_fields(
            request_id="r1", gate_status="passed", text="発話原文", mapping={"PATIENT_001": "山田 太郎"}
        )
        self.assertEqual(set(fields), {"request_id", "gate_status"})


class GateResultLeakageTest(unittest.TestCase):
    """GateResult・監査記録に原文と対応表が入らないことを構造的に固定する。"""

    def test_gate_result_does_not_carry_original_text(self):
        original = "山田太郎さんの電話は090-1234-5678"
        result = privacy.run_gate(original, config=_config(), today=_TODAY)
        serialized = repr(result)
        self.assertNotIn("山田太郎", serialized)
        self.assertNotIn("090-1234-5678", serialized)

    def test_gate_result_has_no_mapping_attribute(self):
        result = privacy.run_gate("山田太郎さんの訪問", config=_config(), today=_TODAY)
        self.assertFalse(hasattr(result, "mapping"))

    def test_detected_counts_hold_only_types_and_counts(self):
        result = privacy.run_gate(
            "山田太郎さんの電話は090-1234-5678", config=_config(), today=_TODAY
        )
        for key, value in result.detected_counts.items():
            self.assertIsInstance(key, str)
            self.assertIsInstance(value, int)

    def test_audit_record_contains_no_body_text(self):
        conn = store.connect(":memory:")
        try:
            result = privacy.run_gate(
                "山田太郎さんの電話は090-1234-5678", config=_config(), today=_TODAY
            )
            store.save_privacy_audit(
                conn,
                request_id=result.request_id,
                gate_status=result.status.value,
                mode=result.mode,
                policy_version=result.policy_version,
                detected_counts=result.detected_counts,
                needs_human_review=result.needs_human_review,
                residual_risk=result.residual_risk,
                reason_codes=result.reason_codes,
                duration_ms=result.duration_ms,
                proceeded_to_structuring=True,
            )
            dumped = "".join(str(row) for row in conn.execute("SELECT * FROM privacy_audits"))
            self.assertNotIn("山田太郎", dumped)
            self.assertNotIn("090-1234-5678", dumped)
        finally:
            conn.close()


class LocalOnlyRoutingTest(unittest.TestCase):
    """§13「ローカルモードで外部LLM通信が発生しない」。"""

    def test_pseudonymize_stage_is_local_only(self):
        self.assertIn("pseudonymize", llm.LOCAL_ONLY_STAGES)

    def test_models_toml_rejects_cloud_backend_for_pseudonymize(self):
        # 設定でクラウドを指定しても起動時に拒否される（設定ミスで未仮名化の発話が
        # 外部へ出ることが構造的に起こらない）。
        config_text = """
dev_local_only = false

[diff]
backend = "local"
model = "m"

[requirement]
backend = "local"
model = "m"

[matching]
backend = "local"
model = "m"

[draft]
backend = "local"
model = "m"

[nippou]
backend = "local"
model = "m"

[pseudonymize]
backend = "orca"
model = "some-cloud-model"
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "models.toml"
            path.write_text(config_text, encoding="utf-8")
            with self.assertRaises(llm.ConfigError):
                llm.load_model_config(path)

    def test_repository_models_toml_keeps_pseudonymize_local(self):
        cfg = llm.load_model_config(_REPO_ROOT / "config" / "models.toml")
        self.assertEqual(cfg["stages"]["pseudonymize"].backend, "local")
        self.assertEqual(cfg["stages"]["nippou"].backend, "local")

    def test_pseudonymize_request_goes_to_local_endpoint(self):
        posted: list[str] = []

        class _FakeResponse:
            headers: dict = {}

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "choices": [{"message": {"content": "{}"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }

        class _FakeClient:
            def post(self, url, **kwargs):
                posted.append(url)
                return _FakeResponse()

            def close(self):
                pass

        adapter = llm.LLMAdapter(http_client=_FakeClient())
        adapter.complete("pseudonymize", "prompt")
        adapter.complete("nippou", "prompt")
        self.assertTrue(posted)
        for url in posted:
            self.assertNotIn("orcarouter", url)
            self.assertIn("11434", url)

    def test_gate_reports_no_external_transmission(self):
        result = privacy.run_gate("バイタルは安定していた", config=_config(), today=_TODAY)
        self.assertFalse(result.external_transmission)

    def test_dictionary_contents_are_not_sent_to_the_llm(self):
        # §5.2「辞書データを外部へ送信してはいけない」。ローカルLLMにも辞書は渡さない。
        adapter = _FakeAdapter([_llm_payload("PATIENT_001さんの訪問")])
        privacy.run_gate(
            "山田太郎さんの訪問", config=_config(llm_detection=True), adapter=adapter, today=_TODAY
        )
        _, prompt = adapter.calls[0]
        self.assertNotIn("山田", prompt)
        self.assertNotIn("みどり内科クリニック", prompt)

    def test_llm_receives_already_pseudonymized_text(self):
        adapter = _FakeAdapter([_llm_payload("PATIENT_001さんの連絡先は[[PHONE]]")])
        privacy.run_gate(
            "山田太郎さんの連絡先は090-1234-5678",
            config=_config(llm_detection=True),
            adapter=adapter,
            today=_TODAY,
        )
        _, prompt = adapter.calls[0]
        self.assertNotIn("090-1234-5678", prompt)


# ---------------------------------------------------------------------------
# §9 仮名対応表
# ---------------------------------------------------------------------------


class VaultTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.key_path = Path(self._tmp.name) / ".vault_key"
        self.conn = vault.connect(Path(self._tmp.name) / "vault.db")

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_roundtrip(self):
        mapping = {"PATIENT_001": "山田 太郎", "NURSE_001": "森 美咲"}
        vault.save_mapping(
            self.conn,
            request_id="req-1",
            mapping=mapping,
            policy_version="test",
            key_path=self.key_path,
        )
        self.assertEqual(
            vault.decrypt_mapping(self.conn, "req-1", key_path=self.key_path), mapping
        )

    def test_stored_ciphertext_does_not_contain_plaintext(self):
        vault.save_mapping(
            self.conn,
            request_id="req-1",
            mapping={"PATIENT_001": "山田 太郎"},
            policy_version="test",
            key_path=self.key_path,
        )
        blob = self.conn.execute("SELECT ciphertext FROM pseudonym_maps").fetchone()[0]
        self.assertNotIn("山田".encode(), blob)

    def test_key_file_is_owner_only(self):
        vault.save_mapping(
            self.conn,
            request_id="req-1",
            mapping={"PATIENT_001": "山田 太郎"},
            policy_version="test",
            key_path=self.key_path,
        )
        self.assertEqual(self.key_path.stat().st_mode & 0o777, 0o600)

    def test_ciphertext_is_bound_to_request_id(self):
        vault.save_mapping(
            self.conn,
            request_id="req-1",
            mapping={"PATIENT_001": "山田 太郎"},
            policy_version="test",
            key_path=self.key_path,
        )
        self.conn.execute("UPDATE pseudonym_maps SET request_id = 'req-2'")
        with self.assertRaises(vault.VaultError):
            vault.decrypt_mapping(self.conn, "req-2", key_path=self.key_path)

    def test_delete_mapping(self):
        vault.save_mapping(
            self.conn, request_id="req-1", mapping={"A": "B"}, policy_version="t", key_path=self.key_path
        )
        self.assertEqual(vault.delete_mapping(self.conn, "req-1"), 1)
        self.assertIsNone(vault.decrypt_mapping(self.conn, "req-1", key_path=self.key_path))

    def test_purge_expired(self):
        vault.save_mapping(
            self.conn, request_id="old", mapping={"A": "B"}, policy_version="t", key_path=self.key_path
        )
        self.conn.execute("UPDATE pseudonym_maps SET created_at = datetime('now', '-400 days')")
        self.conn.commit()
        self.assertEqual(vault.purge_expired(self.conn, 90), 1)
        self.assertEqual(vault.count(self.conn), 0)

    def test_vault_is_a_separate_database_from_the_main_store(self):
        # §9「原文・仮名対応表・仮名化済み文章を論理的に分離する」。
        self.assertNotEqual(vault.DEFAULT_VAULT_DB_PATH, store.DEFAULT_DB_PATH)

    def test_gate_saves_mapping_when_vault_is_available(self):
        with mock.patch.dict(os.environ, {vault.KEY_ENV_VAR: _TEST_VAULT_KEY}):
            result = privacy.run_gate(
                "山田太郎さんの訪問", config=_config(), vault_conn=self.conn, today=_TODAY
            )
            self.assertTrue(result.mapping_saved)
            mapping = vault.decrypt_mapping(self.conn, result.request_id)
        self.assertEqual(mapping["PATIENT_001"], "山田 太郎")


# ---------------------------------------------------------------------------
# §13 既存処理との接続（fail-closed）と回帰
# ---------------------------------------------------------------------------


class DownstreamWiringTest(unittest.TestCase):
    """Privacy Gate 不合格時に既存の日報・SOAP処理が呼ばれないこと。"""

    def setUp(self):
        from moegi import webapp

        self.webapp = webapp

    def _failing_result(self) -> privacy.GateResult:
        return privacy.run_gate("被保険者証番号 12345678", config=_config(), today=_TODAY)

    def _context(self, *, want_soap: bool = True, subject_code: str = "P-100234"):
        return self.webapp._RecordContext(
            procedures=[], want_soap=want_soap, subject_code=subject_code, visited_on="2026-08-12"
        )

    def test_structure_is_not_called_when_gate_fails(self):
        result = self._failing_result()
        self.assertEqual(result.status, privacy.PrivacyGateStatus.BLOCKED)
        with (
            mock.patch.object(self.webapp, "_save_privacy_audit") as audit,
            mock.patch.object(self.webapp, "_save_nippou_record") as save_record,
            mock.patch.object(self.webapp.nippou, "NippouAgent") as agent_cls,
            mock.patch.object(self.webapp.llm, "LLMAdapter") as adapter_cls,
        ):
            response = self.webapp._structure_and_render(result, self._context())
        agent_cls.assert_not_called()
        adapter_cls.assert_not_called()
        # 不合格の記録は月報の集計材料にもしない
        save_record.assert_not_called()
        audit.assert_called_once()
        self.assertEqual(audit.call_args.kwargs["proceeded"], False)
        self.assertEqual(response.status_code, 200)

    def test_blocked_page_offers_recheck_and_hides_original_values(self):
        result = self._failing_result()
        with (
            mock.patch.object(self.webapp, "_save_privacy_audit"),
            mock.patch.object(self.webapp, "_save_nippou_record"),
            mock.patch.object(self.webapp.nippou, "NippouAgent"),
            mock.patch.object(self.webapp.llm, "LLMAdapter"),
        ):
            body = self.webapp._structure_and_render(result, self._context()).body.decode()
        self.assertIn("/nippou/recheck", body)
        self.assertIn("セキュリティ検査で停止", body)
        self.assertNotIn("12345678", body)

    def test_structure_receives_sanitized_text_when_gate_passes(self):
        result = privacy.run_gate("山田太郎さんはバイタル安定", config=_config(), today=_TODAY)
        self.assertEqual(result.status, privacy.PrivacyGateStatus.PASSED)
        with (
            mock.patch.object(self.webapp, "_save_privacy_audit"),
            mock.patch.object(self.webapp, "_save_nippou_record"),
            mock.patch.object(self.webapp.llm, "LLMAdapter"),
            mock.patch.object(self.webapp.nippou, "NippouAgent") as agent_cls,
        ):
            agent = agent_cls.return_value
            agent.structure.return_value = nippou.NippouRecord(
                checklist_version="v", utterance_id="u"
            )
            agent.structure_soap.return_value = nippou.SoapRecord(utterance_id="u")
            self.webapp._structure_and_render(result, self._context())
        passed_text = agent.structure.call_args.args[0]
        self.assertIn("PATIENT_001", passed_text)
        self.assertNotIn("山田太郎", passed_text)
        agent.structure_soap.assert_called_once()

    def test_soap_is_skipped_when_not_requested(self):
        result = privacy.run_gate("バイタル安定", config=_config(), today=_TODAY)
        with (
            mock.patch.object(self.webapp, "_save_privacy_audit"),
            mock.patch.object(self.webapp, "_save_nippou_record"),
            mock.patch.object(self.webapp.llm, "LLMAdapter"),
            mock.patch.object(self.webapp.nippou, "NippouAgent") as agent_cls,
        ):
            agent = agent_cls.return_value
            agent.structure.return_value = nippou.NippouRecord(checklist_version="v", utterance_id="u")
            self.webapp._structure_and_render(result, self._context(want_soap=False))
        agent.structure_soap.assert_not_called()

    def test_endpoints_do_not_accept_a_mode_parameter(self):
        import inspect

        for handler in (self.webapp.nippou_structure, self.webapp.nippou_recheck):
            with self.subTest(handler=handler.__name__):
                params = inspect.signature(handler).parameters
                self.assertNotIn("mode", params)
                self.assertNotIn("privacy_mode", params)
                self.assertNotIn("skip_privacy", params)


class SoapStructuringTest(unittest.TestCase):
    """SOAP構造化（新規機能）。日報と同じ規律で作られていること。"""

    def test_returns_four_sections(self):
        adapter = _FakeAdapter(
            [
                json.dumps(
                    {
                        "subjective": "腰の痛みが楽になったとの訴え",
                        "objective": "血圧128/78、体温36.5度",
                        "assessment": "疼痛はコントロールできている",
                        "plan": "体位交換の指導を継続",
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = nippou.NippouAgent(adapter)
        record = agent.structure_soap("PATIENT_001さんの訪問", utterance_id="u1", procedures=["吸引"])
        self.assertEqual(record.subjective, "腰の痛みが楽になったとの訴え")
        self.assertIn("128/78", record.objective)
        self.assertEqual(record.procedures, ["吸引"])
        self.assertFalse(record.undecidable)

    def test_uses_local_only_stage(self):
        adapter = _FakeAdapter([json.dumps({"subjective": "", "objective": "", "assessment": "", "plan": ""})])
        nippou.NippouAgent(adapter).structure_soap("テキスト")
        stage, _ = adapter.calls[0]
        self.assertEqual(stage, "nippou")
        self.assertIn(stage, llm.LOCAL_ONLY_STAGES)

    def test_timeout_marks_undecidable_without_raising(self):
        agent = nippou.NippouAgent(_FakeAdapter([], timeout=True))
        record = agent.structure_soap("テキスト", procedures=["点滴"])
        self.assertTrue(record.undecidable)
        self.assertEqual(record.procedures, ["点滴"])

    def test_malformed_response_marks_undecidable(self):
        agent = nippou.NippouAgent(_FakeAdapter(["これはJSONではない"]))
        self.assertTrue(agent.structure_soap("テキスト").undecidable)

    def test_prompt_instructs_to_keep_pseudonyms(self):
        adapter = _FakeAdapter([json.dumps({"subjective": "", "objective": "", "assessment": "", "plan": ""})])
        nippou.NippouAgent(adapter).structure_soap("PATIENT_001さん")
        _, prompt = adapter.calls[0]
        self.assertIn("PATIENT_001", prompt)
        self.assertIn("そのまま書き写す", prompt)


class ExistingBehaviourRegressionTest(unittest.TestCase):
    """§13 回帰テスト: 既存の日報構造化の契約が変わっていないこと。"""

    def test_nippou_record_schema_is_backward_compatible(self):
        """既存フィールドが消えず順序も変わらないこと、追加分は既定値を持つこと。

        完全一致で固定すると、後方互換な追加（ledger R-88 の checklist_item_key など）
        まで落ちてしまい、テストが「壊れていないのに赤くなる」側に倒れる。
        守りたいのは「既存の呼び出し側が壊れないこと」なので、そちらを検査する。
        """
        import dataclasses

        for cls, original in (
            (
                nippou.NippouRecord,
                ["checklist_version", "utterance_id", "items", "evaluation", "procedures"],
            ),
            (nippou.NippouItem, ["checklist_item_id", "value", "filled"]),
        ):
            with self.subTest(cls=cls.__name__):
                names = [f.name for f in dataclasses.fields(cls)]
                self.assertEqual(names[: len(original)], original)
                for field in dataclasses.fields(cls)[len(original) :]:
                    has_default = (
                        field.default is not dataclasses.MISSING
                        or field.default_factory is not dataclasses.MISSING
                    )
                    self.assertTrue(has_default, f"{cls.__name__}.{field.name} に既定値がありません")

    def test_structure_still_works_after_the_gate(self):
        adapter = _FakeAdapter(
            [json.dumps({"items": [{"item_id": "1", "filled": True, "value": "血圧128/78"}]})]
        )
        agent = nippou.NippouAgent(adapter)
        from moegi.pipeline import ChecklistItem

        agent.set_checklist([ChecklistItem(sitedoc_id="s", label="バイタルサインの記録", kind="自由記述")])
        gate_result = privacy.run_gate(
            "山田太郎さん。血圧128/78。", config=_config(), today=_TODAY
        )
        record = agent.structure(gate_result.sanitized_text)
        self.assertTrue(record.items[0].filled)
        self.assertIn("128/78", record.items[0].value)

    def test_existing_stages_still_load_without_a_pseudonymize_section(self):
        # 既存の models.toml（[pseudonymize] 無し）でも起動できること。
        config_text = """
dev_local_only = false

[diff]
backend = "local"
model = "m"

[requirement]
backend = "local"
model = "m"

[matching]
backend = "local"
model = "m"

[draft]
backend = "local"
model = "m"

[nippou]
backend = "local"
model = "m"
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "models.toml"
            path.write_text(config_text, encoding="utf-8")
            cfg = llm.load_model_config(path)
        self.assertEqual(cfg["stages"]["pseudonymize"].backend, "local")


class ConfigTest(unittest.TestCase):
    def test_repository_config_loads(self):
        config = privacy.load_privacy_config()
        self.assertIn(config.mode, privacy.MODES)
        self.assertTrue(config.policy_version)

    def test_unknown_mode_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "privacy.toml"
            path.write_text('mode = "off"\n', encoding="utf-8")
            with self.assertRaises(privacy.PrivacyConfigError):
                privacy.load_privacy_config(path)

    def test_mode_can_be_overridden_by_admin_environment_variable(self):
        with mock.patch.dict(os.environ, {privacy.MODE_ENV_VAR: "observe"}):
            self.assertEqual(privacy.load_privacy_config().mode, "observe")

    def test_risk_threshold_comparison(self):
        config = _config(max_residual_risk="low")
        self.assertFalse(config.risk_exceeds_threshold("low"))
        self.assertTrue(config.risk_exceeds_threshold("medium"))
        self.assertTrue(config.risk_exceeds_threshold("unknown"))


class WordingTest(unittest.TestCase):
    """§6「匿名化済み」「個人情報ではない」「完全に安全」と書かない。"""

    FORBIDDEN = ("匿名化済み", "個人情報ではない", "完全に安全", "完全匿名")

    def test_source_and_docs_avoid_overclaiming_wording(self):
        targets = list((_REPO_ROOT / "src" / "moegi" / "privacy").glob("*.py"))
        targets += [
            _REPO_ROOT / "src" / "moegi" / "webapp.py",
            _REPO_ROOT / "config" / "privacy.toml",
            _REPO_ROOT / "design" / "moegi" / "privacy.md",
        ]
        for path in targets:
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            for phrase in self.FORBIDDEN:
                with self.subTest(path=path.name, phrase=phrase):
                    # 「〜と書かない」という説明文自体は許容する
                    occurrences = [
                        line for line in text.splitlines() if phrase in line and "書かない" not in line
                    ]
                    self.assertEqual(occurrences, [], f"{path.name} に「{phrase}」が含まれています")


if __name__ == "__main__":
    unittest.main()
