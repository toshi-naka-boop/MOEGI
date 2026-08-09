"""通達Diff→更新案パイプライン（design.md §6.1・§6.4）。

ingest.py の出力（allowlist適用済みSection）を入力に、4工程を順に実行する:
    ②差分抽出（diff・クラウド既定）
    ③-a 要件抽出（requirement・クラウド既定・通達本文のみ）
    ③-b 突合（matching・ローカル固定・事業所文書を含む）
    ④更新案生成（draft・ローカル固定・事業所文書を含む）

各工程の実行主体は llm.LLMAdapter。ルーティングの第一原則（事業所文書が入る
matching/draftは常にローカル）は llm.py 側で強制されるため、ここでは
stage名を渡すだけでよい。
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from moegi import store
from moegi.ingest import Section
from moegi.llm import LLMAdapter

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SITEDOC_PATH = _REPO_ROOT / "config" / "sitedoc_sample.toml"

# design.md §6.4 の Change.kind には「追加」があり、完全新設の条文は既存チェックリスト項目に
# 必ずしも対応しない。既存項目のどれにも該当しなかった場合、Impact.sitedoc_item_id は
# None（新規項目の提案）を持つ（§突合の実装を参照）。


# ---------------------------------------------------------------------------
# データモデル（design.md §6.4）
#
# Change / Impact / Proposal には設計書に明示のid項目が無いが、相互参照
# （change_id / impact_id / sitedoc_item_id）を実装するために以下の方針でキーを採る:
#   - Change.id       : allowlistのlabel（a/b/c/... ingest.pyで既に一意）をそのまま使う
#   - ChecklistItem    : ingest.Sectionのpathと同様、labelを自然キーとして使う（id列を持たない）
#   - Impact.id        : f"impact-{change.id}-{item_labelまたは'new'}"（決定的・再現可能）
#   - Proposal.id      : f"proposal-{impact.id}"
# ---------------------------------------------------------------------------


@dataclass
class ChecklistItem:
    """項目（design.md §6.4 ChecklistItem）。"""

    sitedoc_id: str
    label: str
    kind: str  # "自由記述" | "選択" | "評価"
    goal_ref: str | None = None
    required: bool = True


@dataclass
class SiteDoc:
    """事業所文書（design.md §6.4 SiteDoc）。"""

    id: str
    kind: str  # "チェックリスト" | "手順書"
    title: str
    items: list[ChecklistItem] = field(default_factory=list)


@dataclass
class Change:
    """差分（design.md §6.4 Change）。"""

    id: str
    old_section: Section | None
    new_section: Section | None
    kind: str  # "追加" | "削除" | "改正"
    summary: str = ""


@dataclass
class Requirement:
    """③-a 要件抽出の出力（通達本文のみから抽出した「現場に何が要求されたか」）。"""

    change_id: str
    text: str


@dataclass
class Evidence:
    """原文トレース根（design.md §6.4 Impact.evidence）。"""

    notice_section_path: str
    quote: str
    offset: int


@dataclass
class Impact:
    """影響判定（design.md §6.4 Impact）。evidenceが空の場合は提案を作らない。"""

    id: str
    change_id: str
    sitedoc_item_id: str | None  # None = 既存項目に該当なし（新規項目の提案）
    verdict: str  # "要更新" | "影響なし"
    rationale: str
    confidence: float
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class Proposal:
    """更新案（design.md §6.4 Proposal）。"""

    id: str
    impact_id: str
    before: str
    after: str
    status: str = "未承認"  # "未承認" | "承認" | "却下" | "編集済"


# ---------------------------------------------------------------------------
# SiteDoc読み込み
# ---------------------------------------------------------------------------


def load_sitedoc(path: str | Path = DEFAULT_SITEDOC_PATH) -> SiteDoc:
    """config/sitedoc_sample.toml（design.md §9）を読み込む。"""
    with open(path, "rb") as f:
        data = tomllib.load(f)
    items = [
        ChecklistItem(
            sitedoc_id=data["id"],
            label=item["label"],
            kind=item["kind"],
            goal_ref=item.get("goal_ref"),
            required=item.get("required", True),
        )
        for item in data.get("items", [])
    ]
    return SiteDoc(id=data["id"], kind=data["kind"], title=data["title"], items=items)


# ---------------------------------------------------------------------------
# ②差分抽出（LLM不使用の突き合わせ部分）
# ---------------------------------------------------------------------------


def build_changes(
    old_sections: dict[str, Section | None],
    new_sections: dict[str, Section | None],
) -> list[Change]:
    """allowlist適用済みSectionからChangeの骨格を組み立てる（summaryはまだ空）。

    old_path が空（完全新設）の項目は ingest.apply_allowlist が既に None を返しているため、
    ここでは old_section is None を「追加」として扱う。
    """
    changes: list[Change] = []
    for label, new_section in new_sections.items():
        if new_section is None:
            continue  # 新版に対象条文が見つからない（想定外・スキップ）
        old_section = old_sections.get(label)
        kind = "追加" if old_section is None else "改正"
        changes.append(Change(id=label, old_section=old_section, new_section=new_section, kind=kind))
    return changes


_DIFF_PROMPT_NEW = """以下は訪問看護に関する厚生労働省通知の条文です。今回の改定で新設された条文であり、旧版には対応する記載がありません。
現場向けに、何が新しく定められたのかを1〜2文の日本語で要約してください。前置きや記号は付けず、要約文のみを出力すること。

【条文パス】{path}
【新版の条文】
{text}
"""

_DIFF_PROMPT_REVISED = """以下は訪問看護に関する厚生労働省通知の、同一条文の旧版と新版です。
何がどう変わったかを1〜2文の日本語で要約してください。前置きや記号は付けず、要約文のみを出力すること。

【条文パス】{path}

【旧版】
{old_text}

【新版】
{new_text}
"""


def extract_change_summaries(
    changes: list[Change], adapter: LLMAdapter, *, run_id: str, store_conn=None
) -> None:
    """②差分抽出（design.md §6.1）。通達本文のみが入る。各Change.summaryを埋める（in-place）。"""
    for change in changes:
        if change.kind == "追加":
            prompt = _DIFF_PROMPT_NEW.format(path=change.new_section.path, text=change.new_section.text)
        else:
            prompt = _DIFF_PROMPT_REVISED.format(
                path=change.new_section.path,
                old_text=change.old_section.text,
                new_text=change.new_section.text,
            )
        result = adapter.complete("diff", prompt)
        if store_conn is not None:
            store.record_from_llm_result(store_conn, run_id, "diff", result)
        # design.md §6.6（v10）: ローカル呼び出しがtimeoutした場合は例外を上げず
        # 「判定不能」として記録し、パイプラインを続行する（黙って捨てない）。
        change.summary = "判定不能" if result.get("timeout") else result["text"].strip()


# ---------------------------------------------------------------------------
# ③-a 要件抽出（クラウド既定・通達本文のみ）
# ---------------------------------------------------------------------------

_REQUIREMENT_PROMPT = """以下は訪問看護に関する厚生労働省通知の条文と、その変更点の要約です。
この変更によって、訪問看護の現場に新たに何が求められるようになったかを、
チェックリストに落とし込める簡潔な1文で述べてください。
前置きや見出しは付けず、要求事項の文のみを出力すること。

【条文パス】{path}
【変更点】{summary}
【条文本文（新版）】
{text}
"""


def extract_requirements(
    changes: list[Change], adapter: LLMAdapter, *, run_id: str, store_conn=None
) -> list[Requirement]:
    """③-a 要件抽出（design.md §6.1）。事業所文書を含まない（通達本文のみ）。"""
    requirements: list[Requirement] = []
    for change in changes:
        prompt = _REQUIREMENT_PROMPT.format(
            path=change.new_section.path, summary=change.summary, text=change.new_section.text
        )
        result = adapter.complete("requirement", prompt)
        if store_conn is not None:
            store.record_from_llm_result(store_conn, run_id, "requirement", result)
        # design.md §6.6（v10）: timeoutは例外を上げず「判定不能」として続行する。
        text = "判定不能" if result.get("timeout") else result["text"].strip()
        requirements.append(Requirement(change_id=change.id, text=text))
    return requirements


# ---------------------------------------------------------------------------
# ③-b 突合（ローカル固定・事業所文書を含む）
# ---------------------------------------------------------------------------

# ローカル8Bクラスのモデルは、全項目を1回のプロンプトにまとめて判定させると
# 出力形式が崩れやすい（実機検証で確認: 選択肢を"AまたはB"のまま書き写す・JSON崩れ・
# 実行時間39秒超）。そのため1項目=1呼び出しに分割し、既存項目のどれにも該当しなかった
# 場合のみ「新規項目が必要か」を追加で1回問う（design.md §6.4 のChange.kind「追加」に対応）。
_MATCH_ITEM_PROMPT = """あなたは訪問看護事業所のチェックリスト担当者です。
以下の「現場への要求事項」と「通達原文」をもとに、事業所チェックリストの1項目
「{item_label}」が、この要求事項によって「要更新」か「影響なし」かを判定してください。

【現場への要求事項】
{requirement_text}

【通達原文（{path}）】
{notice_text}

【判定対象のチェックリスト項目】
{item_label}

【出力形式】
次のJSONオブジェクトのみを出力すること。前置き・説明文・コードブロック記号は付けないこと。
verdictの値は "要更新" か "影響なし" のどちらか一方をそのまま使うこと（他の文字列を使わない・両方書かない）。
evidence_quoteは、verdictが"要更新"の場合のみ、【通達原文】の中の文字列を一字一句そのまま抜き出して入れること（原文に無い文章を作らない）。影響なしの場合は空文字("")。

出力例1（該当する場合）:
{{"verdict": "要更新", "rationale": "判定理由をここに", "confidence": 0.8, "evidence_quote": "通達原文中の一致する部分文字列"}}

出力例2（該当しない場合）:
{{"verdict": "影響なし", "rationale": "判定理由をここに", "confidence": 0.8, "evidence_quote": ""}}
"""

_MATCH_NEW_ITEM_PROMPT = """あなたは訪問看護事業所のチェックリスト担当者です。
以下の「現場への要求事項」は、事業所チェックリストの既存項目（{existing_labels}）の
どれにも該当しないと判断されました。この要求事項に対応するために、
チェックリストへ新規項目を追加する必要があるかどうかを判定してください。

【現場への要求事項】
{requirement_text}

【通達原文（{path}）】
{notice_text}

【出力形式】
次のJSONオブジェクトのみを出力すること。前置き・説明文・コードブロック記号は付けないこと。
neededの値は true か false のいずれかの真偽値。
evidence_quoteは、neededがtrueの場合のみ、【通達原文】の中の文字列を一字一句そのまま抜き出して入れること（原文に無い文章を作らない）。falseの場合は空文字("")。

出力例1（必要な場合）:
{{"needed": true, "rationale": "判定理由をここに", "confidence": 0.8, "evidence_quote": "通達原文中の一致する部分文字列"}}

出力例2（不要な場合）:
{{"needed": false, "rationale": "判定理由をここに", "confidence": 0.8, "evidence_quote": ""}}
"""


def _format_items_block(items: list[ChecklistItem]) -> str:
    """一括判定プロンプト用に、項目へ1始まりの番号を振って列挙する。

    番号（item_id）はプロンプトと応答の間だけで使う識別子で、Impactに載せる際は
    必ず label へ引き戻す。長いラベル文字列をLLMに書き写させると表記ゆれで
    突き合わせが壊れるため、短い番号を経由させる。
    """
    return "\n".join(f"{i}. {item.label}（種別: {item.kind}）" for i, item in enumerate(items, start=1))


_TRAILING_FIELD_RE = re.compile(r'"(\w+)"\s*:\s*("(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?|true|false)')


def _recover_trailing_fields(entry: dict, text: str, expected_keys: tuple[str, ...]) -> dict:
    """途中で閉じたJSONの外側に漏れたキーを、テキスト全体から正規表現で拾って補う。

    ローカル8Bモデルの実測で確認した崩れ方: `rationale` の直後で早々に "}" を
    出してオブジェクトを閉じ、`confidence`/`evidence_quote` がオブジェクトの外側の
    ただの `"key": value, ...` として続く。json.loads はオブジェクトの中身しか
    見ないため、この漏れたキーは黙って失われ、evidence_quote が無いという理由で
    正しい「要更新」判定ごと捨てられていた。1件のオブジェクトしか無い前提
    （単発モードの1呼び出し1判定）でのみ安全。複数の判定が並ぶ一括モードの
    応答には使わない（別の判定のキーを誤って拾う恐れがあるため）。
    """
    missing = [k for k in expected_keys if k not in entry]
    if not missing:
        return entry
    recovered = dict(entry)
    for match in _TRAILING_FIELD_RE.finditer(text):
        key, raw_value = match.group(1), match.group(2)
        if key not in missing:
            continue
        try:
            recovered[key] = json.loads(raw_value)
        except json.JSONDecodeError:
            recovered[key] = raw_value
        missing.remove(key)
    return recovered


_MATCH_ITEM_EXPECTED_KEYS = ("verdict", "rationale", "confidence", "evidence_quote")
_MATCH_NEW_ITEM_EXPECTED_KEYS = ("needed", "rationale", "confidence", "evidence_quote")


def _extract_json_object(text: str, expected_keys: tuple[str, ...] = ()) -> dict | None:
    """LLM出力からJSONオブジェクトを1つ抜き出す。前後に余計な文・コードブロック記号が付いても拾う。

    非貪欲マッチ（最初の"{"から最初の"}"まで）を使う。判定結果のJSONはキーが
    文字列・数値・真偽値のみのフラットな構造なので、ネストした"{"は現れない前提。
    expected_keys を渡すと、途中で閉じたJSONの外側に漏れたキーを補う
    （_recover_trailing_fields）。
    """
    match = re.search(r"\{.*?\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return _recover_trailing_fields(data, text, expected_keys) if expected_keys else data


def _payload_list(payload: dict | None, *aliases: str) -> list:
    """一括応答から判定リストを取り出す。

    キー名はモデルが勝手に綴りを変える（実測: judgements と指示したのに judgments を返した）。
    別名を許し、それでも見つからなければ「唯一のリスト値」を採る。ここで諦めると
    正しく判定できていた項目まで判定不能に落ちてしまい、条件1の検知が
    「モデルの綴り揺れ」と「本当の判定漏れ」を区別できなくなる。
    """
    if not payload:
        return []
    for alias in aliases:
        value = payload.get(alias)
        if isinstance(value, list):
            return value
    lists = [v for v in payload.values() if isinstance(v, list)]
    return lists[0] if len(lists) == 1 else []


def _extract_json_payload(text: str) -> dict | None:
    """LLM出力から最初の完結したJSONオブジェクトを取り出す（配列・ネストを含んでよい）。

    一括判定の応答は judgements 配列を持つネスト構造なので、_extract_json_object の
    非貪欲マッチでは最初の要素の "}" で切れてしまう。ここでは文字列リテラルを
    読み飛ばしながら波括弧の対応を数え、閉じた時点で json.loads を試す。
    """
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break  # このstartからは組み立てられない。次の "{" を試す
                    return data if isinstance(data, dict) else None
        start = text.find("{", start + 1)
    return None


def _find_quote_offset(quote: str, source_text: str) -> int:
    """quoteをsource_text内で探す。

    PDF抽出テキストは行送りの都合で、日本語の文中（単語の区切りとは無関係な位置）に
    改行が入ることがあり、LLMが引用する際はその改行が失われがち。まず完全一致を試し、
    失敗したら双方から空白類（改行含む）を取り除いた「詰めた」文字列同士で照合し、
    見つかった位置を元のsource_text上のオフセットへマップし直す。
    """
    offset = source_text.find(quote)
    if offset != -1:
        return offset

    compact_chars: list[str] = []
    index_map: list[int] = []  # compact_chars[i] は source_text[index_map[i]] に対応
    for i, ch in enumerate(source_text):
        if not ch.isspace():
            compact_chars.append(ch)
            index_map.append(i)
    compact_source = "".join(compact_chars)
    compact_quote = "".join(ch for ch in quote if not ch.isspace())

    if not compact_quote:
        return -1
    compact_offset = compact_source.find(compact_quote)
    if compact_offset == -1:
        return -1
    return index_map[compact_offset]


def _verified_evidence(entry: dict, change: Change) -> list[Evidence] | None:
    """LLMが返した evidence_quote が通達原文中に実在するか裏取りする（幻覚対策）。

    実在しなければ None を返す。design.md §6.4「Impact.evidenceは必須項目。
    空の更新案は承認UIに出さない」を、単発モードと一括モードの両方で同一に守るため
    共通化してある（D4のゴールドセット比較で、2モードの差が判定方法の差にならないように）。
    """
    quote = str(entry.get("evidence_quote") or "").strip()
    if not quote:
        return None
    offset = _find_quote_offset(quote, change.new_section.text)
    if offset == -1:
        return None
    return [Evidence(notice_section_path=change.new_section.path, quote=quote, offset=offset)]


def _leading_quote(text: str, limit: int = 120) -> str:
    """条文本文の冒頭を、決定的な原文トレース根として切り出す。"""
    head = text.strip()[:limit]
    sentence_end = head.find("。")
    return head[: sentence_end + 1] if sentence_end != -1 else head


def _forced_new_item_impact(change: Change, requirement_text: str, entry: dict | None) -> Impact:
    """完全新設の条文には、新規項目の提案を必ず作る（ledger N-7・v11でユーザー確定）。

    旧版に対応条文が無い（`Change.kind == "追加"`）ということは、その要求に対応する
    既存項目もチェックリストに無い。にもかかわらず、ローカル8Bが既存項目を過剰に
    「要更新」と返すたびに新規項目の提案が潰れ、デモのアンカーが出なくなっていた。
    → **提案を作るかどうかはLLMの needed 判定に委ねず、条文の種別で決める。**
    LLMの rationale / evidence は使えるときだけ使い、使えなければ条文冒頭を根拠に据える
    （新設された条文そのものが根拠なので、これは捏造にならない）。
    """
    evidence = _verified_evidence(entry, change) if entry else None
    if evidence is None:
        evidence = [
            Evidence(
                notice_section_path=change.new_section.path,
                quote=_leading_quote(change.new_section.text),
                offset=0,
            )
        ]
    rationale = str((entry or {}).get("rationale") or "").strip() or requirement_text
    return Impact(
        id=f"impact-{change.id}-new",
        change_id=change.id,
        sitedoc_item_id=None,
        verdict="要更新",
        rationale=rationale,
        confidence=float((entry or {}).get("confidence") or 0.0),
        evidence=evidence,
    )


def _undecidable_impact(change: Change, item_label: str | None, reason: str) -> Impact:
    """design.md §6.6（v10）: 判定できなかった項目を黙って捨てず「判定不能」として残す。"""
    return Impact(
        id=f"impact-{change.id}-{item_label or 'new'}",
        change_id=change.id,
        sitedoc_item_id=item_label,
        verdict="判定不能",
        rationale=reason,
        confidence=0.0,
    )


def match_requirements(
    requirements: list[Requirement],
    changes_by_id: dict[str, Change],
    sitedoc: SiteDoc,
    adapter: LLMAdapter,
    *,
    run_id: str,
    store_conn=None,
) -> list[Impact]:
    """③-b 突合（design.md §6.1）。事業所文書が入るためローカル固定。

    要件 × 既存チェックリスト項目それぞれを1呼び出しで判定する。既存項目のどれも
    「要更新」にならなかった場合のみ、新規項目の要否を追加で1回問う。

    design.md §6.4「Impact.evidenceは必須項目。空の更新案は承認UIに出さない」を守るため、
    LLMが返した evidence_quote が通達原文（change.new_section.text）中に実在しない場合は
    根拠不成立とみなし、そのImpactを作らない（幻覚対策）。
    """
    impacts: list[Impact] = []
    existing_labels = "、".join(item.label for item in sitedoc.items)

    for req in requirements:
        change = changes_by_id[req.change_id]
        matched_existing = False

        for item in sitedoc.items:
            prompt = _MATCH_ITEM_PROMPT.format(
                item_label=item.label,
                requirement_text=req.text,
                path=change.new_section.path,
                notice_text=change.new_section.text,
            )
            result = adapter.complete("matching", prompt)
            if store_conn is not None:
                store.record_from_llm_result(store_conn, run_id, "matching", result)

            if result.get("timeout"):
                # design.md §6.6（v10）: 判定不能として記録し、他項目の判定は続行する
                # （evidenceが無いので承認UI上の更新案は作られないが、Impactとして
                # 「判定不能」がJSON出力・画面に残る＝黙って捨てない）。
                impacts.append(
                    _undecidable_impact(change, item.label, "ローカルLLM呼び出しがタイムアウトしました")
                )
                continue

            entry = _extract_json_object(result["text"], _MATCH_ITEM_EXPECTED_KEYS)
            if not entry or entry.get("verdict") != "要更新":
                continue  # 影響なし、または形式不正な応答は提案を作らない

            evidence = _verified_evidence(entry, change)
            if evidence is None:
                continue  # 通達原文中で裏取りできない引用はevidence不成立→提案を作らない

            matched_existing = True
            impacts.append(
                Impact(
                    id=f"impact-{change.id}-{item.label}",
                    change_id=change.id,
                    sitedoc_item_id=item.label,
                    verdict="要更新",
                    rationale=str(entry.get("rationale", "")),
                    confidence=float(entry.get("confidence") or 0.0),
                    evidence=evidence,
                )
            )

        # ledger N-7: 完全新設の条文は、既存項目が要更新になっていても新規項目を必ず問う。
        is_new_provision = change.kind == "追加"
        if matched_existing and not is_new_provision:
            continue

        prompt = _MATCH_NEW_ITEM_PROMPT.format(
            existing_labels=existing_labels,
            requirement_text=req.text,
            path=change.new_section.path,
            notice_text=change.new_section.text,
        )
        result = adapter.complete("matching", prompt)
        if store_conn is not None:
            store.record_from_llm_result(store_conn, run_id, "matching", result)

        entry = (
            None
            if result.get("timeout")
            else _extract_json_object(result["text"], _MATCH_NEW_ITEM_EXPECTED_KEYS)
        )

        if is_new_provision:
            impacts.append(_forced_new_item_impact(change, req.text, entry))
            continue

        if result.get("timeout"):
            impacts.append(_undecidable_impact(change, None, "ローカルLLM呼び出しがタイムアウトしました"))
            continue

        if not entry or not entry.get("needed"):
            continue

        evidence = _verified_evidence(entry, change)
        if evidence is None:
            continue  # 通達原文中で裏取りできない引用はevidence不成立→提案を作らない

        impacts.append(
            Impact(
                id=f"impact-{change.id}-new",
                change_id=change.id,
                sitedoc_item_id=None,
                verdict="要更新",
                rationale=str(entry.get("rationale", "")),
                confidence=float(entry.get("confidence") or 0.0),
                evidence=evidence,
            )
        )

    return impacts


# ---------------------------------------------------------------------------
# ③-b 突合（一括モード・design.md §7「投入単位と突合のバッチ化」v11）
#
# 単発モードは「変更 × チェックリスト項目」の直積で呼ばれ、8項目で125.2秒かかった
# （D2実測・ledger R-60）。一括モードは変更1件ぶんを1呼び出しにまとめる。
#
# ⚠️ 一括プロンプトは過去に一度失敗している（出力形式の崩れ・選択肢の書き写し）。
# その再発を検知できるようにするため、出力スキーマで全項目のID別判定を必須とし、
# 判定が返らなかった項目は「判定不能」として残す（承認条件1・ledger R-61）。
# ---------------------------------------------------------------------------

_MATCH_BATCH_PROMPT = """あなたは訪問看護事業所のチェックリスト担当者です。
以下の「通達原文」で新たに求められるようになった内容にもとづき、事業所チェックリストの
各項目について「この通達変更を受けて項目の文言を書き換える必要があるか」を判定してください。

【判定の基準】
- "要更新" は、この通達変更が要求する内容を書き加えないと、その項目が通達に適合しなくなる場合だけ。
- 項目の書き方が良いか悪いか、記録が実際にきちんと行われているかは、判定の対象ではない。
- 1つの通達変更が関係する項目はふつう少数で、大半の項目は "影響なし" になる。無理に "要更新" にしない。

【現場への要求事項】
{requirement_text}

【通達原文（{path}）】
{notice_text}

【判定対象のチェックリスト項目】
{items_block}

【出力形式】
次の形のJSONオブジェクトのみを出力すること。前置き・説明文・コードブロック記号は付けないこと。
judgements には項目番号 {id_list} を1つ残らず含めること（1つでも欠けたら不正な出力とみなす）。
各要素は item_id・verdict・rationale・confidence・evidence_quote の5つのキーを必ず持つこと。
item_id は上記の項目番号を文字列で入れること。項目のラベル文言は書き写さないこと。
verdict は "要更新" か "影響なし" のどちらか一方をそのまま使うこと（他の文字列を使わない・両方書かない）。
evidence_quote は、verdict が "要更新" の場合、【通達原文】の中の文字列を一字一句そのまま抜き出して入れること（原文に無い文章を作らない。要約しない）。"影響なし" の場合は空文字("")。
new_item は、どの既存項目にも該当しない場合にチェックリストへ新規項目を追加する必要があるかどうか。needed は true か false の真偽値。

出力例:
{{"judgements": [{{"item_id": "1", "verdict": "影響なし", "rationale": "判定理由をここに", "confidence": 0.8, "evidence_quote": ""}}, {{"item_id": "2", "verdict": "要更新", "rationale": "判定理由をここに", "confidence": 0.7, "evidence_quote": "通達原文中の一致する部分文字列"}}], "new_item": {{"needed": false, "rationale": "判定理由をここに", "confidence": 0.6, "evidence_quote": ""}}}}
"""


def _match_requirements_batched(
    requirements: list[Requirement],
    changes_by_id: dict[str, Change],
    sitedoc: SiteDoc,
    adapter: LLMAdapter,
    *,
    run_id: str,
    store_conn=None,
) -> list[Impact]:
    """③-b 突合（一括モード）。要件1件につきLLM呼び出し1回で全項目を判定する。"""
    impacts: list[Impact] = []
    id_list = "、".join(str(i) for i in range(1, len(sitedoc.items) + 1))

    for req in requirements:
        change = changes_by_id[req.change_id]
        prompt = _MATCH_BATCH_PROMPT.format(
            requirement_text=req.text,
            path=change.new_section.path,
            notice_text=change.new_section.text,
            items_block=_format_items_block(sitedoc.items),
            id_list=id_list,
        )
        result = adapter.complete("matching", prompt)
        if store_conn is not None:
            store.record_from_llm_result(store_conn, run_id, "matching", result)

        if result.get("timeout"):
            # 一括呼び出しのtimeoutは全項目が巻き添えになる（単発モードには無い失敗の形）。
            # 黙って消さず、全項目を判定不能として残す。
            impacts.extend(
                _undecidable_impact(change, item.label, "一括判定の呼び出しがタイムアウトしました")
                for item in sitedoc.items
            )
            # 新規項目の提案は条文の種別だけで決まる（LLMの応答を必要としない）ので、
            # timeoutでも作れる。ここで作らないと、回線断のデモで落ちが消える。
            if change.kind == "追加":
                impacts.append(_forced_new_item_impact(change, req.text, None))
            continue

        payload = _extract_json_payload(result["text"])
        entries_by_id: dict[str, dict] = {}
        for entry in _payload_list(payload, "judgements", "judgments"):
            if isinstance(entry, dict) and entry.get("item_id") is not None:
                entries_by_id[str(entry["item_id"]).strip()] = entry

        matched_existing = False
        for index, item in enumerate(sitedoc.items, start=1):
            entry = entries_by_id.get(str(index))
            if entry is None:
                # 承認条件1: 判定が返らなかった項目は「判定不能」。黙って影響なし扱いにしない
                # （一括プロンプトの判定漏れをここで検知できるようにするため）。
                impacts.append(
                    _undecidable_impact(change, item.label, "一括判定でこの項目の判定が返りませんでした")
                )
                continue
            if entry.get("verdict") != "要更新":
                continue
            evidence = _verified_evidence(entry, change)
            if evidence is None:
                continue  # 通達原文中で裏取りできない引用はevidence不成立→提案を作らない
            matched_existing = True
            impacts.append(
                Impact(
                    id=f"impact-{change.id}-{item.label}",
                    change_id=change.id,
                    sitedoc_item_id=item.label,
                    verdict="要更新",
                    rationale=str(entry.get("rationale", "")),
                    confidence=float(entry.get("confidence") or 0.0),
                    evidence=evidence,
                )
            )

        new_item = (payload or {}).get("new_item")
        new_item = new_item if isinstance(new_item, dict) else None

        # ledger N-7: 完全新設の条文は、LLMのneeded判定に関わらず新規項目の提案を作る。
        if change.kind == "追加":
            impacts.append(_forced_new_item_impact(change, req.text, new_item))
            continue

        if matched_existing:
            continue  # 既存項目で対応済みなら新規項目は要らない（単発モードと同じ規則）

        if new_item is None or not new_item.get("needed"):
            continue
        evidence = _verified_evidence(new_item, change)
        if evidence is None:
            continue
        impacts.append(
            Impact(
                id=f"impact-{change.id}-new",
                change_id=change.id,
                sitedoc_item_id=None,
                verdict="要更新",
                rationale=str(new_item.get("rationale", "")),
                confidence=float(new_item.get("confidence") or 0.0),
                evidence=evidence,
            )
        )

    return impacts


# ---------------------------------------------------------------------------
# ④更新案生成（ローカル固定・事業所文書を含む）
# ---------------------------------------------------------------------------

_DRAFT_PROMPT = """あなたは訪問看護事業所の現場文書担当者です。以下の内容をもとに、
チェックリストへの更新案（更新後の項目文言）を作成してください。

【対象項目の現状】
{before}

【通達で新たに求められる内容】
{rationale}

【根拠（通達原文からの引用）】
{quote}

出力は「更新後のチェックリスト項目文言」のみを1〜2文で。前置き・記号は付けないこと。
"""


def draft_proposals(
    impacts: list[Impact], sitedoc: SiteDoc, adapter: LLMAdapter, *, run_id: str, store_conn=None
) -> list[Proposal]:
    """④更新案生成（design.md §6.1）。事業所文書が入るためローカル固定。"""
    items_by_label = {item.label: item for item in sitedoc.items}
    proposals: list[Proposal] = []

    for impact in impacts:
        if impact.verdict != "要更新" or not impact.evidence:
            continue  # design.md §6.4: evidence必須。空なら提案を作らない

        if impact.sitedoc_item_id is None:
            before = ""
            before_display = "(現在この項目はチェックリストに存在しない)"
        else:
            before = items_by_label[impact.sitedoc_item_id].label
            before_display = before

        quote = impact.evidence[0].quote
        prompt = _DRAFT_PROMPT.format(before=before_display, rationale=impact.rationale, quote=quote)
        result = adapter.complete("draft", prompt)
        if store_conn is not None:
            store.record_from_llm_result(store_conn, run_id, "draft", result)

        # design.md §6.6（v10）: draftはD2実測で唯一timeoutした工程。例外を上げず
        # 「判定不能」として更新案を残し、承認UI・JSON出力から見えるようにする。
        after_text = "判定不能" if result.get("timeout") else result["text"].strip()

        proposals.append(
            Proposal(
                id=f"proposal-{impact.id}",
                impact_id=impact.id,
                before=before,
                after=after_text,
            )
        )
    return proposals


_DRAFT_BATCH_PROMPT = """あなたは訪問看護事業所の現場文書担当者です。以下の通達変更を受けて、
チェックリストの各対象項目について「更新後の項目文言」を作成してください。

【通達で新たに求められる内容と根拠】
{targets_block}

【出力形式】
次の形のJSONオブジェクトのみを出力すること。前置き・説明文・コードブロック記号は付けないこと。
drafts には項目番号 {id_list} を1つ残らず含めること（1つでも欠けたら不正な出力とみなす）。
after には更新後のチェックリスト項目文言のみを1〜2文で入れること。

出力例:
{{"drafts": [{{"item_id": "1", "after": "更新後の項目文言をここに"}}]}}
"""


def _draft_proposals_batched(
    impacts: list[Impact], sitedoc: SiteDoc, adapter: LLMAdapter, *, run_id: str, store_conn=None
) -> list[Proposal]:
    """④更新案生成（一括モード）。変更1件ぶんの要更新項目をまとめて1回で生成する。

    変更（change_id）ごとにまとめる。1回のプロンプトに複数の通達変更を混ぜると、
    別の変更の根拠を引いた文言が出やすくなるため。
    """
    items_by_label = {item.label: item for item in sitedoc.items}
    eligible = [i for i in impacts if i.verdict == "要更新" and i.evidence]
    proposals: list[Proposal] = []

    change_ids: list[str] = []
    for impact in eligible:
        if impact.change_id not in change_ids:
            change_ids.append(impact.change_id)

    for change_id in change_ids:
        group = [i for i in eligible if i.change_id == change_id]
        befores = [
            "" if i.sitedoc_item_id is None else items_by_label[i.sitedoc_item_id].label for i in group
        ]
        targets_block = "\n\n".join(
            f"{index}. 対象項目の現状: {before or '(現在この項目はチェックリストに存在しない)'}\n"
            f"   求められる内容: {impact.rationale}\n"
            f"   根拠（通達原文からの引用）: {impact.evidence[0].quote}"
            for index, (impact, before) in enumerate(zip(group, befores), start=1)
        )
        prompt = _DRAFT_BATCH_PROMPT.format(
            targets_block=targets_block,
            id_list="、".join(str(i) for i in range(1, len(group) + 1)),
        )
        result = adapter.complete("draft", prompt)
        if store_conn is not None:
            store.record_from_llm_result(store_conn, run_id, "draft", result)

        drafts_by_id: dict[str, str] = {}
        if not result.get("timeout"):
            payload = _extract_json_payload(result["text"])
            for entry in _payload_list(payload, "drafts"):
                if isinstance(entry, dict) and entry.get("item_id") is not None:
                    drafts_by_id[str(entry["item_id"]).strip()] = str(entry.get("after", "")).strip()

        for index, (impact, before) in enumerate(zip(group, befores), start=1):
            # design.md §6.6（v10）・承認条件1: timeoutも判定漏れも「判定不能」として残す。
            after_text = drafts_by_id.get(str(index)) or "判定不能"
            proposals.append(
                Proposal(id=f"proposal-{impact.id}", impact_id=impact.id, before=before, after=after_text)
            )

    return proposals


# ---------------------------------------------------------------------------
# 4工程の通し実行
# ---------------------------------------------------------------------------


def run(
    *,
    old_sections: dict[str, Section | None],
    new_sections: dict[str, Section | None],
    sitedoc: SiteDoc,
    adapter: LLMAdapter,
    run_id: str,
    store_conn=None,
    labels: set[str] | None = None,
    batch: bool | None = None,
) -> dict[str, list]:
    """②差分抽出→③-a要件抽出→③-b突合→④更新案生成を順に実行する。

    labels: design.md §2「投入単位」（v10）。指定した場合、その label（allowlistの
        label集合の部分集合）だけを処理する1項目実行になる。Noneなら従来どおり全項目
        （一括実行）。D4の承認UIから1項目だけ流す用途にも使う。
    batch: design.md §7「投入単位と突合のバッチ化」（v11）。突合・更新案生成を
        1呼び出しにまとめるか。None なら models.toml の batch_matching に従う。
        False は単発モード（デバッグ用・承認条件3）。
    """
    if batch is None:
        batch = getattr(adapter, "batch_matching", True)
    if labels is not None:
        old_sections = {label: sec for label, sec in old_sections.items() if label in labels}
        new_sections = {label: sec for label, sec in new_sections.items() if label in labels}
    changes = build_changes(old_sections, new_sections)
    extract_change_summaries(changes, adapter, run_id=run_id, store_conn=store_conn)
    requirements = extract_requirements(changes, adapter, run_id=run_id, store_conn=store_conn)
    changes_by_id = {c.id: c for c in changes}
    match_fn = _match_requirements_batched if batch else match_requirements
    draft_fn = _draft_proposals_batched if batch else draft_proposals
    impacts = match_fn(
        requirements, changes_by_id, sitedoc, adapter, run_id=run_id, store_conn=store_conn
    )
    proposals = draft_fn(impacts, sitedoc, adapter, run_id=run_id, store_conn=store_conn)
    return {
        "changes": changes,
        "requirements": requirements,
        "impacts": impacts,
        "proposals": proposals,
    }
