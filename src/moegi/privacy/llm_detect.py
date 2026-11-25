"""ローカルLLMによる文脈依存情報の補完検出（design/moegi/privacy.md §5.3）。

ルール（rules.py）と辞書（dictionary.py）を通したあとのテキストを入力にする。
つまりローカルLLMに渡る時点で、直接識別子と登録済みの氏名は既に消えている。
LLMが担当するのは「娘の花子」「向かいの佐々木さん」のような、辞書に無い文脈上の
固有名だけ。

stage は "pseudonymize"。llm.py の LOCAL_ONLY_STAGES に入っているので、
models.toml で backend="orca" を指定しても起動時に拒否される（設定ミスで
患者の発話がクラウドへ出ることが構造的に起こらない）。

JSON Schemaに従った構造化出力を要求し、スキーマ違反・タイムアウト・接続失敗は
すべて「自動で先へ進めない」（§5.3の停止条件）。ここでのfail-closedは
LLMDetectionResult.ok=False として呼び出し側（gate.py）へ返す。
"""

from __future__ import annotations

from moegi.jsonutil import extract_json_payload as _extract_json_object
from dataclasses import dataclass, field

STAGE = "pseudonymize"

RISK_LEVELS = ("low", "medium", "high")

#: §5.3 の構造化出力スキーマ（人間向けの記述はプロンプトに、機械的な検証は _validate に）
OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["sanitized_text", "detected_entities", "residual_risk", "needs_human_review"],
    "properties": {
        "sanitized_text": {"type": "string"},
        "detected_entities": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["type", "replacement", "confidence", "review_required"],
                "properties": {
                    "type": {"type": "string"},
                    "replacement": {"type": "string"},
                    "confidence": {"type": "number"},
                    "review_required": {"type": "boolean"},
                },
            },
        },
        "residual_risk": {"type": "string", "enum": list(RISK_LEVELS)},
        "needs_human_review": {"type": "boolean"},
    },
}

_PROMPT = """あなたは訪問看護記録の匿名化担当者です。
以下のテキストは、既に機械的なルールと事業所マスタによって、電話番号・住所・生年月日・
登録済みの氏名などが仮名（PATIENT_001 等）や型付きタグ（[[PHONE]] 等）へ置換済みです。

あなたの仕事は、**まだ残っている「個人を特定し得る表現」だけ**を見つけて置換することです。

【置換する対象】
- 文脈中の固有名（例:「娘の花子」の「花子」、「向かいの佐々木さん」の「佐々木」）
- 愛称・略称・音声認識で表記が崩れた氏名
- 家族・近隣住民・医療従事者の固有名
- 住所や施設を推測できる表現（「駅前の◯◯ビル3階」など）
- 単独では特定できないが、組み合わせると個人が特定され得る表現

【絶対に消してはいけない対象（そのまま残すこと）】
- 病名・症状・服薬・処置の内容
- バイタルサイン（血圧・体温・脈拍・呼吸数・SpO2 等の数値）
- 看護目標・評価・ADL・生活状況の記述
- 既にある仮名（PATIENT_001 等）とタグ（[[PHONE]] 等）— これらは書き換えないこと

【置換の書き方】
- 家族は続柄（長女・配偶者・長男 など）へ
- 職員は NURSE_101、医師は DOCTOR_101、その他の人物は PERSON_101 から採番
- 施設・建物は FACILITY_101 から採番

【対象テキスト】
{text}

【出力形式】
次のJSONオブジェクトのみを出力すること。前置き・説明文・コードブロック記号は付けないこと。
- sanitized_text: 置換後のテキスト全文。置換が不要なら入力をそのまま返すこと
- detected_entities: 置換した箇所の配列。各要素は type・replacement・confidence・review_required の4キー
- residual_risk: "low" | "medium" | "high" のいずれか。判断に迷う表現が残っているなら "medium" 以上
- needs_human_review: 人による確認が必要なら true

出力例:
{{"sanitized_text": "PATIENT_001は昨日から発熱がある。長女から連絡があった。", "detected_entities": [{{"type": "person_name", "replacement": "長女", "confidence": 0.95, "review_required": false}}], "residual_risk": "low", "needs_human_review": false}}
"""


@dataclass
class LLMDetectionResult:
    """ローカルLLM補完検出の結果。ok=False なら自動で先へ進めてはいけない。"""

    ok: bool
    sanitized_text: str
    entities: list[dict] = field(default_factory=list)
    residual_risk: str = "high"
    needs_human_review: bool = True
    #: 失敗理由のコード（本文は含めない。ログ・監査へそのまま出せる語彙にする）
    error_code: str | None = None


def _validate(payload: dict) -> str | None:
    """OUTPUT_SCHEMA に対する最小限の検証。違反していれば理由コードを返す。

    jsonschema等の新しい依存を足さず、必要な検査だけを手で書く（型・必須キー・enum）。
    """
    for key in OUTPUT_SCHEMA["required"]:
        if key not in payload:
            return "schema_missing_key"
    if not isinstance(payload["sanitized_text"], str):
        return "schema_type_sanitized_text"
    if not isinstance(payload["detected_entities"], list):
        return "schema_type_detected_entities"
    for entity in payload["detected_entities"]:
        if not isinstance(entity, dict):
            return "schema_type_entity"
        for key in ("type", "replacement", "confidence", "review_required"):
            if key not in entity:
                return "schema_missing_entity_key"
        if not isinstance(entity["confidence"], (int, float)) or isinstance(entity["confidence"], bool):
            return "schema_type_confidence"
        if not isinstance(entity["review_required"], bool):
            return "schema_type_review_required"
    if payload["residual_risk"] not in RISK_LEVELS:
        return "schema_enum_residual_risk"
    if not isinstance(payload["needs_human_review"], bool):
        return "schema_type_needs_human_review"
    return None


def detect(text: str, adapter) -> LLMDetectionResult:
    """ローカルLLMへ補完検出を依頼する。

    adapter は llm.LLMAdapter（complete(stage, prompt, opts) -> dict の契約）。
    例外・タイムアウト・スキーマ違反はすべて ok=False として返す——ここで
    「まあ大丈夫だろう」と通すと、仮名化されていない発話が下流へ流れる。
    """
    try:
        result = adapter.complete(STAGE, _PROMPT.format(text=text))
    except Exception:  # noqa: BLE001 — 接続失敗・設定不備を含め、理由に関わらず先へ進めない
        # 例外オブジェクトそのものは呼び出し元へ渡さない（メッセージに本文が
        # 混ざる可能性を構造的に消すため。§10 ログ漏えい防止）。
        return LLMDetectionResult(ok=False, sanitized_text=text, error_code="llm_unavailable")

    if result.get("unavailable"):
        # Ollama停止・接続拒否・応答が読めない。timeoutとは原因も次の行動も違うので分ける。
        return LLMDetectionResult(ok=False, sanitized_text=text, error_code="llm_unavailable")
    if result.get("timeout"):
        return LLMDetectionResult(ok=False, sanitized_text=text, error_code="llm_timeout")

    payload = _extract_json_object(result.get("text") or "")
    if payload is None:
        return LLMDetectionResult(ok=False, sanitized_text=text, error_code="llm_invalid_json")

    violation = _validate(payload)
    if violation is not None:
        return LLMDetectionResult(ok=False, sanitized_text=text, error_code=violation)

    return LLMDetectionResult(
        ok=True,
        sanitized_text=payload["sanitized_text"],
        entities=payload["detected_entities"],
        residual_risk=payload["residual_risk"],
        needs_human_review=payload["needs_human_review"],
    )
