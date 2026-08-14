"""ORCA Guardrails 連携のテスト（ledger R-91）。

対象:
  1. llm.py の guardrail_blocked ハンドリング（skip-retry・breaker非加算・監査フラグ）
  2. scripts/setup_orca_guardrail.py のポリシー定義（RE2安全性・発火の妥当性）

外部サービスには接続しない。合成データのみ。
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

from moegi import llm, store

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import setup_orca_guardrail as guardrail_script  # noqa: E402


CONFIG = """
dev_local_only = false

[diff]
backend = "orca"
model = "candidate-a"
cloud_fallback_model = "orcarouter/auto"
fallback_backend = "local"
fallback_model = "local-model"

[requirement]
backend = "local"
model = "local-model"

[matching]
backend = "local"
model = "local-model"

[draft]
backend = "local"
model = "local-model"

[nippou]
backend = "local"
model = "local-model"
"""


def _chat_body(text: str) -> dict:
    return {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


class _Response:
    def __init__(self, json_data: dict, status_code: int = 200, headers: dict | None = None):
        self._json_data = json_data
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("error", request=None, response=None)

    def json(self) -> dict:
        return self._json_data


class _Client:
    def __init__(self):
        self.calls: list[str] = []
        self.orca_queue: list = []
        self.local_response = _Response(_chat_body("ローカル応答"))

    def post(self, url: str, *, headers=None, json=None, timeout=None):
        self.calls.append(url)
        if "orcarouter" in url:
            item = self.orca_queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return self.local_response

    def close(self) -> None:
        pass


def _blocked_response() -> _Response:
    """ORCAドキュメントどおりの遮断応答: HTTP 400 + error code guardrail_blocked。"""
    return _Response({"error": {"code": "guardrail_blocked", "message": "blocked by pii-shield"}}, 400)


def _adapter(client: _Client) -> llm.LLMAdapter:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "models.toml"
        path.write_text(CONFIG, encoding="utf-8")
        adapter = llm.LLMAdapter(config_path=path, http_client=client)
    adapter._orca_api_key_cache = "sk-orca-test-dummy"  # キーチェーン参照を避ける
    return adapter


class GuardrailBlockedHandlingTest(unittest.TestCase):
    def test_blocked_primary_falls_back_to_local_without_cloud_retry(self):
        """skip-retry: 遮断されたら cloud_fallback_model を試さずローカルへ。"""
        client = _Client()
        client.orca_queue = [_blocked_response()]
        result = _adapter(client).complete("diff", "プロンプト")
        self.assertTrue(result["guardrail_blocked"])
        self.assertTrue(result["degraded"])
        self.assertEqual(result["fallback_level"], 2)
        self.assertTrue(result["backend"].startswith("local:"))
        # ORCAへは1回だけ（cloud_fallback_modelの再試行なし）、あとはローカル1回
        orca_calls = [u for u in client.calls if "orcarouter" in u]
        self.assertEqual(len(orca_calls), 1)

    def test_blocked_does_not_count_toward_circuit_breaker(self):
        """ゲートウェイは健全に応答している＝到達性の障害ではない。"""
        client = _Client()
        client.orca_queue = [_blocked_response()] * llm.CIRCUIT_BREAKER_THRESHOLD
        adapter = _adapter(client)
        for _ in range(llm.CIRCUIT_BREAKER_THRESHOLD):
            adapter.complete("diff", "プロンプト")
        self.assertEqual(adapter._consecutive_failures, 0)
        self.assertIsNone(adapter._breaker_open_until)

    def test_blocked_cloud_fallback_also_goes_local(self):
        """主モデルは通常エラー→cloud_fallbackが遮断、のケースでもローカルへ落ちる。"""
        import httpx

        client = _Client()
        client.orca_queue = [httpx.ConnectError("down"), _blocked_response()]
        result = _adapter(client).complete("diff", "プロンプト")
        self.assertTrue(result["guardrail_blocked"])
        self.assertTrue(result["backend"].startswith("local:"))

    def test_ordinary_400_is_not_treated_as_guardrail_block(self):
        """error code が guardrail_blocked でない400は従来どおりのフォールバック経路。"""
        client = _Client()
        client.orca_queue = [
            _Response({"error": {"code": "invalid_request"}}, 400),
            _Response(_chat_body("別プロバイダ応答")),
        ]
        result = _adapter(client).complete("diff", "プロンプト")
        self.assertFalse(result["guardrail_blocked"])
        self.assertEqual(result["fallback_level"], 1)  # cloud_fallback_model が成功

    def test_success_result_carries_guardrail_blocked_false(self):
        client = _Client()
        client.orca_queue = [_Response(_chat_body("応答"))]
        result = _adapter(client).complete("diff", "プロンプト")
        self.assertFalse(result["guardrail_blocked"])

    def test_exception_message_contains_no_prompt_content(self):
        exc = llm.OrcaGuardrailBlocked("ORCA Guardrailsが入力を遮断しました")
        self.assertNotIn("プロンプト", str(exc))

    def test_blocked_flag_is_persisted_to_cost_records(self):
        conn = store.connect(":memory:")
        try:
            store.record_from_llm_result(
                conn,
                "run-1",
                "diff",
                {
                    "backend": "local:local-model",
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    "cost_jpy": 0.0,
                    "latency_ms": 1.0,
                    "degraded": True,
                    "resolved_model": "local-model",
                    "fallback_level": 2,
                    "timeout": False,
                    "guardrail_blocked": True,
                },
            )
            row = conn.execute(
                "SELECT guardrail_blocked FROM cost_records WHERE run_id='run-1'"
            ).fetchone()
            self.assertEqual(row[0], 1)
        finally:
            conn.close()

    def test_migration_adds_column_to_legacy_database(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.db"
            legacy = sqlite3.connect(path)
            legacy.execute(
                "CREATE TABLE cost_records (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " run_id TEXT NOT NULL, stage TEXT NOT NULL, backend TEXT NOT NULL,"
                " tokens_in INTEGER NOT NULL, tokens_out INTEGER NOT NULL,"
                " cost_jpy REAL NOT NULL, latency_ms REAL NOT NULL, degraded INTEGER NOT NULL,"
                " resolved_model TEXT NOT NULL, fallback_level INTEGER NOT NULL,"
                " timeout INTEGER NOT NULL DEFAULT 0,"
                " created_at TEXT NOT NULL DEFAULT (datetime('now')))"
            )
            legacy.commit()
            legacy.close()
            conn = store.connect(path)
            try:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(cost_records)")}
                self.assertIn("guardrail_blocked", columns)
            finally:
                conn.close()


class GuardrailPolicyTest(unittest.TestCase):
    """scripts/setup_orca_guardrail.py のポリシー定義。"""

    def test_patterns_are_re2_safe(self):
        # RE2は lookaround・後方参照を持たない。混入したらORCA側で保存できない。
        for entity in guardrail_script.CUSTOM_ENTITIES:
            with self.subTest(entity=entity["name"]):
                pattern = entity["pattern"]
                for forbidden in ("(?=", "(?!", "(?<", "\\1", "\\2"):
                    self.assertNotIn(forbidden, pattern)
                re.compile(pattern)  # Pythonでもコンパイルできること（RE2構文の下位互換）

    def test_patterns_fire_on_synthetic_identifiers(self):
        samples = {
            "jp_insurance_number": "被保険者証番号 12345678 を確認",
            "jp_medical_card_number": "診察券番号 A-99881 を控えた",
            "jp_patient_id": "利用者ID P-100234 の記録",
            "jp_phone": "連絡先は090-1234-5678です",
            "jp_postal_code": "〒160-0023 へ郵送",
            "jp_wareki_birthdate": "昭和18年5月3日生まれ",
        }
        patterns = {e["name"]: e["pattern"] for e in guardrail_script.CUSTOM_ENTITIES}
        for name, text in samples.items():
            with self.subTest(name=name):
                self.assertIsNotNone(re.search(patterns[name], text))

    def test_patterns_do_not_fire_on_clinical_text(self):
        # §6「病名・症状・服薬・バイタルは原則保持」——第二防衛線が通常入力を殺さないこと。
        clinical = (
            "指定訪問看護は、1回あたり30分から1時間30分を標準として行うものとする。"
            "血圧128/78、体温36.5度、脈拍70、SpO2 98%。ロキソニン60mgを内服。"
        )
        for entity in guardrail_script.CUSTOM_ENTITIES:
            with self.subTest(entity=entity["name"]):
                self.assertIsNone(re.search(entity["pattern"], clinical))

    def test_blocking_actions_match_local_policy(self):
        # ローカルの仮名化ポリシー（rules.BLOCKING_TYPES相当）と方針が揃っていること:
        # 保険証・診察券番号・キー・JWTは block、連絡先は mask。
        # ⚠️ entity_actionsは組み込みエンティティ専用（コンソール実測）のため、
        # 保険証・診察券番号のblockは先頭のregexルールが担う。
        payload = guardrail_script.build_guardrail_payload()
        block_rule, mask_rule = payload["rules"]
        self.assertEqual(block_rule["type"], "regex")
        self.assertEqual(block_rule["action"], "block")
        for keyword in ("被保険者証番号", "診察券番号", "カルテ番号"):
            self.assertIn(keyword, block_rule["pattern"])
        actions = mask_rule["entity_actions"]
        self.assertEqual(actions["jwt"], "block")
        self.assertEqual(actions["api_key_openai"], "block")
        # カスタムエンティティ名がentity_actionsに混じっていないこと（バリデーションで弾かれる）
        self.assertTrue(all(not k.startswith("jp_") for k in actions))
        self.assertEqual(mask_rule["action"], "mask")  # 既定は mask（連絡先系）

    def test_block_rule_comes_before_mask_rule(self):
        # maskが先に走ると番号がマスクされ、後段のblockが原文を見られず発火しない恐れがある。
        payload = guardrail_script.build_guardrail_payload()
        self.assertEqual([r["action"] for r in payload["rules"]], ["block", "mask"])

    def test_block_pattern_is_re2_safe_and_fires_on_synthetic_numbers(self):
        pattern = guardrail_script.JP_BLOCK_PATTERN
        for forbidden in ("(?=", "(?!", "(?<", "\\1"):
            self.assertNotIn(forbidden, pattern)
        self.assertIsNotNone(re.search(pattern, "被保険者証番号 12345678 を確認"))
        self.assertIsNotNone(re.search(pattern, "診察券番号 A-99881 を控えた"))
        self.assertIsNone(re.search(pattern, "血圧128/78、体温36.5度、脈拍70"))

    def test_mask_tags_match_moegi_tags(self):
        # ORCA側のマスクがMOEGIの型付きタグと同じ表記になる（再検査・表示が一貫する）。
        tags = {e["name"]: e["mask_with"] for e in guardrail_script.CUSTOM_ENTITIES}
        self.assertEqual(tags["jp_phone"], "[[PHONE]]")
        self.assertEqual(tags["jp_insurance_number"], "[[INSURANCE_NUMBER]]")

    def test_observe_mode_downgrades_everything_to_flag(self):
        payload = guardrail_script.build_guardrail_payload(mode="observe")
        for rule in payload["rules"]:
            self.assertEqual(rule["action"], "flag")
            if "entity_actions" in rule:
                self.assertTrue(all(v == "flag" for v in rule["entity_actions"].values()))

    def test_payload_is_not_default_and_enabled(self):
        # 本番トラフィックへの適用（キーへの紐づけ）は人の明示操作に残す。
        payload = guardrail_script.build_guardrail_payload()
        self.assertFalse(payload["is_default"])
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["name"], "moegi-cloud-boundary")

    def test_stage_is_input_only(self):
        # output の mask はstreaming未対応（ORCAドキュメントFAQ）。第二防衛線の目的は
        # 「クラウドへ出る前に止める」なので input に限定していることを固定する。
        for rule in guardrail_script.build_guardrail_payload()["rules"]:
            self.assertEqual(rule["stage"], "input")


if __name__ == "__main__":
    unittest.main()
