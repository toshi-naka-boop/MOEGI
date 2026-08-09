"""通達PDFの取込・正規化（design.md §6.1 第一原則: LLMを使わない決定的処理）。

厚労省の留意事項通知（保発0305号）は、日本の行政文書に典型的な
「第４ > １ > (１) > ア」という4階層の項番構造を持つ。本モジュールは
その構造をルールベースでパースし、design.md §6.4 の Notice / Section を組み立てる。

さらに、design.md §2 の allowlist（対象条文パスの一覧）を設定ファイルから読み込み、
その対象パスだけを切り出す関数を提供する。
"""

from __future__ import annotations

import re
import tomllib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber

# ---------------------------------------------------------------------------
# データモデル（design.md §6.4）
# ---------------------------------------------------------------------------


@dataclass
class Section:
    """条項 { notice_id, path, text, offset }（design.md §6.4）"""

    notice_id: str
    path: str
    text: str
    offset: int  # raw_text内での開始位置（文字オフセット）


@dataclass
class Notice:
    """通達 { id, title, issued_on, version, source_url, raw_text, sections[] }（design.md §6.4）"""

    id: str
    title: str
    issued_on: str
    version: str  # "old" | "new"
    source_url: str
    raw_text: str
    sections: list[Section] = field(default_factory=list)


# ---------------------------------------------------------------------------
# PDF取込
# ---------------------------------------------------------------------------


def extract_raw_text(pdf_path: str | Path) -> str:
    """pdfplumberでPDF全ページのテキストを抽出し、1本のテキストに結合する。

    各ページのテキストを改行区切りで連結する。以降の正規化・パースは
    この raw_text を対象に行うため、Section.offset はこの文字列上の位置になる。
    """
    lines: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            lines.extend(page_text.split("\n"))
    return "\n".join(lines)


# ページフッターの「- 13 -」のようなノンブルは条文本文ではないので取り除く
_PAGE_NUMBER_RE = re.compile(r"^-\s*\d+\s*-$")


def _normalize_lines(raw_text: str) -> list[str]:
    """ノンブル行を空行に置き換える（offsetを保つため行数・文字数は変えない）。"""
    normalized = []
    for line in raw_text.split("\n"):
        if _PAGE_NUMBER_RE.match(line.strip()):
            normalized.append("")
        else:
            normalized.append(line)
    return normalized


# ---------------------------------------------------------------------------
# 章立てパースの正規表現
#
# 厚労省の留意事項通知は全角数字・全角/半角括弧が混在する（実物のPDFで確認済み）。
# 例:
#   第４ 訪問看護基本療養費...について        ← 第1階層（dai）見出し
#   １（１）同一の利用者について、...           ← 第2階層(item)+第3階層(paren)が同一行
#   ２ 指定訪問看護の実施時間は、...           ← 第2階層(item)のみ（枝番なしの1段落）
#   (４) (１)の場合においては、...            ← 第3階層(paren)のみ（枝番の続き）
#   ア 訪問看護療養費を算定するに適切な...      ← 第4階層(kana)
# ---------------------------------------------------------------------------

_ZEN_DIGITS = "０-９"
_WS = " 　\t"  # 半角スペース／全角スペース／タブ

# 第N + 空白 + 見出し文言（「第４の２に」のような本文中の参照は、数字の直後が空白でないため除外される）
_RE_DAI = re.compile(rf"^第(?P<num>[{_ZEN_DIGITS}0-9]+)[{_WS}]+(?P<rest>\S.*)$")

# N（M）... : 第2階層+第3階層が同一行（数字とカッコの間に空白なし）
_RE_ITEM_PAREN = re.compile(
    rf"^(?P<item>[{_ZEN_DIGITS}0-9]{{1,2}})[（(](?P<paren>[{_ZEN_DIGITS}0-9]{{1,2}})[）)][{_WS}]*(?P<rest>.*)$"
)

# N + 空白 + 本文 : 第2階層のみ（枝番なしの1段落項目）
_RE_ITEM = re.compile(rf"^(?P<item>[{_ZEN_DIGITS}0-9]{{1,2}})[{_WS}]+(?P<rest>.*)$")

# （M） + 本文 : 第3階層のみ（既に開いている第2階層の続き）
_RE_PAREN = re.compile(rf"^[（(](?P<paren>[{_ZEN_DIGITS}0-9]{{1,2}})[）)][{_WS}]*(?P<rest>.*)$")

# カタカナ1文字 + 空白 + 本文 : 第4階層（イロハ）
_RE_KANA = re.compile(rf"^(?P<kana>[ア-ン])[{_WS}]+(?P<rest>.*)$")


def _to_halfwidth_digits(s: str) -> str:
    """全角数字を半角に正規化する（path文字列を design.md 表記に揃えるため）。"""
    return unicodedata.normalize("NFKC", s)


class _Node:
    """パース中に使う一時ノード。確定したら Section に変換する。"""

    __slots__ = ("path", "offset", "own_lines")

    def __init__(self, path: str, offset: int):
        self.path = path
        self.offset = offset
        self.own_lines: list[str] = []


def parse_sections(notice_id: str, raw_text: str) -> list[Section]:
    """raw_textを第N > N > (N) > イロハ の4階層でパースし、Sectionのフラットな列を返す。

    全階層をそれぞれ独立した Section として返す（例えば「第4の6」自体も、
    その配下に枝番があれば「第4の6(1)」も、両方 Section になる）。
    allowlist で使う粒度は呼び出し側（apply_allowlist）が path で選ぶ。
    """
    lines = _normalize_lines(raw_text)

    sections: list[Section] = []
    # 各階層の現在開いているノード（Noneなら未オープン）
    stack: list[_Node | None] = [None, None, None, None]  # dai, item, paren, kana

    def close_from(level: int) -> None:
        """level以降（0-indexed、深い方）で開いているノードを確定してSection化する。"""
        for i in range(3, level - 1, -1):
            node = stack[i]
            if node is not None:
                text = "\n".join(ln for ln in node.own_lines if ln.strip())
                sections.append(Section(notice_id=notice_id, path=node.path, text=text, offset=node.offset))
                stack[i] = None

    def deepest_open() -> _Node | None:
        for node in reversed(stack):
            if node is not None:
                return node
        return None

    offset = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            offset += len(line) + 1
            continue

        m_dai = _RE_DAI.match(stripped)
        m_item_paren = _RE_ITEM_PAREN.match(stripped) if not m_dai else None
        m_item = _RE_ITEM.match(stripped) if not m_dai and not m_item_paren else None
        m_paren = (
            _RE_PAREN.match(stripped)
            if not m_dai and not m_item_paren and not m_item
            else None
        )
        m_kana = (
            _RE_KANA.match(stripped)
            if not m_dai and not m_item_paren and not m_item and not m_paren
            else None
        )

        if m_dai:
            close_from(0)
            num = _to_halfwidth_digits(m_dai.group("num"))
            path = f"第{num}"
            rest_offset = offset + stripped.index(m_dai.group("rest"))
            node = _Node(path, rest_offset)
            node.own_lines.append(m_dai.group("rest"))
            stack[0] = node

        elif m_item_paren and stack[0] is not None:
            close_from(1)
            item_num = _to_halfwidth_digits(m_item_paren.group("item"))
            paren_num = _to_halfwidth_digits(m_item_paren.group("paren"))
            item_path = f"{stack[0].path}の{item_num}"
            item_node = _Node(item_path, offset)
            stack[1] = item_node

            paren_path = f"{item_path}({paren_num})"
            rest = m_item_paren.group("rest")
            rest_offset = offset + stripped.index(rest) if rest else offset + len(stripped)
            paren_node = _Node(paren_path, rest_offset)
            paren_node.own_lines.append(rest)
            stack[2] = paren_node

        elif m_item and stack[0] is not None:
            close_from(1)
            item_num = _to_halfwidth_digits(m_item.group("item"))
            path = f"{stack[0].path}の{item_num}"
            rest = m_item.group("rest")
            rest_offset = offset + stripped.index(rest) if rest else offset + len(stripped)
            node = _Node(path, rest_offset)
            node.own_lines.append(rest)
            stack[1] = node

        elif m_paren and stack[1] is not None:
            close_from(2)
            paren_num = _to_halfwidth_digits(m_paren.group("paren"))
            path = f"{stack[1].path}({paren_num})"
            rest = m_paren.group("rest")
            rest_offset = offset + stripped.index(rest) if rest else offset + len(stripped)
            node = _Node(path, rest_offset)
            node.own_lines.append(rest)
            stack[2] = node

        elif m_kana and (stack[2] is not None or stack[1] is not None):
            close_from(3)
            parent = stack[2] if stack[2] is not None else stack[1]
            kana = m_kana.group("kana")
            path = f"{parent.path}{kana}"
            rest = m_kana.group("rest")
            rest_offset = offset + stripped.index(rest) if rest else offset + len(stripped)
            node = _Node(path, rest_offset)
            node.own_lines.append(rest)
            stack[3] = node

        else:
            # 見出しにマッチしない行 = 現在開いている最深ノードの続き（本文の折り返し）
            node = deepest_open()
            if node is not None:
                node.own_lines.append(stripped)

        offset += len(line) + 1

    close_from(0)
    return sections


def load_notice(
    pdf_path: str | Path,
    *,
    id: str,
    title: str,
    issued_on: str,
    version: str,
    source_url: str,
) -> Notice:
    """PDFを読み込み、Notice（sections込み）を組み立てる。"""
    raw_text = extract_raw_text(pdf_path)
    sections = parse_sections(id, raw_text)
    return Notice(
        id=id,
        title=title,
        issued_on=issued_on,
        version=version,
        source_url=source_url,
        raw_text=raw_text,
        sections=sections,
    )


# ---------------------------------------------------------------------------
# allowlist（design.md §2・§10 D1完了条件）
# ---------------------------------------------------------------------------


def load_allowlist(config_path: str | Path) -> list[dict]:
    """config/allowlist.toml の targets を読み込む。"""
    with open(config_path, "rb") as f:
        data = tomllib.load(f)
    return data["targets"]


def apply_allowlist(notice: Notice, targets: list[dict]) -> dict[str, Section | None]:
    """allowlistの各対象について、notice.version に応じたpathでSectionを切り出す。

    新旧で項番がずれている項目は targets 側の new_path / old_path で吸収する。
    旧文書に対応する条文が存在しない項目（完全新設）は old_path が空文字列であり、
    その場合 old な notice に対しては None を返す（同じパス文字列を旧文書にも
    使い回すと、無関係な条文を誤って対象扱いしてしまうため、意図的に抽出しない）。
    """
    by_path = {s.path: s for s in notice.sections}
    result: dict[str, Section | None] = {}
    for target in targets:
        label = target["label"]
        path = target["new_path"] if notice.version == "new" else target["old_path"]
        if not path:
            result[label] = None
            continue
        result[label] = by_path.get(path)
    return result
