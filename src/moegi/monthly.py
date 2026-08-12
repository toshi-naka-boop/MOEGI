"""月報（1ヶ月分の日報の集約）。

**LLMを一切使わない。** 集計はすべて決定論的で、同じ日報からは常に同じ月報が出る
（ユーザー決定・2026-08-12。ledger R-84 で R-15「月報はスコープ外」を条件付きで撤回した）。

理由は2つ:
  1. 月報は加算のエビデンスにも使われ得るため、数字が実行のたびに揺れてはいけない
  2. LLM要約を入れると、月報が要配慮個人情報の**集積**を1回のプロンプトに載せることになる。
     ローカル固定でも露出面は増えるので、必要になるまで作らない

集計単位は「安定仮名（vault.stable_pseudonym）」。日報DBには利用者コードそのものが
入っていないので、このモジュールも利用者コードを受け取らない——集計対象の解決は
呼び出し側（承認UI）が行う。

バイタルの抽出について:
    バイタルは日報の自由記述欄（`items[].value`）とSOAPのO欄に文字列で入っている。
    ここから数値を取り出すのは best-effort で、読めなかった訪問は `unparsed_visits` に
    数える。**「読めなかった」を0や欠測として黙って混ぜない**——月報の数字が実態より
    良くも悪くも見えるのを避けるため。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: バイタルらしき記述を探すチェックリスト項目。不変ID（ledger R-88）で当てるのが本筋だが、
#: 不変ID導入前に保存された日報にはキーが無いので、ラベルの部分一致もフォールバックとして残す。
_VITAL_ITEM_KEY = "vital-signs"
_VITAL_ITEM_HINT = "バイタル"

# すべて上限つき量化子・ネストなし（rules.py と同じ ReDoS 方針）。
_BLOOD_PRESSURE_RE = re.compile(r"(?<![0-9])([0-9]{2,3})\s?[/／]\s?([0-9]{2,3})(?![0-9])")
_TEMPERATURE_RE = re.compile(r"(?:体温|BT|KT)?\s?[:：]?\s?(3[0-9](?:\.[0-9])?|4[0-2](?:\.[0-9])?)\s?(?:度|℃)")
_PULSE_RE = re.compile(r"(?:脈拍|脈|HR)\s?[:：]?\s?([0-9]{2,3})(?![0-9])")
_SPO2_RE = re.compile(r"(?:SpO2|SPO2|spo2|サチュレーション)\s?[:：]?\s?([0-9]{2,3})\s?%?")


@dataclass
class VitalPoint:
    """1訪問ぶんのバイタル。読み取れなかった項目は None のまま残す。"""

    visited_on: str
    systolic: int | None = None
    diastolic: int | None = None
    temperature: float | None = None
    pulse: int | None = None
    spo2: int | None = None

    @property
    def is_empty(self) -> bool:
        return all(
            v is None for v in (self.systolic, self.diastolic, self.temperature, self.pulse, self.spo2)
        )


@dataclass
class ItemFillRate:
    """チェックリスト項目1つぶんの、その月の記入状況。

    ledger R-88: 集計のキーは `key`（不変ID）。`label` は表示用の文言で、
    月の途中で更新案が承認されると変わり得る（その場合は最新のラベルを表示する）。
    """

    label: str
    filled: int
    total: int
    key: str = ""

    @property
    def rate(self) -> float:
        return self.filled / self.total if self.total else 0.0


@dataclass
class MonthlyReport:
    """design/moegi/privacy.md の仮名化方針のうえに成り立つ月次サマリ。

    実名も利用者コードも持たない（subject_pseudonym だけ）。
    """

    subject_pseudonym: str
    year: int
    month: int
    visit_count: int = 0
    visit_days: int = 0
    procedure_counts: dict[str, int] = field(default_factory=dict)
    item_fill_rates: list[ItemFillRate] = field(default_factory=list)
    evaluation_filled: int = 0
    evaluation_total: int = 0
    vitals: list[VitalPoint] = field(default_factory=list)
    unparsed_vital_visits: int = 0
    #: Privacy Gate が passed 以外だった日報の件数（observe/shadow運用で混ざり得る）。
    non_passed_records: int = 0
    policy_versions: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return self.visit_count == 0


def _vital_source_text(record: dict) -> str:
    """バイタルが書かれていそうな箇所だけを連結する。

    日報の全文を舐めると「訪問時間 10:30-11:00」の数字を血圧に読み違える。
    バイタル項目とSOAPのO欄（客観的情報）に限定する。
    """
    parts: list[str] = []
    for item in record.get("items") or []:
        key = str(item.get("checklist_item_key") or "")
        label = str(item.get("checklist_item_id", ""))
        if key == _VITAL_ITEM_KEY or (not key and _VITAL_ITEM_HINT in label):
            parts.append(str(item.get("value") or ""))
    soap = record.get("soap") or {}
    parts.append(str(soap.get("objective") or ""))
    return " ".join(p for p in parts if p)


def _extract_vitals(visited_on: str, text: str) -> VitalPoint:
    point = VitalPoint(visited_on=visited_on)
    if not text:
        return point

    bp = _BLOOD_PRESSURE_RE.search(text)
    if bp:
        systolic, diastolic = int(bp.group(1)), int(bp.group(2))
        # 収縮期 > 拡張期 が成り立たない組は血圧として採らない（時刻等の誤検出よけ）
        if 60 <= systolic <= 260 and 30 <= diastolic <= 160 and systolic > diastolic:
            point.systolic, point.diastolic = systolic, diastolic

    temperature = _TEMPERATURE_RE.search(text)
    if temperature:
        point.temperature = float(temperature.group(1))

    pulse = _PULSE_RE.search(text)
    if pulse and 20 <= int(pulse.group(1)) <= 220:
        point.pulse = int(pulse.group(1))

    spo2 = _SPO2_RE.search(text)
    if spo2 and 50 <= int(spo2.group(1)) <= 100:
        point.spo2 = int(spo2.group(1))

    return point


def build(records: list[dict], *, subject_pseudonym: str, year: int, month: int) -> MonthlyReport:
    """日報レコード（store.list_nippou_records_for_month の戻り値）から月報を組み立てる。

    LLMを呼ばないので、同じ入力からは常に同じ月報が出る。
    """
    report = MonthlyReport(subject_pseudonym=subject_pseudonym, year=year, month=month)
    if not records:
        return report

    report.visit_count = len(records)
    report.visit_days = len({r["visited_on"] for r in records})
    report.non_passed_records = sum(1 for r in records if r.get("gate_status") != "passed")
    report.policy_versions = sorted({str(r.get("policy_version") or "") for r in records if r.get("policy_version")})

    for record in records:
        for procedure in record.get("procedures") or []:
            report.procedure_counts[procedure] = report.procedure_counts.get(procedure, 0) + 1

    # チェックリスト項目の記入率。項目の並びは最初に現れた順を保つ（sitedocの順序に近い）。
    #
    # ledger R-88: 集計のキーは不変ID（checklist_item_key）。ラベルで束ねると、
    # 更新案が承認されて文言が変わった月に**同じ項目が別項目として二重にカウントされる**。
    # 表示に使うラベルは、その項目の最新（＝最後に記録された）ものを採る。
    totals: dict[str, int] = {}
    filled: dict[str, int] = {}
    labels: dict[str, str] = {}
    for record in records:
        for item in record.get("items") or []:
            label = str(item.get("checklist_item_id", ""))
            key = str(item.get("checklist_item_key") or "") or label
            if not key:
                continue
            totals[key] = totals.get(key, 0) + 1
            labels[key] = label or key
            if item.get("filled"):
                filled[key] = filled.get(key, 0) + 1
    report.item_fill_rates = [
        ItemFillRate(label=labels.get(key, key), filled=filled.get(key, 0), total=total, key=key)
        for key, total in totals.items()
    ]

    for record in records:
        evaluation = record.get("evaluation")
        if evaluation is None:
            continue
        report.evaluation_total += 1
        if evaluation.get("filled"):
            report.evaluation_filled += 1

    for record in records:
        point = _extract_vitals(record["visited_on"], _vital_source_text(record))
        if point.is_empty:
            report.unparsed_vital_visits += 1
        else:
            report.vitals.append(point)

    return report
