"""ルールベースの直接識別子・秘密情報検出（design/moegi/privacy.md §5.1）。

決定論的であることがこのモジュールの存在理由。ローカルLLMの判断がどうであれ、
ここで拾えるものは必ず拾う——そして §8 の再検査でも同じ関数を使うので、
「LLMが消したつもりで消せていない」を独立に検知できる。

ReDoS対策:
    - 呼び出し側（gate.py）が max_input_chars で入力長を先に打ち切る
    - すべての量化子に上限を付ける（`+` / `*` を使わない）
    - 量化子のネスト（(a+)+ 形）を作らない
    - 曖昧な判定（電話番号の桁数・住所らしさ）は正規表現に押し込まず、
      マッチ後のPythonコード側で確定させる
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 仮名化の「処理」種別（design/moegi/privacy.md §6 仮名化ポリシー）
# ---------------------------------------------------------------------------

#: 型付きタグへ置換して先へ進める
ACTION_TAG = "tag"
#: 地域レベルへ一般化して先へ進める
ACTION_GENERALIZE = "generalize"
#: 連番の仮名へ置換して先へ進める（対応表に載る）
ACTION_PSEUDONYM = "pseudonym"
#: 検出したらPrivacy Gateを blocked にする（それ自体を記録に残してはいけない情報）
ACTION_BLOCK = "block"


@dataclass(frozen=True)
class Detection:
    """検出した1件。start/end は検出時点のテキストのインデックス。"""

    type: str
    start: int
    end: int
    value: str
    replacement: str
    action: str
    source: str = "rule"  # "rule" | "dictionary" | "llm"
    review_required: bool = False

    @property
    def length(self) -> int:
        return self.end - self.start


# ---------------------------------------------------------------------------
# 正規表現（すべて上限つき量化子・ネストなし）
# ---------------------------------------------------------------------------

_PREFECTURES = (
    "北海道|青森県|岩手県|宮城県|秋田県|山形県|福島県|茨城県|栃木県|群馬県|埼玉県|千葉県|"
    "東京都|神奈川県|新潟県|富山県|石川県|福井県|山梨県|長野県|岐阜県|静岡県|愛知県|三重県|"
    "滋賀県|京都府|大阪府|兵庫県|奈良県|和歌山県|鳥取県|島根県|岡山県|広島県|山口県|徳島県|"
    "香川県|愛媛県|高知県|福岡県|佐賀県|長崎県|熊本県|大分県|宮崎県|鹿児島県|沖縄県"
)

# 住所は「市区町村 + 番地」の骨格だけを正規表現で取り、住所らしさの最終判定はコード側で行う。
# rest からひらがなを除いてあるので「新宿区西新宿2-8-1に訪問」の「に」で自然に止まる。
_ADDRESS_RE = re.compile(
    r"(?P<pref>" + _PREFECTURES + r")?"
    r"(?P<city>[一-龥ァ-ヶー]{1,10}?[市区町村郡])"
    r"(?P<rest>[一-龥ァ-ヶA-Za-z0-9０-９丁目番地号\-−ー－・]{0,30})"
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,255}\.[A-Za-z]{2,24}")

# 電話番号は「0始まりの数字と区切り」だけを拾い、総桁数（10 or 11）でコード側が確定させる。
_PHONE_RE = re.compile(r"(?<![0-9０-９])0[0-9０-９]{1,4}[\-−ー－\(\) ]{0,2}[0-9０-９]{1,4}[\-−ー－\(\) ]{0,2}[0-9０-９]{3,4}(?![0-9０-９])")

_POSTAL_RE = re.compile(r"〒? ?(?<![0-9０-９])[0-9０-９]{3}[\-−ー－][0-9０-９]{4}(?![0-9０-９])")

_BIRTHDATE_WESTERN_RE = re.compile(
    r"(?<![0-9])(?P<year>1[89][0-9]{2}|20[0-9]{2})[年/\-.]"
    r"(?P<month>0?[1-9]|1[0-2])[月/\-.]"
    r"(?P<day>0?[1-9]|[12][0-9]|3[01])日?"
)
_ERA_OFFSETS = {"明治": 1867, "大正": 1911, "昭和": 1925, "平成": 1988, "令和": 2018}
_BIRTHDATE_ERA_RE = re.compile(
    r"(?P<era>明治|大正|昭和|平成|令和)(?P<year>元|[0-9]{1,2})年"
    r"(?P<month>[0-9]{1,2})月(?P<day>[0-9]{1,2})日"
)

# 番号系はラベルつきで拾う。裸の数字列を番号扱いすると、バイタル値や訪問時間まで
# 巻き込んで臨床情報を壊す（§6「病名・症状・バイタルは原則保持」に反する）。
_INSURANCE_RE = re.compile(
    r"(?:被保険者証番号|被保険者番号|保険証番号|保険者番号|記号番号)\s?[：:＝=]?\s?[0-9０-９A-Za-z\-−]{4,24}"
)
_MEDICAL_CARD_RE = re.compile(r"(?:診察券番号|診察券|カルテ番号|カルテNo\.?)\s?[：:＝=]?\s?[0-9０-９A-Za-z\-−]{4,24}")
_PATIENT_ID_RE = re.compile(
    r"(?:利用者ID|利用者番号|患者ID|患者番号|利用者コード)\s?[：:＝=]?\s?[0-9０-９A-Za-z\-−]{2,24}"
)

_IPV4_RE = re.compile(r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])")

# URL内の認証情報は scheme://user:pass@host の形。@ より前に : を1つ持つものだけ。
_URL_CREDENTIAL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]{1,20}://[^\s:/@]{1,64}:[^\s:/@]{1,64}@[^\s]{1,128}")

_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\-]{8,512}\.[A-Za-z0-9_\-]{8,2048}\.[A-Za-z0-9_\-]{8,512}")

_API_KEY_RES = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,128}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,255}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,255}"),
    re.compile(
        r"(?i:api[_\- ]?key|apikey|access[_\- ]?token|secret[_\- ]?key|bearer|password|passwd)"
        r"\s?[：:＝=]{0,2}\s?[\"']?[A-Za-z0-9_\-]{16,255}"
    ),
)

# 置換タグ（再検査で自分自身を再検出しないよう、記号は数字・英字と混ざらない形にする）
TAG_PHONE = "[[PHONE]]"
TAG_EMAIL = "[[EMAIL]]"
TAG_POSTAL = "[[POSTAL_CODE]]"
TAG_ADDRESS = "[[ADDRESS]]"
TAG_IP = "[[IP_ADDRESS]]"
TAG_URL_CREDENTIAL = "[[URL_CREDENTIAL]]"
TAG_SECRET = "[[SECRET]]"
TAG_JWT = "[[JWT]]"
TAG_INSURANCE = "[[INSURANCE_NUMBER]]"
TAG_MEDICAL_CARD = "[[MEDICAL_CARD_NUMBER]]"
TAG_PATIENT_ID = "[[PATIENT_ID]]"

#: 「臨床情報として残してよい」種別は1つも無い＝ここに載る型は全て置換対象。
RULE_TYPES = (
    "phone",
    "email",
    "postal_code",
    "address",
    "birthdate",
    "insurance_number",
    "medical_card_number",
    "patient_id",
    "ip_address",
    "url_credential",
    "api_key",
    "jwt",
)

#: 検出したらPrivacy Gateを blocked にする型（§6「ブロック」）。
BLOCKING_TYPES = frozenset({"insurance_number", "medical_card_number", "api_key", "jwt", "url_credential"})


def _to_ascii_digits(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def _digits_only(text: str) -> str:
    return "".join(ch for ch in _to_ascii_digits(text) if ch.isdigit())


def _age_bucket(birth_year: int, birth_month: int, birth_day: int, today: tuple[int, int, int]) -> str:
    """生年月日を年代（例: 「80代」）へ変換する（§6 仮名化ポリシー）。

    年齢そのものではなく年代に丸めるのは、生年月日→年齢の復元可能性を下げるため。
    100歳以上は「100歳以上」でまとめる（1人しかいない事業所では年代でも特定され得るが、
    そこまでは仮名化では消せない＝残存リスクとして privacy.md に明記してある）。
    """
    year, month, day = today
    age = year - birth_year - ((month, day) < (birth_month, birth_day))
    if age < 0:
        return "[[BIRTHDATE]]"  # 未来日＝生年月日として解釈できない
    if age >= 100:
        return "(100歳以上)"
    return f"({age // 10 * 10}代)"


def _address_detections(text: str) -> list[Detection]:
    """住所を都道府県レベルへ一般化する（§6「地域レベルへ一般化」）。"""
    results: list[Detection] = []
    for match in _ADDRESS_RE.finditer(text):
        pref = match.group("pref")
        rest = match.group("rest") or ""
        has_number = any(ch.isdigit() for ch in _to_ascii_digits(rest))
        # 都道府県が明示されている、または番地らしい数字が続く場合だけ住所とみなす。
        # 「この地区の担当」「◯◯村の方針」のような一般語を巻き込まないための条件。
        if not pref and not has_number:
            continue
        replacement = f"{pref}{TAG_ADDRESS}" if pref else TAG_ADDRESS
        results.append(
            Detection(
                type="address",
                start=match.start(),
                end=match.end(),
                value=match.group(0),
                replacement=replacement,
                action=ACTION_GENERALIZE,
            )
        )
    return results


def _phone_detections(text: str) -> list[Detection]:
    results: list[Detection] = []
    for match in _PHONE_RE.finditer(text):
        if len(_digits_only(match.group(0))) not in (10, 11):
            continue  # 日本の固定/携帯電話の桁数でないものは電話番号とみなさない
        results.append(
            Detection(
                type="phone",
                start=match.start(),
                end=match.end(),
                value=match.group(0),
                replacement=TAG_PHONE,
                action=ACTION_TAG,
            )
        )
    return results


def _birthdate_detections(text: str, today: tuple[int, int, int]) -> list[Detection]:
    results: list[Detection] = []
    for match in _BIRTHDATE_WESTERN_RE.finditer(text):
        results.append(
            Detection(
                type="birthdate",
                start=match.start(),
                end=match.end(),
                value=match.group(0),
                replacement=_age_bucket(
                    int(match.group("year")), int(match.group("month")), int(match.group("day")), today
                ),
                action=ACTION_GENERALIZE,
            )
        )
    for match in _BIRTHDATE_ERA_RE.finditer(text):
        era_year = 1 if match.group("year") == "元" else int(match.group("year"))
        year = _ERA_OFFSETS[match.group("era")] + era_year
        results.append(
            Detection(
                type="birthdate",
                start=match.start(),
                end=match.end(),
                value=match.group(0),
                replacement=_age_bucket(year, int(match.group("month")), int(match.group("day")), today),
                action=ACTION_GENERALIZE,
            )
        )
    return results


def _simple_detections(
    text: str, pattern: re.Pattern[str], type_: str, replacement: str, action: str
) -> list[Detection]:
    return [
        Detection(
            type=type_,
            start=m.start(),
            end=m.end(),
            value=m.group(0),
            replacement=replacement,
            action=action,
        )
        for m in pattern.finditer(text)
    ]


def detect(text: str, *, today: tuple[int, int, int]) -> list[Detection]:
    """ルールベースの全検出器を走らせ、重なりを解決した結果を返す。

    today は (年, 月, 日)。生年月日→年代の変換に使う。呼び出し側が明示的に渡すことで、
    同じ入力に対して同じ出力が出る（テストが日付で揺れない・監査の再現性が保てる）。
    """
    detections: list[Detection] = []

    # 秘密情報を先に拾う（URLに埋まったキー等、他の検出器と重なった場合に優先させるため）。
    detections += _simple_detections(text, _URL_CREDENTIAL_RE, "url_credential", TAG_URL_CREDENTIAL, ACTION_BLOCK)
    detections += _simple_detections(text, _JWT_RE, "jwt", TAG_JWT, ACTION_BLOCK)
    for pattern in _API_KEY_RES:
        detections += _simple_detections(text, pattern, "api_key", TAG_SECRET, ACTION_BLOCK)

    detections += _simple_detections(text, _INSURANCE_RE, "insurance_number", TAG_INSURANCE, ACTION_BLOCK)
    detections += _simple_detections(
        text, _MEDICAL_CARD_RE, "medical_card_number", TAG_MEDICAL_CARD, ACTION_BLOCK
    )
    detections += _simple_detections(text, _PATIENT_ID_RE, "patient_id", TAG_PATIENT_ID, ACTION_TAG)

    detections += _simple_detections(text, _EMAIL_RE, "email", TAG_EMAIL, ACTION_TAG)
    detections += _simple_detections(text, _IPV4_RE, "ip_address", TAG_IP, ACTION_TAG)
    detections += _simple_detections(text, _POSTAL_RE, "postal_code", TAG_POSTAL, ACTION_TAG)
    detections += _phone_detections(text)
    detections += _birthdate_detections(text, today)
    detections += _address_detections(text)

    return resolve_overlaps(detections)


#: 重なった検出のうちどちらを残すかの優先順位（大きいほど優先）。
_PRIORITY = {
    "url_credential": 90,
    "jwt": 85,
    "api_key": 80,
    "insurance_number": 70,
    "medical_card_number": 70,
    "patient_id": 60,
    "person_name": 55,
    "facility": 50,
    "email": 45,
    "address": 40,
    "birthdate": 35,
    "phone": 30,
    "postal_code": 25,
    "ip_address": 20,
}


def resolve_overlaps(detections: list[Detection]) -> list[Detection]:
    """重なった検出を1つに絞り、開始位置の昇順で返す。

    優先順位 → 長さ → 開始位置 の順で決める。決定論的であることが重要
    （同じ入力からは常に同じ仮名化テキストが出る＝再検査・監査が意味を持つ）。
    """
    ordered = sorted(
        detections,
        key=lambda d: (-_PRIORITY.get(d.type, 0), -d.length, d.start),
    )
    kept: list[Detection] = []
    for detection in ordered:
        if any(detection.start < k.end and k.start < detection.end for k in kept):
            continue
        kept.append(detection)
    return sorted(kept, key=lambda d: d.start)


def apply_detections(text: str, detections: list[Detection]) -> str:
    """検出結果をテキストへ適用する（後ろから置換するのでインデックスがずれない）。"""
    result = text
    for detection in sorted(detections, key=lambda d: d.start, reverse=True):
        result = result[: detection.start] + detection.replacement + result[detection.end :]
    return result
