"""ingest.py の単体テスト（標準ライブラリの unittest のみを使用。追加依存なし）。

parse_sections はネットワーク・実PDFに依存しないよう、実際の通達に見られる
表記ゆれ（全角/半角括弧の混在、枝番の有無）を再現した最小限の合成テキストで検証する。
allowlist の適用ロジックは新旧の項番ずれを吸収できているかを確認する。
"""

import unittest

from moegi.ingest import Notice, apply_allowlist, parse_sections

SAMPLE_TEXT = """\
第４ 訪問看護基本療養費及び精神科訪問看護基本療養費の共通事項について
１（１）同一の利用者について、保険医療機関において算定した場合は算定できない。
ア 基準告示第２の１に規定する疾病等の利用者について算定した場合
イ 特別訪問看護指示書の交付を受けた利用者である場合
(２) 特別の関係にある保険医療機関等において算定した日については算定できない。
２ 指定訪問看護の実施時間は、30 分から１時間 30 分程度を標準とする。なお、頻繁に
行われている場合には、実施したとは認められないことに留意すること。
４ 指定訪問看護の提供に当たっては、目標達成の程度及びその効果等について評価を
行い、その内容を訪問看護記録書に記録すること。
第５ 訪問看護管理療養費について
１(１) 訪問看護管理療養費は、安全な提供体制が整備されている場合に算定する。
"""


class ParseSectionsTest(unittest.TestCase):
    def test_builds_hierarchical_paths(self):
        sections = parse_sections("test-notice", SAMPLE_TEXT)
        by_path = {s.path: s for s in sections}

        for path in [
            "第4",
            "第4の1",
            "第4の1(1)",
            "第4の1(1)ア",
            "第4の1(1)イ",
            "第4の1(2)",
            "第4の2",
            "第4の4",
            "第5",
            "第5の1",
            "第5の1(1)",
        ]:
            self.assertIn(path, by_path)

    def test_text_content_matches_expected_paragraph(self):
        sections = parse_sections("test-notice", SAMPLE_TEXT)
        by_path = {s.path: s for s in sections}

        # 第4の4（評価の記録に相当する合成テキスト）の本文が正しく1段落抜き出せていること
        # （Section.text は元PDFの行送りをそのまま\nで保持するため、比較時は詰めて判定する）
        joined = by_path["第4の4"].text.replace("\n", "")
        self.assertIn("目標達成の程度及びその効果等について評価を行い", joined)

        # 枝番の内容が親（第4の1）には混ざらず、子ノード側だけに入っていること
        leaf_text = "基準告示第２の１に規定する疾病等の利用者について算定した場合"
        self.assertNotIn(leaf_text, by_path["第4の1"].text)
        self.assertIn(leaf_text, by_path["第4の1(1)ア"].text)

    def test_ignores_page_number_footer(self):
        text_with_footer = SAMPLE_TEXT.replace(
            "第５ 訪問看護管理療養費について",
            "- 13 -\n第５ 訪問看護管理療養費について",
        )
        sections = parse_sections("test-notice", text_with_footer)
        by_path = {s.path: s for s in sections}
        self.assertNotIn("- 13 -", by_path["第4の4"].text)


class ApplyAllowlistTest(unittest.TestCase):
    def test_uses_new_path_for_new_notice(self):
        sections = parse_sections("new-notice", SAMPLE_TEXT)
        notice = Notice(
            id="new-notice",
            title="t",
            issued_on="2026-03-05",
            version="new",
            source_url="https://example.invalid",
            raw_text=SAMPLE_TEXT,
            sections=sections,
        )
        targets = [
            {"label": "c", "title": "評価の記録", "new_path": "第4の4", "old_path": ""},
        ]
        result = apply_allowlist(notice, targets)
        self.assertIsNotNone(result["c"])
        self.assertIn("評価を", result["c"].text)

    def test_returns_none_when_old_path_is_empty(self):
        """完全新設項目は、旧文書に同名パスが存在しても意図的に None を返す。"""
        old_text = SAMPLE_TEXT.replace(
            "４ 指定訪問看護の提供に当たっては、目標達成の程度及びその効果等について評価を\n行い、その内容を訪問看護記録書に記録すること。",
            "４ これは全く別内容の条文である（評価の記録とは無関係）。",
        )
        sections = parse_sections("old-notice", old_text)
        notice = Notice(
            id="old-notice",
            title="t",
            issued_on="2024-03-05",
            version="old",
            source_url="https://example.invalid",
            raw_text=old_text,
            sections=sections,
        )
        targets = [
            {"label": "c", "title": "評価の記録", "new_path": "第4の4", "old_path": ""},
        ]
        result = apply_allowlist(notice, targets)
        self.assertIsNone(result["c"])

    def test_follows_shifted_old_path(self):
        """項番がずれている項目は、old_path で指定した旧文書側のパスを見に行く。"""
        sections = parse_sections("old-notice", SAMPLE_TEXT)
        notice = Notice(
            id="old-notice",
            title="t",
            issued_on="2024-03-05",
            version="old",
            source_url="https://example.invalid",
            raw_text=SAMPLE_TEXT,
            sections=sections,
        )
        # 「ab」は新文書で第4の6にあるが、このサンプルでは(旧を模して)第4の4に対応させる
        targets = [
            {"label": "ab", "title": "開始・終了時刻", "new_path": "第4の6", "old_path": "第4の4"},
        ]
        result = apply_allowlist(notice, targets)
        self.assertIsNotNone(result["ab"])
        self.assertIn("評価を", result["ab"].text)


if __name__ == "__main__":
    unittest.main()
