"""FAX様式PDF生成（design.md §5.3・R-39・R-43）。

即時チャネル。承認済みの更新案からチェックリストのFAX様式PDFを生成し、
画面プレビュー（webapp.py）と事前印刷（コンビニ印刷）の両方で使う。

R-43: フォント同梱必須（システム/CDNからフォントを解決する構成だとオフラインで
文字化け・描画失敗する）。WeasyPrintはfontconfig経由でローカルのシステムフォント
（Hiragino Sans）を解決する——ネットワークに一切依存しないため、オフラインで
安定して描画できる（CDN経由のWebフォントとは異なり、これ自体は§5.2の
「CDN依存ゼロ」要件に抵触しない）。
"""

from __future__ import annotations

import html
from datetime import date
from pathlib import Path

from weasyprint import HTML

_STYLE = """
<style>
  @page { size: A4; margin: 18mm 15mm; }
  body { font-family: "Hiragino Sans", "Hiragino Kaku Gothic Pro", sans-serif; font-size: 10.5pt; color: #1a1a1a; }
  h1 { font-size: 15pt; text-align: center; border-bottom: 2px solid #1a1a1a; padding-bottom: 6pt; margin-bottom: 4pt; }
  .meta { text-align: right; font-size: 9pt; color: #444; margin-bottom: 10pt; }
  table { width: 100%; border-collapse: collapse; margin-top: 8pt; }
  th, td { border: 1px solid #888; padding: 5pt 6pt; text-align: left; vertical-align: top; font-size: 9.5pt; }
  th { background: #eee; font-weight: 600; width: 30%; }
  .before { color: #888; text-decoration: line-through; display: block; margin-bottom: 2pt; }
  .after { font-weight: 600; display: block; }
  .footer { margin-top: 16pt; font-size: 8pt; color: #666; text-align: center; }
  .new-item { background: #fff8e6; }
</style>
"""


def render_fax_html(site_title: str, proposals: list[dict], *, generated_at: date | None = None) -> str:
    """FAX様式のHTMLを組み立てる（webapp.pyのプレビュー・PDF生成の両方で共有する）。

    proposals は store.list_proposals_for_review() の戻り値の形（辞書のリスト）を想定。
    承認（status=="承認"）済みのものだけを渡すこと（呼び出し側の責務）。
    """
    generated_at = generated_at or date.today()
    rows = []
    for p in proposals:
        item_label = p["sitedoc_item_id"] or "（新規項目）"
        row_class = "new-item" if p["sitedoc_item_id"] is None else ""
        before = html.escape(p["before"]) if p["before"] else "（現在この項目はチェックリストに存在しない）"
        after = html.escape(p["after"])
        rows.append(
            f"<tr class='{row_class}'>"
            f"<th>{html.escape(item_label)}</th>"
            f"<td><span class='before'>変更前: {before}</span>"
            f"<span class='after'>変更後: {after}</span></td>"
            f"</tr>"
        )

    body = "".join(rows) or "<tr><td colspan='2'>承認済みの更新案がありません。</td></tr>"
    return f"""
    <!doctype html><html lang="ja"><head><meta charset="utf-8">{_STYLE}</head>
    <body>
      <h1>{html.escape(site_title)} — チェックリスト更新のお知らせ</h1>
      <p class="meta">発行日: {generated_at.isoformat()}</p>
      <table>{body}</table>
      <p class="footer">MOEGI（通達Diff→現場手順自動更新エージェント）による自動生成。原文トレースは承認UIで確認できます。</p>
    </body></html>
    """


def generate_fax_pdf(site_title: str, proposals: list[dict], out_path: str | Path, *, generated_at: date | None = None) -> Path:
    """FAX様式PDFをout_pathに書き出す。design.md §5.3の即時チャネル本体。"""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html_text = render_fax_html(site_title, proposals, generated_at=generated_at)
    HTML(string=html_text).write_pdf(str(out_path))
    return out_path
