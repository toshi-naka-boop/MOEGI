"""ログ漏えい防止（design/moegi/privacy.md §10）。

方針は2つ。

1. **出さない**: 本文（文字起こし原文・仮名化済み全文・日報/SOAP全文・対応表）を
   ログへ渡す経路そのものを作らない。監査に必要な情報は audit_fields() が返す
   許可リストだけで組み立てる。

2. **漏れても落とす**: それでも例外メッセージ・サードパーティのログ（uvicorn・httpx）
   経由で本文が流れることはある。install_log_sanitizer() がルートロガーへ
   フィルタを差し込み、logging を通る全レコードにサニタイズをかける。

サニタイズは rules.py の検出器を再利用する（ログ用に別のパターンを書くと、
片方だけ更新されて穴が開く）。
"""

from __future__ import annotations

import logging
from typing import Any

from moegi.privacy import rules

#: ログに残してよい情報（design/moegi/privacy.md §10）。ここに無いキーは監査記録に載らない。
ALLOWED_AUDIT_FIELDS = (
    "request_id",
    "site_id",
    "occurred_at",
    "gate_status",
    "effective_status",
    "mode",
    "detected_counts",
    "policy_version",
    "needs_human_review",
    "residual_risk",
    "duration_ms",
    "error_code",
    "local_model",
    "proceeded_to_structuring",
    "external_transmission",
)

#: ログ1行の上限。長い本文がまるごと流れるのを、内容に関わらず物理的に止める。
MAX_LOG_CHARS = 400

_MASK = "[[REDACTED]]"

#: サニタイズ時の日付基準。実際の年齢計算はしない（ログでは年代も出さずタグに潰す）。
_LOG_TODAY = (2000, 1, 1)


def sanitize(value: str) -> str:
    """ログへ出る文字列から、直接識別子・秘密情報を落として長さを打ち切る。

    rules.detect() は入力長に比例した時間で動くので、先に打ち切ってから検出する
    （巨大な文字列がログに来たときに検出処理で詰まらせない）。
    """
    if not isinstance(value, str):
        return value
    text = value if len(value) <= MAX_LOG_CHARS else value[:MAX_LOG_CHARS] + "…(truncated)"
    detections = rules.detect(text, today=_LOG_TODAY)
    if not detections:
        return text
    masked = [
        rules.Detection(
            type=d.type,
            start=d.start,
            end=d.end,
            value=d.value,
            replacement=_MASK,
            action=d.action,
        )
        for d in detections
    ]
    return rules.apply_detections(text, masked)


class SanitizingFilter(logging.Filter):
    """logging を通る全レコードの本文・引数・例外テキストをサニタイズする。

    filter() は False を返すとレコードを捨てるが、ここでは捨てずに書き換える
    （「ログが出ない」より「安全な形で出る」方が運用時に困らない）。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = sanitize(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: sanitize(v) if isinstance(v, str) else v for k, v in record.args.items()}
            else:
                record.args = tuple(sanitize(a) if isinstance(a, str) else a for a in record.args)
        if record.exc_info:
            # トレースバックの本文には、ローカル変数の値こそ出ないものの、
            # 例外メッセージ（発話原文を含み得る）がそのまま載る。
            # 文字列化してサニタイズしたうえで exc_info を外す。
            exc_type, exc_value, _ = record.exc_info
            summary = f"{getattr(exc_type, '__name__', 'Exception')}: {sanitize(str(exc_value))}"
            record.msg = f"{record.msg} | {summary}" if isinstance(record.msg, str) else summary
            record.exc_info = None
            record.exc_text = None
        return True


#: install_log_sanitizer() が触ったロガー名（二重装着を避けるための記録）
_INSTALLED: set[str] = set()

_TARGET_LOGGERS = ("", "uvicorn", "uvicorn.error", "uvicorn.access", "fastapi", "httpx", "httpcore")


def install_log_sanitizer(logger_names: tuple[str, ...] = _TARGET_LOGGERS) -> None:
    """ルート・uvicorn・httpx のロガーへサニタイザーを差し込む。

    アプリ起動時に1度だけ呼ぶ（webapp.py のモジュール読み込み時）。
    ハンドラ側ではなくロガー側に付けるのは、あとから追加されたハンドラにも効かせるため。
    """
    for name in logger_names:
        if name in _INSTALLED:
            continue
        logger = logging.getLogger(name)
        logger.addFilter(SanitizingFilter())
        _INSTALLED.add(name)


def audit_fields(**kwargs: Any) -> dict[str, Any]:
    """監査記録に載せてよいキーだけを残した辞書を返す（§10 の許可リスト）。

    許可リスト方式にしているのは、あとから呼び出し側が `text=...` を足しても
    本文がログに出ないようにするため（拒否リストだと足された分が素通りする）。
    """
    return {k: v for k, v in kwargs.items() if k in ALLOWED_AUDIT_FIELDS}
