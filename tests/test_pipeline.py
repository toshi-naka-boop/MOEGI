"""pipeline.py の単体テスト。

LLM呼び出しは_FakeAdapter（design.md §6.1の complete(stage, prompt, opts) 契約を
満たすダブル）に置き換え、実際のOllama/ORCAには依存しない。
design.md §6.4「Impact.evidenceは必須。空なら提案を作らない」の検証が主眼。
"""

from __future__ import annotations

import json
import unittest

from moegi import pipeline
from moegi.ingest import Section


def _section(path: str, text: str) -> Section:
    return Section(notice_id="test-notice", path=path, text=text, offset=0)


_TIMEOUT = object()  # 応答キューにこのセンチネルを積むと、そのcompleteはtimeout結果を返す


class _FakeAdapter:
    """complete(stage, prompt, opts) -> dict の契約を満たすテストダブル。"""

    def __init__(self, responses: dict[str, list[str]]):
        self._responses = {stage: list(texts) for stage, texts in responses.items()}
        self.calls: list[tuple[str, str]] = []

    def complete(self, stage: str, prompt: str, opts: dict | None = None) -> dict:
        self.calls.append((stage, prompt))
        queue = self._responses.get(stage)
        if not queue:
            raise AssertionError(f"stage={stage!r} の応答キューが空です（テストの想定より多く呼ばれた）")
        text = queue.pop(0)
        if text is _TIMEOUT:
            # design.md §6.6（v10）: ローカルtimeoutはtext=""・timeout=Trueで返る（例外を上げない）。
            return {
                "text": "",
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                "cost_jpy": 0.0,
                "backend": f"local:test-{stage}",
                "degraded": True,
                "timeout": True,
                "latency_ms": 60000.0,
                "resolved_model": f"test-{stage}",
                "fallback_level": 0,
            }
        return {
            "text": text,
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            "cost_jpy": 0.0,
            "backend": f"local:test-{stage}",
            "degraded": False,
            "timeout": False,
            "latency_ms": 1.0,
            "resolved_model": f"test-{stage}",
            "fallback_level": 0,
        }


class BuildChangesTest(unittest.TestCase):
    def test_new_provision_without_old_section_is_kind_tsuika(self):
        new_sections = {"c": _section("第4の4", "評価の記録の本文")}
        old_sections = {"c": None}
        changes = pipeline.build_changes(old_sections, new_sections)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].kind, "追加")
        self.assertIsNone(changes[0].old_section)
        self.assertEqual(changes[0].id, "c")

    def test_existing_provision_is_kind_kaisei(self):
        new_sections = {"d": _section("第4の2", "新版本文")}
        old_sections = {"d": _section("第4の2", "旧版本文")}
        changes = pipeline.build_changes(old_sections, new_sections)
        self.assertEqual(changes[0].kind, "改正")
        self.assertIsNotNone(changes[0].old_section)

    def test_skips_when_new_section_missing(self):
        changes = pipeline.build_changes({"x": None}, {"x": None})
        self.assertEqual(changes, [])


class MatchRequirementsTest(unittest.TestCase):
    def setUp(self):
        self.sitedoc = pipeline.SiteDoc(
            id="s",
            kind="チェックリスト",
            title="t",
            items=[pipeline.ChecklistItem(sitedoc_id="s", label="バイタルサインの記録", kind="自由記述")],
        )
        self.change = pipeline.Change(
            id="c",
            old_section=None,
            new_section=_section(
                "第4の4",
                "指定訪問看護の提供に当たっては、その目標達成の程度及びその効果等について評価を行うこと。",
            ),
            kind="追加",
            summary="評価の記録が新設された",
        )
        self.requirement = pipeline.Requirement(change_id="c", text="目標達成の程度及びその効果を記録する")

    def _match(self, call_responses: list[dict]) -> list[pipeline.Impact]:
        """call_responses: match_requirementsが行う呼び出し順に並べた応答オブジェクトの列。

        sitedocの項目が1件なので、呼び出し順は必ず
        [その項目の判定, (影響なしだった場合のみ)新規項目の要否判定] になる。
        """
        adapter = _FakeAdapter({"matching": [json.dumps(r, ensure_ascii=False) for r in call_responses]})
        return pipeline.match_requirements(
            [self.requirement], {"c": self.change}, self.sitedoc, adapter, run_id="r"
        )

    def test_creates_impact_for_new_item_when_evidence_is_verifiable(self):
        impacts = self._match(
            [
                {"verdict": "影響なし", "rationale": "既存項目とは無関係", "confidence": 0.8, "evidence_quote": ""},
                {
                    "needed": True,
                    "rationale": "評価を記録する項目が無い",
                    "confidence": 0.9,
                    "evidence_quote": "目標達成の程度及びその効果等について評価を行う",
                },
            ]
        )
        self.assertEqual(len(impacts), 1)
        self.assertIsNone(impacts[0].sitedoc_item_id)
        self.assertEqual(impacts[0].verdict, "要更新")
        self.assertEqual(len(impacts[0].evidence), 1)
        self.assertGreaterEqual(impacts[0].evidence[0].offset, 0)

    def _existing_item_impacts(self, impacts):
        # self.change は完全新設なので、新規項目の提案は常に1件付く（ledger N-7）。
        # 既存項目に対する判定だけを見たいテストではこちらを使う。
        return [i for i in impacts if i.sitedoc_item_id is not None]

    def test_drops_impact_when_evidence_quote_not_found_in_notice_text(self):
        impacts = self._match(
            [
                {"verdict": "影響なし", "rationale": "既存項目とは無関係", "confidence": 0.8, "evidence_quote": ""},
                {
                    "needed": True,
                    "rationale": "でっちあげ",
                    "confidence": 0.9,
                    "evidence_quote": "通達本文に存在しない引用文",
                },
            ]
        )
        self.assertEqual(self._existing_item_impacts(impacts), [])
        # 幻覚引用は根拠に採らず、条文冒頭へ差し替わること
        self.assertNotIn("通達本文に存在しない引用文", impacts[0].evidence[0].quote)

    def test_drops_when_verdict_is_no_impact(self):
        impacts = self._match(
            [
                {"verdict": "影響なし", "rationale": "無関係", "confidence": 0.1, "evidence_quote": ""},
                {"needed": False, "rationale": "新規項目も不要", "confidence": 0.6, "evidence_quote": ""},
            ]
        )
        self.assertEqual(self._existing_item_impacts(impacts), [])

    def test_ignores_malformed_response(self):
        adapter = _FakeAdapter({"matching": ["これはJSONではない出力です", '{"needed": false}']})
        impacts = pipeline.match_requirements(
            [self.requirement], {"c": self.change}, self.sitedoc, adapter, run_id="r"
        )
        self.assertEqual(self._existing_item_impacts(impacts), [])

    def test_recovers_verdict_when_local_model_closes_json_early(self):
        # 実機（G9計測）で確認した崩れ方: rationaleの直後で "}" が出て、confidence・
        # evidence_quoteがオブジェクトの外側に漏れる。パース側で救わないと、
        # 正しい「要更新」判定ごと evidence 不成立として捨てられていた。
        malformed = (
            '{"verdict": "要更新", "rationale": "標準時間内の訪問が厳格化されたため。"}, '
            '"confidence": 0.8, "evidence_quote": "'
            "目標達成の程度及びその効果等について評価を行う"
            '"'
        )
        adapter = _FakeAdapter(
            {"matching": [malformed, '{"needed": false, "rationale": "x", "confidence": 0.1}']}
        )
        impacts = pipeline.match_requirements(
            [self.requirement], {"c": self.change}, self.sitedoc, adapter, run_id="r"
        )
        existing = self._existing_item_impacts(impacts)
        self.assertEqual(len(existing), 1)
        self.assertEqual(existing[0].verdict, "要更新")
        self.assertEqual(existing[0].confidence, 0.8)

    def test_matches_existing_item_and_still_adds_new_item_for_new_provision(self):
        # 完全新設では、既存項目が「要更新」になっても新規項目の提案が別に立つ（ledger N-7）
        impacts = self._match(
            [
                {
                    "verdict": "要更新",
                    "rationale": "評価と合わせて記録が必要",
                    "confidence": 0.7,
                    "evidence_quote": "目標達成の程度及びその効果等について評価を行う",
                },
                {"needed": False, "rationale": "不要", "confidence": 0.2, "evidence_quote": ""},
            ]
        )
        self.assertEqual([i.sitedoc_item_id for i in impacts], ["バイタルサインの記録", None])


class MatchRequirementsBatchedTest(unittest.TestCase):
    """design.md §7「投入単位と突合のバッチ化」（v11）・承認条件1。"""

    def setUp(self):
        self.sitedoc = pipeline.SiteDoc(
            id="s",
            kind="チェックリスト",
            title="t",
            items=[
                pipeline.ChecklistItem(sitedoc_id="s", label="バイタルサインの記録", kind="自由記述"),
                pipeline.ChecklistItem(sitedoc_id="s", label="利用者の看護目標", kind="自由記述"),
            ],
        )
        self.change = pipeline.Change(
            id="c",
            old_section=None,
            new_section=_section(
                "第4の4",
                "指定訪問看護の提供に当たっては、その目標達成の程度及びその効果等について評価を行うこと。",
            ),
            kind="追加",
            summary="評価の記録が新設された",
        )
        self.requirement = pipeline.Requirement(change_id="c", text="目標達成の程度及びその効果を記録する")

    def _match(self, response) -> list[pipeline.Impact]:
        text = response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)
        adapter = _FakeAdapter({"matching": [text]})
        self.adapter = adapter
        return pipeline._match_requirements_batched(
            [self.requirement], {"c": self.change}, self.sitedoc, adapter, run_id="r"
        )

    def _existing_item_impacts(self, impacts):
        # self.change は完全新設なので新規項目の提案が常に1件付く（ledger N-7）
        return [i for i in impacts if i.sitedoc_item_id is not None]

    def test_single_call_covers_all_items(self):
        impacts = self._match(
            {
                "judgements": [
                    {"item_id": "1", "verdict": "影響なし", "rationale": "無関係", "confidence": 0.8, "evidence_quote": ""},
                    {
                        "item_id": "2",
                        "verdict": "要更新",
                        "rationale": "目標に対する評価が要る",
                        "confidence": 0.7,
                        "evidence_quote": "目標達成の程度及びその効果等について評価を行う",
                    },
                ],
                "new_item": {"needed": False, "rationale": "x", "confidence": 0.1, "evidence_quote": ""},
            }
        )
        self.assertEqual(len(self.adapter.calls), 1)  # 2項目でも呼び出しは1回
        existing = self._existing_item_impacts(impacts)
        self.assertEqual(len(existing), 1)
        self.assertEqual(existing[0].sitedoc_item_id, "利用者の看護目標")
        self.assertEqual(existing[0].verdict, "要更新")

    def test_missing_item_id_becomes_undecidable(self):
        # 承認条件1: 一括プロンプトで項目2の判定が返らなかったら「判定不能」として検知できること
        impacts = self._match(
            {
                "judgements": [
                    {"item_id": "1", "verdict": "影響なし", "rationale": "無関係", "confidence": 0.8, "evidence_quote": ""}
                ],
                "new_item": {"needed": False, "rationale": "x", "confidence": 0.1, "evidence_quote": ""},
            }
        )
        existing = self._existing_item_impacts(impacts)
        self.assertEqual(len(existing), 1)
        self.assertEqual(existing[0].verdict, "判定不能")
        self.assertEqual(existing[0].sitedoc_item_id, "利用者の看護目標")

    def test_malformed_response_marks_every_item_undecidable(self):
        impacts = self._match("これはJSONではない出力です")
        self.assertEqual([i.verdict for i in self._existing_item_impacts(impacts)], ["判定不能", "判定不能"])

    def test_timeout_marks_every_item_undecidable_but_keeps_new_provision_proposal(self):
        adapter = _FakeAdapter({"matching": [_TIMEOUT]})
        impacts = pipeline._match_requirements_batched(
            [self.requirement], {"c": self.change}, self.sitedoc, adapter, run_id="r"
        )
        self.assertEqual([i.verdict for i in self._existing_item_impacts(impacts)], ["判定不能", "判定不能"])
        # 完全新設の提案はLLM応答に依存しないので、timeoutでも残ること（回線断デモの落ち）
        new_items = [i for i in impacts if i.sitedoc_item_id is None]
        self.assertEqual(len(new_items), 1)
        self.assertEqual(new_items[0].verdict, "要更新")

    def test_unverifiable_quote_is_dropped(self):
        impacts = self._match(
            {
                "judgements": [
                    {"item_id": "1", "verdict": "影響なし", "rationale": "x", "confidence": 0.8, "evidence_quote": ""},
                    {
                        "item_id": "2",
                        "verdict": "要更新",
                        "rationale": "でっちあげ",
                        "confidence": 0.9,
                        "evidence_quote": "通達本文に存在しない引用文",
                    },
                ],
                "new_item": {"needed": False, "rationale": "x", "confidence": 0.1, "evidence_quote": ""},
            }
        )
        self.assertEqual(self._existing_item_impacts(impacts), [])

    def test_creates_new_item_impact_when_no_existing_item_matches(self):
        impacts = self._match(
            {
                "judgements": [
                    {"item_id": "1", "verdict": "影響なし", "rationale": "x", "confidence": 0.8, "evidence_quote": ""},
                    {"item_id": "2", "verdict": "影響なし", "rationale": "x", "confidence": 0.8, "evidence_quote": ""},
                ],
                "new_item": {
                    "needed": True,
                    "rationale": "評価を記録する項目が無い",
                    "confidence": 0.9,
                    "evidence_quote": "目標達成の程度及びその効果等について評価を行う",
                },
            }
        )
        self.assertEqual(len(impacts), 1)
        self.assertIsNone(impacts[0].sitedoc_item_id)
        self.assertEqual(impacts[0].verdict, "要更新")


class DraftProposalsBatchedTest(unittest.TestCase):
    def setUp(self):
        self.sitedoc = pipeline.SiteDoc(
            id="s",
            kind="チェックリスト",
            title="t",
            items=[pipeline.ChecklistItem(sitedoc_id="s", label="利用者の看護目標", kind="自由記述")],
        )
        self.impacts = [
            pipeline.Impact(
                id="impact-c-利用者の看護目標",
                change_id="c",
                sitedoc_item_id="利用者の看護目標",
                verdict="要更新",
                rationale="評価が要る",
                confidence=0.7,
                evidence=[pipeline.Evidence(notice_section_path="第4の4", quote="評価を行う", offset=0)],
            ),
            pipeline.Impact(
                id="impact-c-new",
                change_id="c",
                sitedoc_item_id=None,
                verdict="要更新",
                rationale="新規項目が要る",
                confidence=0.9,
                evidence=[pipeline.Evidence(notice_section_path="第4の4", quote="評価を行う", offset=0)],
            ),
        ]

    def test_single_call_for_all_targets(self):
        adapter = _FakeAdapter(
            {
                "draft": [
                    json.dumps(
                        {
                            "drafts": [
                                {"item_id": "1", "after": "看護目標と、その達成度の評価を記録する。"},
                                {"item_id": "2", "after": "評価欄を新設する。"},
                            ]
                        },
                        ensure_ascii=False,
                    )
                ]
            }
        )
        proposals = pipeline._draft_proposals_batched(self.impacts, self.sitedoc, adapter, run_id="r")
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(len(proposals), 2)
        self.assertEqual(proposals[0].before, "利用者の看護目標")
        self.assertEqual(proposals[1].before, "")
        self.assertIn("評価", proposals[1].after)

    def test_missing_draft_becomes_undecidable(self):
        adapter = _FakeAdapter(
            {"draft": [json.dumps({"drafts": [{"item_id": "1", "after": "更新後の文言"}]}, ensure_ascii=False)]}
        )
        proposals = pipeline._draft_proposals_batched(self.impacts, self.sitedoc, adapter, run_id="r")
        self.assertEqual(proposals[1].after, "判定不能")

    def test_timeout_marks_all_undecidable(self):
        adapter = _FakeAdapter({"draft": [_TIMEOUT]})
        proposals = pipeline._draft_proposals_batched(self.impacts, self.sitedoc, adapter, run_id="r")
        self.assertEqual([p.after for p in proposals], ["判定不能", "判定不能"])

    def test_skips_impacts_without_evidence(self):
        impact = pipeline.Impact(
            id="impact-c-x", change_id="c", sitedoc_item_id=None, verdict="要更新", rationale="x",
            confidence=0.9, evidence=[],
        )
        adapter = _FakeAdapter({})
        proposals = pipeline._draft_proposals_batched([impact], self.sitedoc, adapter, run_id="r")
        self.assertEqual(proposals, [])


class ExtractJsonPayloadTest(unittest.TestCase):
    def test_reads_nested_array_payload(self):
        payload = pipeline._extract_json_payload(
            '前置き {"judgements": [{"item_id": "1", "verdict": "影響なし"}]} 後置き'
        )
        self.assertEqual(len(payload["judgements"]), 1)

    def test_ignores_braces_inside_string_literals(self):
        payload = pipeline._extract_json_payload('{"rationale": "記号 } を含む理由", "confidence": 0.5}')
        self.assertEqual(payload["confidence"], 0.5)

    def test_returns_none_when_no_json(self):
        self.assertIsNone(pipeline._extract_json_payload("JSONではない出力"))


class RecoverTrailingFieldsTest(unittest.TestCase):
    """G9計測で実測した崩れ方（rationale直後の早すぎる"}"）に対する回復ロジック。"""

    def test_recovers_fields_that_leaked_outside_the_object(self):
        text = '{"verdict": "要更新", "rationale": "理由"}, "confidence": 0.8, "evidence_quote": "根拠"'
        entry = pipeline._extract_json_object(text, pipeline._MATCH_ITEM_EXPECTED_KEYS)
        self.assertEqual(entry["confidence"], 0.8)
        self.assertEqual(entry["evidence_quote"], "根拠")

    def test_well_formed_object_is_unaffected(self):
        text = '{"verdict": "要更新", "rationale": "理由", "confidence": 0.8, "evidence_quote": "根拠"}'
        entry = pipeline._extract_json_object(text, pipeline._MATCH_ITEM_EXPECTED_KEYS)
        self.assertEqual(entry["evidence_quote"], "根拠")

    def test_without_expected_keys_does_not_recover(self):
        # expected_keys を渡さない呼び出し（一括モードの応答本体の抽出）は対象外
        text = '{"verdict": "要更新", "rationale": "理由"}, "confidence": 0.8'
        entry = pipeline._extract_json_object(text)
        self.assertNotIn("confidence", entry)


class ForcedNewItemForNewProvisionTest(unittest.TestCase):
    """ledger N-7（v11）: 完全新設の条文は新規項目の提案を必ず作る。"""

    def setUp(self):
        self.sitedoc = pipeline.SiteDoc(
            id="s",
            kind="チェックリスト",
            title="t",
            items=[pipeline.ChecklistItem(sitedoc_id="s", label="バイタルサインの記録", kind="自由記述")],
        )
        self.new_provision = pipeline.Change(
            id="c",
            old_section=None,  # 旧版に対応条文が無い＝完全新設
            new_section=_section(
                "第4の4",
                "指定訪問看護の提供に当たっては、その目標達成の程度及びその効果等について評価を行うこと。",
            ),
            kind="追加",
            summary="評価の記録が新設された",
        )
        self.requirement = pipeline.Requirement(change_id="c", text="目標達成の程度及びその効果を記録する")

    def _new_item_impacts(self, impacts):
        return [i for i in impacts if i.sitedoc_item_id is None]

    def test_single_mode_creates_new_item_even_when_existing_item_matched(self):
        # 既存項目が「要更新」になっても、完全新設なら新規項目の判定まで進むこと
        adapter = _FakeAdapter(
            {
                "matching": [
                    json.dumps(
                        {
                            "verdict": "要更新",
                            "rationale": "過剰検出",
                            "confidence": 0.7,
                            "evidence_quote": "目標達成の程度及びその効果等について評価を行う",
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps({"needed": False, "rationale": "不要", "confidence": 0.2, "evidence_quote": ""}),
                ]
            }
        )
        impacts = pipeline.match_requirements(
            [self.requirement], {"c": self.new_provision}, self.sitedoc, adapter, run_id="r"
        )
        new_items = self._new_item_impacts(impacts)
        self.assertEqual(len(new_items), 1)
        self.assertEqual(new_items[0].verdict, "要更新")
        self.assertTrue(new_items[0].evidence)  # 根拠が無いと更新案が作られない

    def test_single_mode_falls_back_to_notice_head_when_llm_gives_no_usable_quote(self):
        adapter = _FakeAdapter(
            {
                "matching": [
                    json.dumps({"verdict": "影響なし", "rationale": "x", "confidence": 0.1, "evidence_quote": ""}),
                    "これはJSONではない出力です",
                ]
            }
        )
        impacts = pipeline.match_requirements(
            [self.requirement], {"c": self.new_provision}, self.sitedoc, adapter, run_id="r"
        )
        new_items = self._new_item_impacts(impacts)
        self.assertEqual(len(new_items), 1)
        # 根拠は条文冒頭。通達原文中に実在する文字列であること
        quote = new_items[0].evidence[0].quote
        self.assertIn(quote, self.new_provision.new_section.text)
        self.assertEqual(new_items[0].rationale, self.requirement.text)

    def test_batch_mode_creates_new_item_even_when_llm_says_not_needed(self):
        adapter = _FakeAdapter(
            {
                "matching": [
                    json.dumps(
                        {
                            "judgements": [
                                {"item_id": "1", "verdict": "影響なし", "rationale": "x", "confidence": 0.8, "evidence_quote": ""}
                            ],
                            "new_item": {"needed": False, "rationale": "不要", "confidence": 0.2, "evidence_quote": ""},
                        },
                        ensure_ascii=False,
                    )
                ]
            }
        )
        impacts = pipeline._match_requirements_batched(
            [self.requirement], {"c": self.new_provision}, self.sitedoc, adapter, run_id="r"
        )
        new_items = self._new_item_impacts(impacts)
        self.assertEqual(len(new_items), 1)
        self.assertEqual(new_items[0].verdict, "要更新")

    def test_revised_provision_still_skips_new_item_when_existing_matched(self):
        # 改正（旧版に対応条文がある）では従来どおり、既存項目が拾えたら新規項目は問わない
        revised = pipeline.Change(
            id="d",
            old_section=_section("第4の2", "旧版本文"),
            new_section=_section("第4の2", "評価を行うこと。"),
            kind="改正",
        )
        adapter = _FakeAdapter(
            {
                "matching": [
                    json.dumps(
                        {"verdict": "要更新", "rationale": "y", "confidence": 0.7, "evidence_quote": "評価を行う"},
                        ensure_ascii=False,
                    )
                ]
            }
        )
        impacts = pipeline.match_requirements(
            [pipeline.Requirement(change_id="d", text="評価する")],
            {"d": revised},
            self.sitedoc,
            adapter,
            run_id="r",
        )
        self.assertEqual(len(adapter.calls), 1)  # 新規項目の追加呼び出しをしていない
        self.assertEqual(self._new_item_impacts(impacts), [])


class FindQuoteOffsetTest(unittest.TestCase):
    def test_exact_match(self):
        self.assertEqual(pipeline._find_quote_offset("評価を行う", "前置き評価を行う後置き"), 3)

    def test_tolerates_pdf_line_wrap_inside_quote(self):
        # PDF抽出テキストは行送りの都合で文中に改行が入ることがある。
        # 引用文には改行が無くても、原文中の対応箇所を見つけられること。
        source = "指定訪問看護の提供に当たっては、目標達成の程度及びその効果等について評価を\n行い、その内容を記録すること。"
        quote = "目標達成の程度及びその効果等について評価を行い、その内容を記録すること。"
        offset = pipeline._find_quote_offset(quote, source)
        self.assertNotEqual(offset, -1)
        self.assertEqual(source[offset], "目")

    def test_returns_minus_one_when_not_found(self):
        self.assertEqual(pipeline._find_quote_offset("存在しない文", "全く関係ない原文"), -1)


class DraftProposalsTest(unittest.TestCase):
    def test_creates_proposal_for_new_item(self):
        sitedoc = pipeline.SiteDoc(id="s", kind="チェックリスト", title="t", items=[])
        impact = pipeline.Impact(
            id="impact-c-new",
            change_id="c",
            sitedoc_item_id=None,
            verdict="要更新",
            rationale="評価が必要",
            confidence=0.9,
            evidence=[pipeline.Evidence(notice_section_path="第4の4", quote="評価を行う", offset=0)],
        )
        adapter = _FakeAdapter({"draft": ["目標達成の程度及びその効果を記録する評価欄を追加する。"]})
        proposals = pipeline.draft_proposals([impact], sitedoc, adapter, run_id="r")
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].before, "")
        self.assertIn("評価", proposals[0].after)
        self.assertEqual(proposals[0].status, "未承認")

    def test_skips_impact_without_evidence(self):
        sitedoc = pipeline.SiteDoc(id="s", kind="チェックリスト", title="t", items=[])
        impact = pipeline.Impact(
            id="impact-c-new",
            change_id="c",
            sitedoc_item_id=None,
            verdict="要更新",
            rationale="x",
            confidence=0.9,
            evidence=[],
        )
        adapter = _FakeAdapter({})
        proposals = pipeline.draft_proposals([impact], sitedoc, adapter, run_id="r")
        self.assertEqual(proposals, [])

    def test_skips_impact_with_no_impact_verdict(self):
        sitedoc = pipeline.SiteDoc(id="s", kind="チェックリスト", title="t", items=[])
        impact = pipeline.Impact(
            id="impact-c-x",
            change_id="c",
            sitedoc_item_id=None,
            verdict="影響なし",
            rationale="x",
            confidence=0.1,
            evidence=[],
        )
        adapter = _FakeAdapter({})
        proposals = pipeline.draft_proposals([impact], sitedoc, adapter, run_id="r")
        self.assertEqual(proposals, [])


class TimeoutHandlingTest(unittest.TestCase):
    """design.md §6.6（v10）: ローカルtimeoutは例外を上げず「判定不能」として続行する。"""

    def setUp(self):
        self.sitedoc = pipeline.SiteDoc(
            id="s",
            kind="チェックリスト",
            title="t",
            items=[pipeline.ChecklistItem(sitedoc_id="s", label="バイタルサインの記録", kind="自由記述")],
        )
        self.change = pipeline.Change(
            id="c",
            old_section=None,
            new_section=_section("第4の4", "評価を行うこと。"),
            kind="追加",
        )

    def test_extract_change_summaries_marks_hantei_funou_on_timeout(self):
        adapter = _FakeAdapter({"diff": [_TIMEOUT]})
        changes = [self.change]
        pipeline.extract_change_summaries(changes, adapter, run_id="r")
        self.assertEqual(changes[0].summary, "判定不能")

    def test_extract_requirements_marks_hantei_funou_on_timeout(self):
        adapter = _FakeAdapter({"requirement": [_TIMEOUT]})
        self.change.summary = "評価の記録が新設された"
        requirements = pipeline.extract_requirements([self.change], adapter, run_id="r")
        self.assertEqual(requirements[0].text, "判定不能")

    def test_match_requirements_creates_hantei_funou_impact_and_continues(self):
        # 1件目の項目判定でtimeout → 判定不能のImpactを作り、新規項目判定へ処理を続行する。
        requirement = pipeline.Requirement(change_id="c", text="評価を記録する")
        adapter = _FakeAdapter(
            {
                "matching": [
                    _TIMEOUT,
                    json.dumps({"needed": False, "rationale": "x", "confidence": 0.5, "evidence_quote": ""}),
                ]
            }
        )
        impacts = pipeline.match_requirements(
            [requirement], {"c": self.change}, self.sitedoc, adapter, run_id="r"
        )
        undecidable = [i for i in impacts if i.verdict == "判定不能"]
        self.assertEqual(len(undecidable), 1)
        self.assertEqual(undecidable[0].sitedoc_item_id, "バイタルサインの記録")
        self.assertEqual(undecidable[0].evidence, [])
        # timeoutの後も次の呼び出し（新規項目判定）まで到達している
        self.assertEqual(len(adapter.calls), 2)

    def test_draft_proposals_marks_hantei_funou_on_timeout_and_keeps_proposal(self):
        # design.md §6.6: draftがtimeoutしても例外を上げず、更新案として「判定不能」を残す
        # （承認UI・JSON出力に残る＝黙って捨てない）。
        sitedoc = pipeline.SiteDoc(id="s", kind="チェックリスト", title="t", items=[])
        impact = pipeline.Impact(
            id="impact-c-new",
            change_id="c",
            sitedoc_item_id=None,
            verdict="要更新",
            rationale="評価が必要",
            confidence=0.9,
            evidence=[pipeline.Evidence(notice_section_path="第4の4", quote="評価を行う", offset=0)],
        )
        adapter = _FakeAdapter({"draft": [_TIMEOUT]})
        proposals = pipeline.draft_proposals([impact], sitedoc, adapter, run_id="r")
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].after, "判定不能")


class RunLabelFilterTest(unittest.TestCase):
    """design.md §2「投入単位」（v10）: labelsを指定すると1項目だけを処理する。"""

    def test_run_with_labels_processes_only_specified_label(self):
        old_sections = {"c": None, "d": _section("第4の2", "旧版本文")}
        new_sections = {
            "c": _section("第4の4", "評価を行うこと。"),
            "d": _section("第4の2", "新版本文"),
        }
        sitedoc = pipeline.SiteDoc(id="s", kind="チェックリスト", title="t", items=[])
        adapter = _FakeAdapter(
            {
                "diff": ["cの要約"],
                "requirement": ["cの要件"],
                "matching": [json.dumps({"needed": False, "rationale": "x", "confidence": 0.1, "evidence_quote": ""})],
                # cは完全新設なので新規項目の提案が立ち、更新案生成まで到達する（ledger N-7）
                "draft": [json.dumps({"drafts": [{"item_id": "1", "after": "評価欄を新設する。"}]}, ensure_ascii=False)],
            }
        )
        result = pipeline.run(
            old_sections=old_sections,
            new_sections=new_sections,
            sitedoc=sitedoc,
            adapter=adapter,
            run_id="r",
            labels={"c"},
        )
        self.assertEqual(len(result["changes"]), 1)
        self.assertEqual(result["changes"][0].id, "c")


class LoadSitedocTest(unittest.TestCase):
    def test_loads_sample_sitedoc(self):
        sitedoc = pipeline.load_sitedoc()
        self.assertEqual(sitedoc.kind, "チェックリスト")
        self.assertGreater(len(sitedoc.items), 0)
        labels = [item.label for item in sitedoc.items]
        # デモアンカー（design.md §2 c「評価の記録」）の前提: 既存項目に無いこと
        self.assertNotIn("評価の記録", labels)


if __name__ == "__main__":
    unittest.main()
