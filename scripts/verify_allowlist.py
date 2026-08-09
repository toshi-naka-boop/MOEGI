"""D1完了条件の検証スクリプト。

design.md §2「D1の完了条件」: 新旧PDFから allowlist を通して対象項目だけが
抽出できていること。このスクリプトは data/notices/ の新旧PDFに実際に allowlist を
適用し、各パスが何文字のテキストとして抽出できたかを一覧で出す。

実行:
    PYTHONPATH=src python3 scripts/verify_allowlist.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from moegi.ingest import apply_allowlist, load_allowlist, load_notice  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
NEW_PDF = ROOT / "data" / "notices" / "new_hohatsu0305-19.pdf"
OLD_PDF = ROOT / "data" / "notices" / "old_hohatsu0305-12.pdf"
ALLOWLIST_PATH = ROOT / "config" / "allowlist.toml"


def main() -> int:
    if not NEW_PDF.exists() or not OLD_PDF.exists():
        print(f"通達PDFが見つかりません: {NEW_PDF} / {OLD_PDF}")
        print("先に data/notices/ にPDFをダウンロードしてください。")
        return 1

    targets = load_allowlist(ALLOWLIST_PATH)

    new_notice = load_notice(
        NEW_PDF,
        id="hohatsu0305-19",
        title="訪問看護療養費に係る指定訪問看護の費用の額の算定方法の一部改正に伴う実施上の留意事項について",
        issued_on="2026-03-05",
        version="new",
        source_url="https://www.mhlw.go.jp/content/12400000/001686845.pdf",
    )
    old_notice = load_notice(
        OLD_PDF,
        id="hohatsu0305-12",
        title="訪問看護療養費に係る指定訪問看護の費用の額の算定方法の一部改正に伴う実施上の留意事項について",
        issued_on="2024-03-05",
        version="old",
        source_url="https://www.mhlw.go.jp/content/12404000/001241054.pdf",
    )

    new_result = apply_allowlist(new_notice, targets)
    old_result = apply_allowlist(old_notice, targets)

    print(f"新PDF sections総数: {len(new_notice.sections)}")
    print(f"旧PDF sections総数: {len(old_notice.sections)}")
    print()
    header = f"{'label':6} {'title':46} {'new_path':16} {'new文字数':>10} {'old_path':16} {'old文字数':>10}"
    print(header)
    print("-" * len(header))

    ok = True
    for t in targets:
        label = t["label"]
        new_sec = new_result.get(label)
        old_sec = old_result.get(label)
        new_path = t["new_path"]
        old_path = t["old_path"] or "(旧文書に対応なし)"
        new_chars = str(len(new_sec.text)) if new_sec else "抽出失敗"
        old_chars = str(len(old_sec.text)) if old_sec else ("---" if not t["old_path"] else "抽出失敗")

        if new_sec is None:
            ok = False
        if t["old_path"] and old_sec is None:
            ok = False

        print(f"{label:6} {t['title']:46} {new_path:16} {new_chars:>10} {old_path:16} {old_chars:>10}")

    print()
    if ok:
        print("結果: 新PDFの全対象パス・旧PDFで対応が定義されている全パスが抽出できました。")
    else:
        print("結果: 抽出に失敗したパスがあります（上表参照）。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
