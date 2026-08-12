"""ローカル辞書による患者・家族・職員・医師・施設の検出（design/moegi/privacy.md §5.2）。

⚠️ 辞書の内容は外部へ送信しない。ローカルLLMのプロンプトにも入れない。
   照合はすべてこのモジュール（コード側の完全一致）で行い、LLMには
   「辞書で仮名化し終わったテキスト」だけを渡す。

一般名詞との衝突対策:
    フルネーム・エイリアスは完全一致で拾う。姓だけの表記（「森」「山田」）は
    敬称（さん/様/氏/先生/ちゃん/くん）を伴う場合だけ拾う。「森の中を散歩」の
    「森」は職員名として検出しない——訪問看護の記録では自然な語が姓と衝突するため。

同姓同名対策:
    同じ表記が複数の人物に対応する場合、どちらか一意に決められない。
    検出はするが review_required=True を立て、人による確認を必須にする。
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from moegi.privacy.rules import ACTION_PSEUDONYM, ACTION_TAG, Detection, TAG_ADDRESS, TAG_PHONE

_HONORIFICS = ("さん", "様", "さま", "氏", "先生", "ちゃん", "くん", "君")

#: 仮名のプレフィックス（§6 仮名化ポリシー）
PREFIXES = {
    "patient": "PATIENT",
    "family": "FAMILY",
    "staff": "NURSE",
    "doctor": "DOCTOR",
    "facility": "FACILITY",
}


@dataclass(frozen=True)
class Entry:
    """辞書の1件。surfaces は「このテキストが出たらこの人物」という表記の集合。"""

    category: str  # "patient" | "family" | "staff" | "doctor" | "facility"
    canonical: str  # 対応表に載る実名（例: "山田 太郎"）
    surfaces: tuple[str, ...]
    surname: str | None = None
    relation: str | None = None  # 家族の続柄（"長女" 等）。あればこれを仮名に使う


@dataclass
class Dictionary:
    """ロード済みのローカル辞書。"""

    entries: list[Entry] = field(default_factory=list)
    addresses: tuple[str, ...] = ()
    phones: tuple[str, ...] = ()

    #: surface（表記）→ 一致する Entry のリスト。2件以上なら同姓同名。
    _by_surface: dict[str, list[Entry]] = field(default_factory=dict)
    #: 姓 → 一致する Entry のリスト（敬称つきでのみ使う）。
    _by_surname: dict[str, list[Entry]] = field(default_factory=dict)

    def build_index(self) -> None:
        self._by_surface = {}
        self._by_surname = {}
        for entry in self.entries:
            for surface in entry.surfaces:
                self._by_surface.setdefault(surface, []).append(entry)
            if entry.surname:
                self._by_surname.setdefault(entry.surname, []).append(entry)


def _surfaces(raw: dict, *keys: str) -> tuple[str, ...]:
    """辞書エントリから表記の集合を組み立てる（空白を除いた形も含める）。"""
    values: list[str] = []
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value:
            values.append(value)
        elif isinstance(value, list):
            values.extend(str(v) for v in value if v)
    expanded: list[str] = []
    for value in values:
        expanded.append(value)
        compact = value.replace(" ", "").replace("　", "")
        if compact != value:
            expanded.append(compact)
    # 長い表記から先に照合したいので、ここで長さ降順に揃えておく
    return tuple(dict.fromkeys(sorted(expanded, key=len, reverse=True)))


def load_dictionary(path: str | Path) -> Dictionary:
    """辞書TOMLを読み込む。読み込み失敗は呼び出し側（gate.py）が fail-closed で扱う。"""
    with open(path, "rb") as f:
        data = tomllib.load(f)

    entries: list[Entry] = []
    for raw in data.get("patients", []):
        entries.append(
            Entry(
                category="patient",
                canonical=str(raw.get("full_name", "")),
                surfaces=_surfaces(raw, "full_name", "aliases", "patient_ids"),
                surname=raw.get("surname"),
            )
        )
    for raw in data.get("families", []):
        entries.append(
            Entry(
                category="family",
                canonical=str(raw.get("full_name", "")),
                surfaces=_surfaces(raw, "full_name", "aliases"),
                surname=raw.get("surname"),
                relation=raw.get("relation"),
            )
        )
    for raw in data.get("staff", []):
        entries.append(
            Entry(
                category="staff",
                canonical=str(raw.get("full_name", "")),
                surfaces=_surfaces(raw, "full_name", "aliases"),
                surname=raw.get("surname"),
            )
        )
    for raw in data.get("doctors", []):
        entries.append(
            Entry(
                category="doctor",
                canonical=str(raw.get("full_name", "")),
                surfaces=_surfaces(raw, "full_name", "aliases"),
                surname=raw.get("surname"),
            )
        )
    for raw in data.get("facilities", []):
        entries.append(
            Entry(
                category="facility",
                canonical=str(raw.get("name", "")),
                surfaces=_surfaces(raw, "name", "aliases"),
            )
        )

    addresses: list[str] = []
    for raw in data.get("addresses", []):
        addresses.extend(_surfaces(raw, "value", "aliases"))
    phones: list[str] = []
    for raw in data.get("phones", []):
        phones.extend(_surfaces(raw, "value", "aliases"))

    dictionary = Dictionary(
        entries=entries,
        addresses=tuple(sorted(set(addresses), key=len, reverse=True)),
        phones=tuple(sorted(set(phones), key=len, reverse=True)),
    )
    dictionary.build_index()
    return dictionary


class PseudonymAllocator:
    """実名 → 仮名（PATIENT_001 等）の割り当て。1リクエスト分の状態を持つ。

    同じ人物には同じ仮名を割り当て、出現順に採番する（決定論的＝再現できる）。
    家族は続柄が分かっていればそれを仮名に使う（§6「長女・配偶者等へ置換」）。
    """

    def __init__(self) -> None:
        self._by_canonical: dict[str, str] = {}
        self._counters: dict[str, int] = {}

    def allocate(self, entry: Entry) -> str:
        key = f"{entry.category}:{entry.canonical}"
        if key in self._by_canonical:
            return self._by_canonical[key]
        if entry.category == "family" and entry.relation:
            pseudonym = entry.relation
        else:
            prefix = PREFIXES.get(entry.category, "PERSON")
            self._counters[prefix] = self._counters.get(prefix, 0) + 1
            pseudonym = f"{prefix}_{self._counters[prefix]:03d}"
        self._by_canonical[key] = pseudonym
        return pseudonym

    @property
    def mapping(self) -> dict[str, str]:
        """仮名 → 実名 の対応表（vault に保存する中身）。

        続柄仮名（「長女」等）は複数の実名に割り当たり得るので、後勝ちで潰さないよう
        カテゴリ込みのキーから実名を引き直して組み立てる。
        """
        result: dict[str, str] = {}
        for key, pseudonym in self._by_canonical.items():
            canonical = key.split(":", 1)[1]
            if pseudonym in result and result[pseudonym] != canonical:
                # 同じ続柄が2人に割り当たった場合は両方を残す（人が見て判断できるように）
                result[pseudonym] = f"{result[pseudonym]} / {canonical}"
            else:
                result[pseudonym] = canonical
        return result


def detect(text: str, dictionary: Dictionary, allocator: PseudonymAllocator) -> list[Detection]:
    """辞書に載っている人物・施設・住所・電話番号を検出する。

    表記が長いものから順に探し、既に確定した範囲とは重ならないものだけを採る
    （「山田 太郎」を拾ったあとに「山田」を二重に拾わない）。
    """
    detections: list[Detection] = []
    claimed: list[tuple[int, int]] = []

    def _overlaps(start: int, end: int) -> bool:
        return any(start < c_end and c_start < end for c_start, c_end in claimed)

    def _claim(start: int, end: int) -> None:
        claimed.append((start, end))

    # 1) フルネーム・エイリアス・施設名（完全一致・長い順）
    surfaces = sorted(dictionary._by_surface.keys(), key=len, reverse=True)
    for surface in surfaces:
        matched_entries = dictionary._by_surface[surface]
        for match in re.finditer(re.escape(surface), text):
            if _overlaps(match.start(), match.end()):
                continue
            entry = matched_entries[0]
            ambiguous = len({e.canonical for e in matched_entries}) > 1 or len(matched_entries) > 1
            detections.append(
                Detection(
                    type="facility" if entry.category == "facility" else "person_name",
                    start=match.start(),
                    end=match.end(),
                    value=match.group(0),
                    replacement=allocator.allocate(entry),
                    action=ACTION_PSEUDONYM,
                    source="dictionary",
                    # 同姓同名（同じ表記に複数人が対応）は一意に決められない＝人の確認が要る
                    review_required=ambiguous,
                )
            )
            _claim(match.start(), match.end())

    # 2) 姓＋敬称（一般名詞との衝突を避けるため敬称必須）
    for surname, matched_entries in dictionary._by_surname.items():
        for honorific in _HONORIFICS:
            needle = f"{surname}{honorific}"
            for match in re.finditer(re.escape(needle), text):
                if _overlaps(match.start(), match.end()):
                    continue
                entry = matched_entries[0]
                ambiguous = len({e.canonical for e in matched_entries}) > 1
                detections.append(
                    Detection(
                        type="person_name",
                        start=match.start(),
                        # 敬称は残す（「PATIENT_001さん」の方が記録として自然）
                        end=match.start() + len(surname),
                        value=surname,
                        replacement=allocator.allocate(entry),
                        action=ACTION_PSEUDONYM,
                        source="dictionary",
                        review_required=ambiguous,
                    )
                )
                _claim(match.start(), match.end())

    # 3) 登録済み住所・電話番号（ルールベースで拾えない表記も確実に落とす）
    for address in dictionary.addresses:
        for match in re.finditer(re.escape(address), text):
            if _overlaps(match.start(), match.end()):
                continue
            detections.append(
                Detection(
                    type="address",
                    start=match.start(),
                    end=match.end(),
                    value=match.group(0),
                    replacement=TAG_ADDRESS,
                    action=ACTION_TAG,
                    source="dictionary",
                )
            )
            _claim(match.start(), match.end())

    for phone in dictionary.phones:
        for match in re.finditer(re.escape(phone), text):
            if _overlaps(match.start(), match.end()):
                continue
            detections.append(
                Detection(
                    type="phone",
                    start=match.start(),
                    end=match.end(),
                    value=match.group(0),
                    replacement=TAG_PHONE,
                    action=ACTION_TAG,
                    source="dictionary",
                )
            )
            _claim(match.start(), match.end())

    return sorted(detections, key=lambda d: d.start)


def contains_registered_name(text: str, dictionary: Dictionary) -> list[str]:
    """再検査用（§8）: 登録済みの氏名・施設名が残っていないかを調べる。

    再検査では敬称の有無に関わらず、辞書に載っている表記が1つでも残っていたら不合格にする
    （検出側より厳しくてよい。ここで拾いすぎても「人が確認する」に倒れるだけで安全側）。
    """
    found: list[str] = []
    for surface in dictionary._by_surface:
        if surface and surface in text:
            found.append(surface)
    return sorted(set(found))
