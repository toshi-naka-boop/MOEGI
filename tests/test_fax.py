"""fax.py の単体テスト（design.md §5.3・R-39・R-43）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from moegi import fax


class RenderFaxHtmlTest(unittest.TestCase):
    def test_includes_site_title_and_before_after(self):
        html = fax.render_fax_html(
            "◯◯訪問看護ステーション",
            [{"sitedoc_item_id": "バイタルサインの記録", "before": "バイタルサインの記録", "after": "毎回体温を記録する。"}],
        )
        self.assertIn("◯◯訪問看護ステーション", html)
        self.assertIn("バイタルサインの記録", html)
        self.assertIn("毎回体温を記録する。", html)

    def test_new_item_shows_placeholder_before_text(self):
        html = fax.render_fax_html(
            "s", [{"sitedoc_item_id": None, "before": "", "after": "評価欄を新設する。"}]
        )
        self.assertIn("（新規項目）", html)
        self.assertIn("現在この項目はチェックリストに存在しない", html)

    def test_empty_proposals_shows_placeholder_row(self):
        html = fax.render_fax_html("s", [])
        self.assertIn("承認済みの更新案がありません", html)

    def test_escapes_html_in_proposal_text(self):
        html = fax.render_fax_html(
            "s", [{"sitedoc_item_id": "x", "before": "x", "after": "<script>alert(1)</script>"}]
        )
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


class GenerateFaxPdfTest(unittest.TestCase):
    def test_writes_a_pdf_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "out.pdf"
            result = fax.generate_fax_pdf(
                "◯◯訪問看護ステーション",
                [{"sitedoc_item_id": "x", "before": "x", "after": "y"}],
                out_path,
            )
            self.assertEqual(result, out_path)
            self.assertTrue(out_path.exists())
            self.assertGreater(out_path.stat().st_size, 0)
            self.assertEqual(out_path.read_bytes()[:5], b"%PDF-")

    def test_creates_parent_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "nested" / "dir" / "out.pdf"
            fax.generate_fax_pdf("s", [], out_path)
            self.assertTrue(out_path.exists())


if __name__ == "__main__":
    unittest.main()
