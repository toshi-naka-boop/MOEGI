"""仮名化後の独立再検査（design/moegi/privacy.md §8）。

ローカルLLMの判断からは完全に独立した、決定論的な検査。LLMが「消したつもり」で
消せていない場合をここで捕まえる。ここで何か残っていたら passed にしない
（fail-closed）。

前段（rules.detect）と同じ検出器を使うが、目的が違う:
    前段 = 消すために見つける
    再検査 = **消えていないことを証明するために見つける**
同じ関数を使うので、片方だけ更新されて穴が開くことがない。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from moegi.privacy import dictionary as dict_module
from moegi.privacy import rules

#: 再検査で1件でも残っていたら不合格にする型（§8）。
FORBIDDEN_TYPES = frozenset(
    {
        "phone",
        "email",
        "insurance_number",
        "medical_card_number",
        "api_key",
        "jwt",
        "url_credential",
        "address",
        "patient_id",
        "ip_address",
        "postal_code",
        "birthdate",
    }
)


@dataclass
class RecheckResult:
    passed: bool
    #: 残っていた型 → 件数（本文・値そのものは持たない。ログ・監査へそのまま出せる形）
    residual_counts: dict[str, int] = field(default_factory=dict)
    #: 辞書に載っている氏名・施設名が残っていた場合の型名（値は含めない）
    residual_dictionary_hits: int = 0

    @property
    def reason_codes(self) -> list[str]:
        codes = [f"residual_{t}" for t in sorted(self.residual_counts)]
        if self.residual_dictionary_hits:
            codes.append("residual_registered_name")
        return codes


def run(
    sanitized_text: str,
    *,
    dictionary: dict_module.Dictionary | None,
    today: tuple[int, int, int],
) -> RecheckResult:
    """仮名化済みテキストに禁止対象が残っていないかを検査する。

    dictionary が None（辞書の取得に失敗した場合）でも検査自体は行う。
    辞書由来の検査ができないことは呼び出し側（gate.py）が別途 fail-closed の
    理由として扱う——ここで静かに「辞書チェックはスキップして合格」にしない。
    """
    counts: dict[str, int] = {}
    for detection in rules.detect(sanitized_text, today=today):
        if detection.type in FORBIDDEN_TYPES:
            counts[detection.type] = counts.get(detection.type, 0) + 1

    dictionary_hits = 0
    if dictionary is not None:
        dictionary_hits = len(dict_module.contains_registered_name(sanitized_text, dictionary))

    return RecheckResult(
        passed=not counts and dictionary_hits == 0,
        residual_counts=counts,
        residual_dictionary_hits=dictionary_hits,
    )
