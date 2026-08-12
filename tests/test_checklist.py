"""checklist.py（承認済み更新案のチェックリストへの適用）のテスト。

design.md G6「同一の発話データを変更前／変更後のチェックリストで処理した2結果を並べる」の土台。
合成データのみ。
"""

from __future__ import annotations

import unittest
from unittest import mock

from moegi import checklist
from moegi.pipeline import ChecklistItem, SiteDoc


def _sitedoc(*labels: str) -> SiteDoc:
    return SiteDoc(
        id="site-1",
        kind="チェックリスト",
        title="テスト用チェックリスト",
        items=[ChecklistItem(sitedoc_id="site-1", label=label, kind="自由記述") for label in labels],
    )


def _proposal(item_label: str | None, after: str, status: str = "承認") -> dict:
    return {"sitedoc_item_id": item_label, "after": after, "status": status}


class ApplyApprovedTest(unittest.TestCase):
    def setUp(self):
        self.sitedoc = _sitedoc("バイタルサインの記録", "特記事項")

    def test_new_item_is_appended(self):
        diff = checklist.apply_approved(self.sitedoc, [_proposal(None, "目標に対する評価の記録")])
        self.assertEqual(diff.added, ["目標に対する評価の記録"])
        self.assertEqual(len(diff.after.items), 3)
        self.assertEqual(len(diff.before.items), 2)  # beforeは書き換えない

    def test_existing_item_label_is_replaced(self):
        diff = checklist.apply_approved(
            self.sitedoc, [_proposal("特記事項", "特記事項（頻回訪問の理由を含む）")]
        )
        self.assertEqual(diff.changed, [("特記事項", "特記事項（頻回訪問の理由を含む）")])
        self.assertEqual(diff.after.items[1].label, "特記事項（頻回訪問の理由を含む）")

    def test_unapproved_proposals_are_ignored(self):
        for status in ("未承認", "却下"):
            with self.subTest(status=status):
                diff = checklist.apply_approved(
                    self.sitedoc, [_proposal(None, "新項目", status=status)]
                )
                self.assertFalse(diff.has_changes)

    def test_edited_status_is_applied(self):
        diff = checklist.apply_approved(self.sitedoc, [_proposal(None, "新項目", status="編集済")])
        self.assertEqual(diff.added, ["新項目"])

    def test_undecidable_draft_is_not_applied(self):
        # design.md §6.6 の「判定不能」をそのままチェックリストへ入れない。
        diff = checklist.apply_approved(self.sitedoc, [_proposal(None, "判定不能")])
        self.assertFalse(diff.has_changes)

    def test_empty_draft_is_not_applied(self):
        diff = checklist.apply_approved(self.sitedoc, [_proposal("特記事項", "   ")])
        self.assertFalse(diff.has_changes)

    def test_unmatched_target_is_reported_not_silently_dropped(self):
        # ledger checklist-item-identity: ラベルが可変な自然キーであることに起因する取りこぼし。
        diff = checklist.apply_approved(self.sitedoc, [_proposal("存在しない項目", "新しい文言")])
        self.assertEqual(diff.unmatched, ["存在しない項目"])
        self.assertFalse(diff.has_changes)

    def test_goal_ref_is_attached_to_new_evaluation_item(self):
        # G6「評価は看護目標を参照して生成されていること」。
        diff = checklist.apply_approved(
            self.sitedoc, [_proposal(None, "目標に対する評価の記録")], goal_ref="在宅療養の継続"
        )
        self.assertEqual(diff.after.items[-1].goal_ref, "在宅療養の継続")

    def test_goal_ref_is_not_attached_to_unrelated_new_items(self):
        diff = checklist.apply_approved(
            self.sitedoc, [_proposal(None, "訪問時間の記録")], goal_ref="在宅療養の継続"
        )
        self.assertIsNone(diff.after.items[-1].goal_ref)

    def test_no_change_when_label_is_identical(self):
        diff = checklist.apply_approved(self.sitedoc, [_proposal("特記事項", "特記事項")])
        self.assertEqual(diff.changed, [])

    def test_before_sitedoc_is_not_mutated(self):
        before_labels = [i.label for i in self.sitedoc.items]
        checklist.apply_approved(
            self.sitedoc, [_proposal("特記事項", "書き換え後"), _proposal(None, "新項目")]
        )
        self.assertEqual([i.label for i in self.sitedoc.items], before_labels)


class BeforeAfterPageTest(unittest.TestCase):
    """G6 Before/After 比較画面。Privacy Gate を迂回しないことを含めて固定する。"""

    def setUp(self):
        from fastapi.testclient import TestClient

        from moegi import webapp

        self.webapp = webapp
        self.client = TestClient(webapp.app)

    def test_form_page_renders_for_a_run_without_proposals(self):
        with mock.patch.object(self.webapp.store, "list_proposals_for_review", return_value=[]):
            response = self.client.get("/runs/dummyrun/before-after")
        self.assertEqual(response.status_code, 200)
        self.assertIn("承認済みの更新案がありません", response.text)

    def test_diff_shows_added_item(self):
        with mock.patch.object(
            self.webapp.store,
            "list_proposals_for_review",
            return_value=[_proposal(None, "目標に対する評価の記録")],
        ):
            response = self.client.get("/runs/dummyrun/before-after")
        self.assertIn("目標に対する評価の記録", response.text)

    def test_structure_is_not_called_when_gate_blocks(self):
        blocked = self.webapp.privacy.run_gate(
            "被保険者証番号 12345678",
            config=self.webapp.PRIVACY_CONFIG,
            today=(2026, 8, 12),
        )
        with (
            mock.patch.object(self.webapp.store, "list_proposals_for_review", return_value=[]),
            mock.patch.object(self.webapp, "_run_privacy_gate", return_value=blocked),
            mock.patch.object(self.webapp.llm, "LLMAdapter"),
            mock.patch.object(self.webapp.nippou, "NippouAgent") as agent_cls,
        ):
            response = self.client.post(
                "/runs/dummyrun/before-after",
                data={"utterance_text": "被保険者証番号 12345678", "goal_ref": "在宅療養の継続"},
            )
        agent_cls.assert_not_called()
        self.assertIn("構造化は行っていません", response.text)

    def test_same_sanitized_text_is_used_for_both_sides(self):
        from moegi import nippou

        passed = self.webapp.privacy.run_gate(
            "山田太郎さん。血圧128/78。",
            config=self.webapp.PRIVACY_CONFIG,
            today=(2026, 8, 12),
        )
        with (
            mock.patch.object(
                self.webapp.store,
                "list_proposals_for_review",
                return_value=[_proposal(None, "目標に対する評価の記録")],
            ),
            mock.patch.object(self.webapp, "_run_privacy_gate", return_value=passed),
            mock.patch.object(self.webapp, "_save_privacy_audit"),
            mock.patch.object(self.webapp.llm, "LLMAdapter"),
            mock.patch.object(self.webapp.nippou, "NippouAgent") as agent_cls,
        ):
            agent = agent_cls.return_value
            agent.structure.return_value = nippou.NippouRecord(checklist_version="v", utterance_id="u")
            self.client.post(
                "/runs/dummyrun/before-after",
                data={"utterance_text": "山田太郎さん。血圧128/78。", "goal_ref": "在宅療養の継続"},
            )
        self.assertEqual(agent.structure.call_count, 2)
        texts = {call.args[0] for call in agent.structure.call_args_list}
        self.assertEqual(len(texts), 1)  # 同一の発話データを2回使う（G6の要件）
        self.assertIn("PATIENT_001", texts.pop())
        # before は元のチェックリスト、after は新項目を含むチェックリストで呼ばれる
        checklists = [call.args[0] for call in agent.set_checklist.call_args_list]
        self.assertEqual(len(checklists[1]) - len(checklists[0]), 1)


if __name__ == "__main__":
    unittest.main()
