"""nippou.py の単体テスト（design.md §6.5・ledger N-11）。

LLM呼び出しは_FakeAdapter（llm.LLMAdapter.complete(stage, prompt, opts)の契約を
満たすダブル）に置き換え、実際のOllamaには依存しない。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from moegi import nippou
from moegi.pipeline import ChecklistItem

_TIMEOUT = object()


class _FakeAdapter:
    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def complete(self, stage: str, prompt: str, opts: dict | None = None) -> dict:
        self.calls.append((stage, prompt))
        if not self._responses:
            raise AssertionError("応答キューが空です")
        text = self._responses.pop(0)
        if text is _TIMEOUT:
            return {
                "text": "",
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                "cost_jpy": 0.0,
                "backend": "local:test-nippou",
                "degraded": True,
                "timeout": True,
                "latency_ms": 60000.0,
                "resolved_model": "test-nippou",
                "fallback_level": 0,
            }
        return {
            "text": text,
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            "cost_jpy": 0.0,
            "backend": "local:test-nippou",
            "degraded": False,
            "timeout": False,
            "latency_ms": 1.0,
            "resolved_model": "test-nippou",
            "fallback_level": 0,
        }


class SetChecklistTest(unittest.TestCase):
    def test_structure_without_set_checklist_raises(self):
        agent = nippou.NippouAgent(_FakeAdapter([]))
        with self.assertRaises(ValueError):
            agent.structure("訪問終了、バイタル異常なし")


class TranscribeTest(unittest.TestCase):
    def test_text_input_passes_through_unchanged(self):
        agent = nippou.NippouAgent(_FakeAdapter([]))
        self.assertEqual(agent.transcribe("訪問終了、バイタル異常なし"), "訪問終了、バイタル異常なし")

    def test_nonexistent_audio_like_path_is_treated_as_text(self):
        # G7: テキスト入力でも同じ経路に入る。存在しないパスは音声ファイルとして扱わない
        # （拡張子だけでなく実在確認もしているため、誤ってASRを起動しない）。
        agent = nippou.NippouAgent(_FakeAdapter([]))
        self.assertEqual(agent.transcribe("no_such_file.wav"), "no_such_file.wav")


class StructureTest(unittest.TestCase):
    def setUp(self):
        self.items = [
            ChecklistItem(sitedoc_id="s", label="バイタルサインの記録", kind="自由記述"),
            ChecklistItem(sitedoc_id="s", label="特記事項", kind="自由記述"),
        ]

    def test_marks_items_filled_or_not_from_llm_response(self):
        adapter = _FakeAdapter(
            [
                json.dumps(
                    {
                        "items": [
                            {"item_id": "1", "filled": True, "value": "血圧130/80、脈拍72"},
                            {"item_id": "2", "filled": False, "value": ""},
                        ]
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = nippou.NippouAgent(adapter)
        agent.set_checklist(self.items)
        record = agent.structure("訪問終了。血圧130/80、脈拍72、特記事項なし。")
        self.assertEqual(len(record.items), 2)
        self.assertTrue(record.items[0].filled)
        self.assertIn("130/80", record.items[0].value)
        self.assertFalse(record.items[1].filled)
        self.assertIsNone(record.evaluation)  # goal_refを持つ項目が無い

    def test_missing_item_in_response_becomes_unfilled(self):
        # 一括判定の判定漏れは「未記入」として検知できる形にする（pipeline.pyの一括モードと同じ規律）。
        adapter = _FakeAdapter(
            [json.dumps({"items": [{"item_id": "1", "filled": True, "value": "x"}]}, ensure_ascii=False)]
        )
        agent = nippou.NippouAgent(adapter)
        agent.set_checklist(self.items)
        record = agent.structure("発話")
        self.assertFalse(record.items[1].filled)

    def test_timeout_marks_all_items_unfilled_and_does_not_raise(self):
        # design.md §6.6の完走優先方針をnippouにも適用。
        adapter = _FakeAdapter([_TIMEOUT])
        agent = nippou.NippouAgent(adapter)
        agent.set_checklist(self.items)
        record = agent.structure("発話")
        self.assertEqual([i.filled for i in record.items], [False, False])

    def test_malformed_response_marks_all_items_unfilled(self):
        adapter = _FakeAdapter(["これはJSONではない出力です"])
        agent = nippou.NippouAgent(adapter)
        agent.set_checklist(self.items)
        record = agent.structure("発話")
        self.assertEqual([i.filled for i in record.items], [False, False])


class EvaluationTest(unittest.TestCase):
    """design.md §6.5「評価の記録」の実体要件（批評Y-2）。"""

    def setUp(self):
        self.items_with_goal = [
            ChecklistItem(sitedoc_id="s", label="バイタルサインの記録", kind="自由記述"),
            ChecklistItem(
                sitedoc_id="s", label="利用者の看護目標", kind="自由記述", goal_ref="在宅緩和ケアの継続"
            ),
        ]

    def test_evaluation_includes_goal_ref_achievement_effect_as_separate_fields(self):
        adapter = _FakeAdapter(
            [
                json.dumps(
                    {
                        "items": [
                            {"item_id": "1", "filled": True, "value": "異常なし"},
                            {"item_id": "2", "filled": True, "value": "在宅緩和ケア継続中"},
                        ],
                        "evaluation": {"achievement": "順調に経過", "effect": "疼痛が軽減した"},
                    },
                    ensure_ascii=False,
                )
            ]
        )
        agent = nippou.NippouAgent(adapter)
        agent.set_checklist(self.items_with_goal)
        record = agent.structure("発話")
        self.assertIsNotNone(record.evaluation)
        self.assertEqual(record.evaluation.goal_ref, "在宅緩和ケアの継続")
        self.assertEqual(record.evaluation.achievement, "順調に経過")
        self.assertEqual(record.evaluation.effect, "疼痛が軽減した")
        self.assertTrue(record.evaluation.filled)

    def test_goal_ref_prompt_is_sent_when_goal_bearing_item_present(self):
        adapter = _FakeAdapter(
            [json.dumps({"items": [{"item_id": "1", "filled": False, "value": ""},
                                    {"item_id": "2", "filled": False, "value": ""}]})]
        )
        agent = nippou.NippouAgent(adapter)
        agent.set_checklist(self.items_with_goal)
        agent.structure("発話")
        _, prompt = adapter.calls[0]
        self.assertIn("在宅緩和ケアの継続", prompt)

    def test_missing_evaluation_in_response_still_forces_mitassei_without_goal_ref(self):
        # goal_refが空のままachievementが生成された場合は「未達成」とみなす（コード側で強制）。
        # ここではLLMがevaluationを一切返さなかったケースを検証。
        adapter = _FakeAdapter(
            [json.dumps({"items": [{"item_id": "1", "filled": False, "value": ""},
                                    {"item_id": "2", "filled": False, "value": ""}]})]
        )
        agent = nippou.NippouAgent(adapter)
        agent.set_checklist(self.items_with_goal)
        record = agent.structure("発話")
        self.assertEqual(record.evaluation.achievement, "未達成")
        self.assertFalse(record.evaluation.filled)

    def test_timeout_still_forces_mitassei_for_evaluation(self):
        adapter = _FakeAdapter([_TIMEOUT])
        agent = nippou.NippouAgent(adapter)
        agent.set_checklist(self.items_with_goal)
        record = agent.structure("発話")
        self.assertEqual(record.evaluation.achievement, "未達成")
        self.assertEqual(record.evaluation.goal_ref, "在宅緩和ケアの継続")


if __name__ == "__main__":
    unittest.main()
