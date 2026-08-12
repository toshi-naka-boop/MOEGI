"""Privacy Gate 本体（design/moegi/privacy.md §4・§7）。

    文字起こし原文
        → ①ルール・辞書による決定論的な仮名化
        → ②ローカルLLMによる補完検出
        → ③LLMから独立した再検査
        → passed なら既存の日報・SOAP構造化へ / それ以外は停止して人が確認

このモジュールは「既存処理を呼んでよいか」を返すだけで、既存処理そのものは呼ばない。
接続点を1本（cleared_for_downstream）に絞ることで、あとから経路が増えても
検査を迂回できる箇所が増えないようにする。
"""

from __future__ import annotations

import re
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from moegi.privacy import dictionary as dict_module
from moegi.privacy import llm_detect, recheck, rules, vault
from moegi.privacy.config import PrivacyConfig


class PrivacyGateStatus(str, Enum):
    """design/moegi/privacy.md §7。

        pending         : まだ検査していない
        passed          : 既存の日報・SOAP構造化へ進んでよい
        review_required : 人が確認・修正してから再検査すること
        blocked         : 記録に残してはいけない情報が含まれる（保険証番号・APIキー等）
        failed          : 検査自体が完了しなかった（対応表の保存失敗・入力不正等）
    """

    PENDING = "pending"
    PASSED = "passed"
    REVIEW_REQUIRED = "review_required"
    BLOCKED = "blocked"
    FAILED = "failed"


#: 深刻な順。複数の理由が同時に立った場合はより深刻な方を採る。
_SEVERITY = {
    PrivacyGateStatus.PASSED: 0,
    PrivacyGateStatus.REVIEW_REQUIRED: 1,
    PrivacyGateStatus.BLOCKED: 2,
    PrivacyGateStatus.FAILED: 3,
}


@dataclass
class GateResult:
    """Privacy Gate の結果。**原文は保持しない**（sanitized_text だけを持つ）。

    detected_counts は「種別 → 件数」で、検出した値そのものは含まない。
    これをそのままログ・監査・画面へ出せる（§10 のログ許可リストと整合する）。
    """

    request_id: str
    status: PrivacyGateStatus
    mode: str
    sanitized_text: str
    policy_version: str
    detected_counts: dict[str, int] = field(default_factory=dict)
    needs_human_review: bool = False
    residual_risk: str = "low"
    reason_codes: list[str] = field(default_factory=list)
    removal_ratio: float = 0.0
    duration_ms: float = 0.0
    mapping_saved: bool = False
    local_model: str = ""
    #: この処理で外部（クラウド）へ送信したか。ローカル完結なので常に False。
    external_transmission: bool = False

    @property
    def detected_total(self) -> int:
        return sum(self.detected_counts.values())

    @property
    def would_block(self) -> bool:
        """enforce だったら既存処理を止めていたか（shadowモードの表示に使う）。"""
        return self.status is not PrivacyGateStatus.PASSED


def cleared_for_downstream(result: GateResult) -> bool:
    """既存の日報・SOAP構造化処理を呼んでよいか。

    enforce では passed のときだけ True。observe / shadow は段階導入のため常に True を返すが、
    **仮名化テキストを渡すこと自体は3モードとも共通**（モードで変わるのは止めるかどうかだけ）。
    """
    if result.mode == "enforce":
        return result.status is PrivacyGateStatus.PASSED
    return True


def cleared_for_external_transmission(result: GateResult) -> bool:
    """将来クラウドLLMを追加する場合の境界（§11）。passed 以外は決して True にしない。

    observe / shadow の緩和はここには効かせない——段階導入は「事業所内の既存処理を
    止めない」ための猶予であって、外部送信を緩める理由にはならない。
    """
    return result.status is PrivacyGateStatus.PASSED


class ExternalTransmissionBlocked(RuntimeError):
    """Privacy Gate を通っていないデータを外部送信しようとした。"""


def assert_cleared_for_external_transmission(result: GateResult) -> None:
    """外部送信の直前に呼ぶ関門（§11）。現時点で本番の呼び出し箇所は無い。"""
    if not cleared_for_external_transmission(result):
        raise ExternalTransmissionBlocked(
            f"Privacy Gateが{result.status.value}のため外部送信を中止しました"
            f"（request_id={result.request_id}）"
        )


def _is_mojibake(text: str) -> bool:
    """文字化けの検知。置換文字（U+FFFD）か、制御文字が多すぎる場合。"""
    if "�" in text:
        return True
    control = sum(1 for ch in text if unicodedata.category(ch) == "Cc" and ch not in "\n\r\t")
    return control > 0 and control / len(text) > 0.05


def _worst(*statuses: PrivacyGateStatus) -> PrivacyGateStatus:
    return max(statuses, key=lambda s: _SEVERITY[s])


#: 仮名・タグに含まれる数字（PATIENT_001 の 001、[[...]] の中身）は臨床情報ではないので、
#: 数値の保存チェックの対象から外すために先に取り除く。
_PSEUDONYM_TOKEN_RE = re.compile(r"\[\[[A-Z_0-9]{1,32}\]\]|[A-Z]{3,12}_[A-Za-z0-9]{1,16}")
_NUMBER_RE = re.compile(r"[0-9]{1,4}(?:\.[0-9]{1,2})?")


def _clinical_numbers(text: str) -> list[str]:
    """臨床情報の中核である数値（バイタル・用量・回数）を取り出す。

    仮名（PATIENT_001）とタグ（[[PHONE]]）を先に落としてから数える——それらの数字は
    臨床情報ではないので、消えても増えても構わない。
    """
    stripped = _PSEUDONYM_TOKEN_RE.sub(" ", text)
    return _NUMBER_RE.findall(stripped)


def _dropped_clinical_numbers(before: str, after: str) -> list[str]:
    """仮名化の前後で消えた数値を返す（design/moegi/privacy.md §5.3 の停止条件）。

    実測（2026-08-12・ledger R-86）でローカル7Bモデルが「出力例の文をそのまま
    書き写す」＝本文まるごと差し替えを起こしたため追加した。文字数の比（max_removal_ratio）
    だけでは、長さが近い別の文に化けたケースを取り逃す。バイタルの数値が消えたかどうかは
    決定論的に判定でき、臨床上の欠落に直結する。
    """
    remaining = list(_clinical_numbers(after))
    dropped: list[str] = []
    for number in _clinical_numbers(before):
        if number in remaining:
            remaining.remove(number)
        else:
            dropped.append(number)
    return dropped


def _count_by_type(detections: list[rules.Detection]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for detection in detections:
        counts[detection.type] = counts.get(detection.type, 0) + 1
    return counts


def run_gate(
    text: str,
    *,
    config: PrivacyConfig,
    adapter=None,
    vault_conn=None,
    request_id: str | None = None,
    site_id: str = "",
    today: tuple[int, int, int] | None = None,
    dictionary: dict_module.Dictionary | None = None,
) -> GateResult:
    """仮名化・安全検査を実行する（design/moegi/privacy.md §4 のデータフロー）。

    adapter    : llm.LLMAdapter。None または config.llm_detection=False ならLLM補完検出を行わない
    vault_conn : vault.connect() の接続。None なら対応表を保存しない（保存不要の再検査等）
    dictionary : 事前ロード済み辞書。None ならこの関数が config.dictionary_path から読む
    today      : 生年月日→年代の基準日。テストの再現性のため明示指定できる

    どの失敗経路でも例外は投げず、GateResult の status で返す。呼び出し側が
    「例外を握り潰して先へ進む」実装をしても、status を見ないかぎり
    cleared_for_downstream() が True にならない構造にしてある。
    """
    started = time.monotonic()
    request_id = request_id or uuid.uuid4().hex
    today = today or (lambda d: (d.year, d.month, d.day))(date.today())
    reason_codes: list[str] = []

    def _result(status: PrivacyGateStatus, sanitized: str, **kwargs) -> GateResult:
        return GateResult(
            request_id=request_id,
            status=status,
            mode=config.mode,
            sanitized_text=sanitized,
            policy_version=config.policy_version,
            duration_ms=(time.monotonic() - started) * 1000,
            reason_codes=reason_codes,
            **kwargs,
        )

    # --- 入力の検査（fail-closed。ここを通らないものは仮名化を試みない） ---------
    if not text or not text.strip():
        reason_codes.append("empty_input")
        return _result(PrivacyGateStatus.FAILED, "", needs_human_review=True, residual_risk="high")

    if len(text) > config.max_input_chars:
        # 正規表現へ巨大な入力を渡さないため、仮名化を試みずここで止める。
        reason_codes.append("input_too_long")
        return _result(PrivacyGateStatus.BLOCKED, "", needs_human_review=True, residual_risk="high")

    if _is_mojibake(text):
        reason_codes.append("mojibake_input")
        return _result(PrivacyGateStatus.FAILED, "", needs_human_review=True, residual_risk="high")

    # --- 辞書のロード（失敗は fail-closed。辞書無しで「合格」にしない） -----------
    if dictionary is None:
        try:
            dictionary = dict_module.load_dictionary(config.dictionary_path)
        except Exception:  # noqa: BLE001 — 例外の中身（パス・内容）をそのまま外に出さない
            dictionary = None
            reason_codes.append("dictionary_unavailable")

    # --- ①ルール・辞書による決定論的な仮名化 -------------------------------------
    allocator = dict_module.PseudonymAllocator()
    detections = rules.detect(text, today=today)
    if dictionary is not None:
        detections = rules.resolve_overlaps(detections + dict_module.detect(text, dictionary, allocator))

    deterministic_text = rules.apply_detections(text, detections)
    detected_counts = _count_by_type(detections)

    has_blocking = any(d.action == rules.ACTION_BLOCK for d in detections)
    if has_blocking:
        reason_codes.extend(
            sorted({f"blocked_{d.type}" for d in detections if d.action == rules.ACTION_BLOCK})
        )
    dictionary_review = any(d.review_required for d in detections)
    if dictionary_review:
        reason_codes.append("ambiguous_name")  # 同姓同名など一意に決められない照合

    status = PrivacyGateStatus.PASSED
    if has_blocking:
        status = _worst(status, PrivacyGateStatus.BLOCKED)
    if dictionary is None:
        status = _worst(status, PrivacyGateStatus.REVIEW_REQUIRED)
    if dictionary_review:
        status = _worst(status, PrivacyGateStatus.REVIEW_REQUIRED)

    # --- ②ローカルLLMによる補完検出 ----------------------------------------------
    sanitized_text = deterministic_text
    residual_risk = "low"
    needs_human_review = dictionary_review
    local_model = ""

    if config.llm_detection and adapter is not None and status is not PrivacyGateStatus.BLOCKED:
        llm_result = llm_detect.detect(deterministic_text, adapter)
        local_model = llm_detect.STAGE
        if not llm_result.ok:
            # タイムアウト・接続失敗・スキーマ違反はすべて自動で先へ進めない（§5.3）
            reason_codes.append(llm_result.error_code or "llm_failed")
            status = _worst(status, PrivacyGateStatus.REVIEW_REQUIRED)
            needs_human_review = True
            residual_risk = "high"
        else:
            sanitized_text = llm_result.sanitized_text
            residual_risk = llm_result.residual_risk
            needs_human_review = needs_human_review or llm_result.needs_human_review
            for entity in llm_result.entities:
                key = f"llm:{entity.get('type', 'unknown')}"
                detected_counts[key] = detected_counts.get(key, 0) + 1
                if entity.get("review_required"):
                    needs_human_review = True
    elif config.llm_detection and adapter is not None:
        reason_codes.append("llm_skipped_blocked")

    # --- 臨床情報の過剰削除チェック（§5.3の停止条件） -----------------------------
    removal_ratio = max(0.0, (len(deterministic_text) - len(sanitized_text)) / len(deterministic_text))
    if removal_ratio > config.max_removal_ratio:
        reason_codes.append("excessive_removal")
        status = _worst(status, PrivacyGateStatus.REVIEW_REQUIRED)
        needs_human_review = True

    # 文字数比だけでは、長さの近い別の文に化けたケース（実測: 出力例の書き写し・ledger R-86）を
    # 取り逃す。バイタル等の数値が消えていないかを決定論的に確かめる。
    dropped_numbers = _dropped_clinical_numbers(deterministic_text, sanitized_text)
    if dropped_numbers:
        reason_codes.append("clinical_numbers_dropped")
        status = _worst(status, PrivacyGateStatus.REVIEW_REQUIRED)
        needs_human_review = True

    # --- ③LLMから独立した再検査（§8） --------------------------------------------
    recheck_result = recheck.run(sanitized_text, dictionary=dictionary, today=today)
    if not recheck_result.passed:
        reason_codes.extend(recheck_result.reason_codes)
        needs_human_review = True
        residual_blocking = set(recheck_result.residual_counts) & rules.BLOCKING_TYPES
        status = _worst(
            status,
            PrivacyGateStatus.BLOCKED if residual_blocking else PrivacyGateStatus.REVIEW_REQUIRED,
        )

    # --- 残存リスク・人手確認の判定 ------------------------------------------------
    if config.risk_exceeds_threshold(residual_risk):
        reason_codes.append("residual_risk_above_threshold")
        status = _worst(status, PrivacyGateStatus.REVIEW_REQUIRED)
        needs_human_review = True
    if needs_human_review:
        status = _worst(status, PrivacyGateStatus.REVIEW_REQUIRED)

    # --- 仮名対応表の保存（保存できなければ fail-closed・§9） ---------------------
    mapping_saved = False
    mapping = allocator.mapping
    if vault_conn is not None and mapping:
        try:
            vault.save_mapping(
                vault_conn,
                request_id=request_id,
                mapping=mapping,
                policy_version=config.policy_version,
                site_id=site_id,
            )
            mapping_saved = True
        except Exception:  # noqa: BLE001 — 例外の中身に対応表が混ざらないよう外へ出さない
            reason_codes.append("vault_save_failed")
            status = _worst(status, PrivacyGateStatus.FAILED)
            needs_human_review = True

    return _result(
        status,
        sanitized_text,
        detected_counts=detected_counts,
        needs_human_review=needs_human_review,
        residual_risk=residual_risk,
        removal_ratio=removal_ratio,
        mapping_saved=mapping_saved,
        local_model=local_model,
    )
