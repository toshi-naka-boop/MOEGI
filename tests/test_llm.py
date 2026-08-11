"""llm.py の単体テスト。

httpxの実通信はダブル（_FakeClient）で置き換え、ネットワーク・ORCA・Ollamaの
実サービスに依存せずに動く。design.md §6.1（ルーティングの第一原則）と
§6.6（timeout・circuit breaker）の挙動を検証する。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from moegi import llm
from moegi.llm import (
    CIRCUIT_BREAKER_THRESHOLD,
    LLMAdapter,
    load_model_config,
)

VALID_CONFIG = """
dev_local_only = false

[diff]
backend = "orca"
model = "candidate-a"
fallback_backend = "local"
fallback_model = "tanuki"

[requirement]
backend = "orca"
model = "sonnet"
fallback_backend = "local"
fallback_model = "tanuki"

[matching]
backend = "local"
model = "tanuki"

[draft]
backend = "local"
model = "tanuki"

[nippou]
backend = "local"
model = "gemma4"
reasoning_effort = "none"
"""


def _write_toml(dir_path: Path, content: str) -> Path:
    path = dir_path / "models.toml"
    path.write_text(content, encoding="utf-8")
    return path


def _chat_response_body(text: str, prompt_tokens: int = 10, completion_tokens: int = 5) -> dict:
    return {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


class _FakeResponse:
    def __init__(self, json_data: dict, headers: dict | None = None):
        self._json_data = json_data
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._json_data


class _FakeClient:
    """httpx.Clientの代替。URLに"fake-orca"を含むかでORCA/ローカルの呼び出しを判別する。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.post_bodies: list[dict] = []
        self.orca_queue: list = []
        self.local_response = _FakeResponse(_chat_response_body("ローカル応答"))
        self.local_raises: Exception | None = None

    def post(self, url: str, *, headers=None, json=None, timeout=None):
        self.calls.append(url)
        self.post_bodies.append(json)
        if "fake-orca" in url:
            if not self.orca_queue:
                raise AssertionError("orca_queueが空です（テストの想定より多く呼ばれた）")
            item = self.orca_queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        if self.local_raises is not None:
            raise self.local_raises
        return self.local_response

    def close(self) -> None:
        pass


class LoadModelConfigTest(unittest.TestCase):
    def test_loads_valid_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_toml(Path(tmp), VALID_CONFIG)
            cfg = load_model_config(path)
            self.assertFalse(cfg["dev_local_only"])
            self.assertEqual(cfg["stages"]["diff"].model, "candidate-a")
            self.assertEqual(cfg["stages"]["matching"].backend, "local")

    def test_accepts_orca_backend_for_matching_and_draft(self):
        # v13・ledger N-11: matching/draftはSiteDoc（項目ラベルのみ・要配慮個人情報を含まない）
        # しか扱わないためLOCAL_ONLY_STAGESから外れ、他の工程と同じくorcaを設定できる。
        cloud = VALID_CONFIG.replace('[matching]\nbackend = "local"\nmodel = "tanuki"',
                                      '[matching]\nbackend = "orca"\nmodel = "sonnet"\n'
                                      'fallback_backend = "local"\nfallback_model = "tanuki"')
        cloud = cloud.replace('[draft]\nbackend = "local"\nmodel = "tanuki"',
                               '[draft]\nbackend = "orca"\nmodel = "haiku"\n'
                               'fallback_backend = "local"\nfallback_model = "tanuki"')
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_toml(Path(tmp), cloud)
            cfg = load_model_config(path)  # 例外を上げないことそのものがテスト
            self.assertEqual(cfg["stages"]["matching"].backend, "orca")
            self.assertEqual(cfg["stages"]["draft"].backend, "orca")

    def test_rejects_orca_backend_for_nippou(self):
        # ledger N-11: nippou（日報エージェントのstructure()）は患者の実際の看護記録を
        # 扱う唯一の工程なのでLOCAL_ONLY_STAGESに残る。
        bad = VALID_CONFIG.replace('[nippou]\nbackend = "local"', '[nippou]\nbackend = "orca"')
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_toml(Path(tmp), bad)
            with self.assertRaises(llm.ConfigError):
                load_model_config(path)

    def test_reasoning_effort_is_loaded_from_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_toml(Path(tmp), VALID_CONFIG)
            cfg = load_model_config(path)
            self.assertEqual(cfg["stages"]["nippou"].reasoning_effort, "none")
            self.assertIsNone(cfg["stages"]["matching"].reasoning_effort)

    def test_cloud_fallback_model_defaults_to_empty_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_toml(Path(tmp), VALID_CONFIG)
            cfg = load_model_config(path)
            self.assertEqual(cfg["stages"]["diff"].cloud_fallback_model, "")

    def test_cloud_fallback_model_is_loaded_from_config(self):
        config = VALID_CONFIG.replace(
            '[diff]\nbackend = "orca"\nmodel = "candidate-a"\n'
            'fallback_backend = "local"\nfallback_model = "tanuki"',
            '[diff]\nbackend = "orca"\nmodel = "candidate-a"\n'
            'cloud_fallback_model = "orcarouter/auto"\n'
            'fallback_backend = "local"\nfallback_model = "tanuki"',
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_toml(Path(tmp), config)
            cfg = load_model_config(path)
            self.assertEqual(cfg["stages"]["diff"].cloud_fallback_model, "orcarouter/auto")


class AdapterRoutingTest(unittest.TestCase):
    def _make_adapter(
        self, config_text: str = VALID_CONFIG, env: dict | None = None, keychain_key: str = ""
    ):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        path = _write_toml(Path(tmpdir.name), config_text)

        base_env = {"ORCA_BASE_URL": "", "ORCA_API_KEY": "", "OLLAMA_BASE_URL": "http://fake-local"}
        base_env.update(env or {})
        patcher = mock.patch.dict(os.environ, base_env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

        # テストが実機のキーチェーンを叩かないよう常に差し替える（既定は「キー無し」）。
        keychain_patcher = mock.patch("moegi.llm._api_key_from_keychain", return_value=keychain_key)
        keychain_patcher.start()
        self.addCleanup(keychain_patcher.stop)

        client = _FakeClient()
        adapter = LLMAdapter(config_path=path, http_client=client)
        return adapter, client

    def test_matching_routes_local_when_configured_local(self):
        # VALID_CONFIGの既定（backend="local"）どおりに動くこと自体は変わらない。
        # 「常にローカル」という第一原則の強制がLOCAL_ONLY_STAGESの空集合化で外れたことは
        # 下のtest_matching_routes_to_orca_when_configured_as_orcaで検証する。
        adapter, client = self._make_adapter()
        result = adapter.complete("matching", "prompt")
        self.assertEqual(result["backend"], "local:tanuki")
        self.assertFalse(result["degraded"])
        self.assertEqual(result["fallback_level"], 0)
        self.assertTrue(all("fake-orca" not in c for c in client.calls))

    def test_matching_routes_to_orca_when_configured_as_orca(self):
        # v13・ledger N-11の核: matching/draftはもはや強制ローカルではない。
        # backend="orca"を設定すれば、diff/requirementと同じくクラウドへ実際に到達する。
        config = VALID_CONFIG.replace(
            '[matching]\nbackend = "local"\nmodel = "tanuki"',
            '[matching]\nbackend = "orca"\nmodel = "candidate-a"\n'
            'fallback_backend = "local"\nfallback_model = "tanuki"',
        )
        adapter, client = self._make_adapter(
            config_text=config, env={"ORCA_BASE_URL": "http://fake-orca", "ORCA_API_KEY": "k"}
        )
        client.orca_queue.append(_FakeResponse(_chat_response_body("クラウド応答")))
        result = adapter.complete("matching", "prompt")
        self.assertEqual(result["backend"], "orca:candidate-a")
        self.assertFalse(result["degraded"])

    def test_draft_stage_is_local_when_configured_local(self):
        adapter, client = self._make_adapter()
        result = adapter.complete("draft", "prompt")
        self.assertEqual(result["backend"], "local:tanuki")

    def test_dev_local_only_routes_diff_to_local(self):
        config = VALID_CONFIG.replace("dev_local_only = false", "dev_local_only = true")
        adapter, client = self._make_adapter(config_text=config)
        result = adapter.complete("diff", "prompt")
        self.assertEqual(result["backend"], "local:tanuki")
        self.assertTrue(all("fake-orca" not in c for c in client.calls))

    def test_cloud_success_records_resolved_model_and_fallback_level_from_headers(self):
        adapter, client = self._make_adapter(env={"ORCA_BASE_URL": "http://fake-orca", "ORCA_API_KEY": "k"})
        client.orca_queue.append(
            _FakeResponse(
                _chat_response_body("クラウド応答"),
                headers={"X-Orca-Resolved-Model": "candidate-a-v2", "X-Orca-Fallback-Level": "1"},
            )
        )
        result = adapter.complete("diff", "prompt")
        self.assertEqual(result["backend"], "orca:candidate-a")
        self.assertFalse(result["degraded"])
        self.assertEqual(result["resolved_model"], "candidate-a-v2")
        self.assertEqual(result["fallback_level"], 1)

    def test_missing_orca_credentials_falls_back_to_local_without_network_call(self):
        # 環境変数にもキーチェーンにもキーが無い場合。VALID_CONFIGはcloud_fallback_model
        # 未設定なので、主モデル失敗（credentials無し）→即ローカル（fallback_level=2）。
        adapter, client = self._make_adapter(env={"ORCA_BASE_URL": "", "ORCA_API_KEY": ""})
        result = adapter.complete("diff", "prompt")
        self.assertEqual(result["backend"], "local:tanuki")
        self.assertTrue(result["degraded"])
        self.assertEqual(result["fallback_level"], 2)
        self.assertTrue(all("fake-orca" not in c for c in client.calls))

    def test_api_key_falls_back_to_keychain_when_env_is_unset(self):
        adapter, client = self._make_adapter(
            env={"ORCA_BASE_URL": "http://fake-orca", "ORCA_API_KEY": ""}, keychain_key="from-keychain"
        )
        client.orca_queue.append(_FakeResponse(_chat_response_body("クラウド応答")))
        result = adapter.complete("diff", "prompt")
        self.assertEqual(result["backend"], "orca:candidate-a")

    def test_env_api_key_takes_precedence_over_keychain(self):
        adapter, client = self._make_adapter(
            env={"ORCA_BASE_URL": "http://fake-orca", "ORCA_API_KEY": "from-env"},
            keychain_key="from-keychain",
        )
        self.assertEqual(adapter._resolve_api_key(), "from-env")

    def test_base_url_defaults_and_does_not_double_v1(self):
        self.assertEqual(
            llm._orca_chat_completions_url("https://api.orcarouter.ai/v1"),
            "https://api.orcarouter.ai/v1/chat/completions",
        )
        self.assertEqual(
            llm._orca_chat_completions_url("https://api.orcarouter.ai"),
            "https://api.orcarouter.ai/v1/chat/completions",
        )

    def test_circuit_breaker_opens_after_threshold_failures_and_skips_orca(self):
        # VALID_CONFIGはcloud_fallback_model未設定なので、主モデル失敗→即ローカル（level=2）。
        adapter, client = self._make_adapter(env={"ORCA_BASE_URL": "http://fake-orca", "ORCA_API_KEY": "k"})
        for _ in range(CIRCUIT_BREAKER_THRESHOLD):
            client.orca_queue.append(httpx.HTTPError("boom"))

        for _ in range(CIRCUIT_BREAKER_THRESHOLD):
            result = adapter.complete("diff", "prompt")
            self.assertTrue(result["degraded"])
            self.assertEqual(result["fallback_level"], 2)

        orca_calls_before = len([c for c in client.calls if "fake-orca" in c])
        self.assertEqual(orca_calls_before, CIRCUIT_BREAKER_THRESHOLD)

        # breakerが開いた後は、ORCAへ接続を試みずに即ローカルへ（fallback_level=3・ledger R-79）
        result = adapter.complete("diff", "prompt")
        self.assertTrue(result["degraded"])
        self.assertEqual(result["fallback_level"], 3)
        orca_calls_after = len([c for c in client.calls if "fake-orca" in c])
        self.assertEqual(orca_calls_after, orca_calls_before)  # 増えていない＝ORCAへ到達していない

    def test_cloud_fallback_model_is_tried_before_local_and_does_not_count_as_breaker_failure(self):
        # ledger R-79: 主モデル失敗→cloud_fallback_model（ORCA別プロバイダ）で成功したら
        # ローカルへは落ちず、breakerの失敗カウントも増やさない（エンドポイント自体は健全なため）。
        config = VALID_CONFIG.replace(
            '[diff]\nbackend = "orca"\nmodel = "candidate-a"\n'
            'fallback_backend = "local"\nfallback_model = "tanuki"',
            '[diff]\nbackend = "orca"\nmodel = "candidate-a"\n'
            'cloud_fallback_model = "orcarouter/auto"\n'
            'fallback_backend = "local"\nfallback_model = "tanuki"',
        )
        adapter, client = self._make_adapter(
            config_text=config, env={"ORCA_BASE_URL": "http://fake-orca", "ORCA_API_KEY": "k"}
        )
        client.orca_queue.append(httpx.HTTPError("boom"))
        client.orca_queue.append(_FakeResponse(_chat_response_body("別プロバイダ応答")))
        result = adapter.complete("diff", "prompt")
        self.assertEqual(result["backend"], "orca:orcarouter/auto")
        self.assertTrue(result["degraded"])
        self.assertEqual(result["fallback_level"], 1)
        self.assertTrue(all("fake-local" not in c for c in client.calls))
        self.assertEqual(adapter._consecutive_failures, 0)  # breakerの失敗カウントは増えていない

    def test_falls_back_to_local_when_both_primary_and_cloud_fallback_fail(self):
        config = VALID_CONFIG.replace(
            '[diff]\nbackend = "orca"\nmodel = "candidate-a"\n'
            'fallback_backend = "local"\nfallback_model = "tanuki"',
            '[diff]\nbackend = "orca"\nmodel = "candidate-a"\n'
            'cloud_fallback_model = "orcarouter/auto"\n'
            'fallback_backend = "local"\nfallback_model = "tanuki"',
        )
        adapter, client = self._make_adapter(
            config_text=config, env={"ORCA_BASE_URL": "http://fake-orca", "ORCA_API_KEY": "k"}
        )
        client.orca_queue.append(httpx.HTTPError("boom"))
        client.orca_queue.append(httpx.HTTPError("boom again"))
        result = adapter.complete("diff", "prompt")
        self.assertEqual(result["backend"], "local:tanuki")
        self.assertTrue(result["degraded"])
        self.assertEqual(result["fallback_level"], 2)
        self.assertEqual(adapter._consecutive_failures, 1)

    def test_local_timeout_does_not_raise_and_marks_result_timeout(self):
        # design.md §6.6（v10）: ローカルはフォールバック先が無いため、timeoutで例外を
        # 上げてパイプラインを落としてはいけない。timeout=Trueを付けて結果を返すこと。
        adapter, client = self._make_adapter()
        client.local_raises = httpx.ReadTimeout("boom")
        result = adapter.complete("draft", "prompt")
        self.assertTrue(result["timeout"])
        self.assertEqual(result["text"], "")
        self.assertEqual(result["backend"], "local:tanuki")
        self.assertTrue(result["degraded"])

    def test_non_timeout_results_carry_timeout_false(self):
        adapter, client = self._make_adapter()
        result = adapter.complete("matching", "prompt")
        self.assertIn("timeout", result)
        self.assertFalse(result["timeout"])

    def test_stage_max_output_tokens_is_applied_to_local_call(self):
        config = VALID_CONFIG.replace(
            '[draft]\nbackend = "local"\nmodel = "tanuki"',
            '[draft]\nbackend = "local"\nmodel = "tanuki"\nmax_output_tokens = 200',
        )
        adapter, client = self._make_adapter(config_text=config)
        adapter.complete("draft", "prompt")
        self.assertEqual(client.post_bodies[-1]["max_tokens"], 200)

    def test_caller_supplied_max_tokens_overrides_stage_config(self):
        config = VALID_CONFIG.replace(
            '[draft]\nbackend = "local"\nmodel = "tanuki"',
            '[draft]\nbackend = "local"\nmodel = "tanuki"\nmax_output_tokens = 200',
        )
        adapter, client = self._make_adapter(config_text=config)
        adapter.complete("draft", "prompt", {"max_tokens": 50})
        self.assertEqual(client.post_bodies[-1]["max_tokens"], 50)

    def test_reasoning_effort_is_applied_to_local_call(self):
        # opus批評C-11・ledger N-11: nippouはreasoning_effort="none"が設定に無いと
        # gemma4:26b等のreasoningモデルで出力が空になる不具合があった。
        adapter, client = self._make_adapter()
        adapter.complete("nippou", "prompt")
        self.assertEqual(client.post_bodies[-1]["reasoning_effort"], "none")

    def test_reasoning_effort_absent_when_not_configured(self):
        adapter, client = self._make_adapter()
        adapter.complete("draft", "prompt")
        self.assertNotIn("reasoning_effort", client.post_bodies[-1])


if __name__ == "__main__":
    unittest.main()
