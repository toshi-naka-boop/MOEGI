"""配布のキュー型チャネル（design.md §5.3・G8・ledger R-89）。

    承認 → キューに積む（store-and-forward）
         → オフライン中は「送信待ち n件」として溜まる
         → 回線が復帰したら**自動で**流れる（手動再送ボタンは作らない・R-16）

送信先は**差し替え可能**にしてある。design.md はLINEを想定していたが、G8の要件
（キューに積まれる／復帰で自動的に流れる／手動再送しない）は送信先が何であっても
満たせるため、外部連携の承認が要らないメールを既定にした（ユーザー決定・2026-08-13）。

設定（すべて環境変数。`.env` は作らない方針なので、実行時のシェルで export する）:

    MOEGI_DELIVERY_TO       宛先。既定 kanri@example.local
    MOEGI_SMTP_HOST         SMTPホスト。**未設定なら data/outbox/ への .eml 書き出し**
    MOEGI_SMTP_PORT         既定 587
    MOEGI_SMTP_USER         SMTPユーザー
    MOEGI_SMTP_PASSWORD     ⚠️ ここにしか置かない。コードにもDBにもログにも出さない
    MOEGI_SMTP_FROM         差出人。既定 moegi@example.local
    MOEGI_DELIVERY_PROBE    回線復帰の検知に使うTCPプローブ。既定 1.1.1.1:53
                            （SMTP未設定かつ未指定のときは、ファイル書き出しなので検査しない）

⚠️ **何を送ってよいか**（design/moegi/privacy.md §11 の外部送信境界）:
    積んでよいのは**承認済み更新案**だけ。チェックリスト項目のラベルと通達原文の引用で、
    要配慮個人情報を含まない（ledger N-11 のプライバシー境界の定義と同じ）。
    日報・SOAP・文字起こし原文・仮名対応表は積まない——`enqueue()` が本文を
    ルールベースの検出器にかけ、識別子が1つでも見つかったら**積むこと自体を拒否**する。
    「送る直前に止める」のではなく「キューに入る前に止める」ので、
    キューの中身は常に送ってよいものだけになる。
"""

from __future__ import annotations

import os
import smtplib
import socket
import sqlite3
import unicodedata
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

from moegi import paths, store
from moegi.privacy import rules

DEFAULT_OUTBOX_DIR = paths.data_dir() / "outbox"

CHANNEL_EMAIL = "email"
CHANNEL_FILE = "file"

#: 回線復帰の検知に使うTCPプローブの既定値。データは1バイトも送らず、接続できるかだけを見る。
DEFAULT_PROBE = "1.1.1.1:53"


class DeliveryRefused(RuntimeError):
    """送ってはいけない内容をキューに積もうとした（fail-closed）。"""


@dataclass(frozen=True)
class DeliveryConfig:
    """配信設定。認証情報は環境変数からのみ取り、コードにもDBにも書かない。"""

    recipient: str
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    sender: str = ""
    probe: str = DEFAULT_PROBE
    outbox_dir: Path = DEFAULT_OUTBOX_DIR

    @property
    def channel(self) -> str:
        """SMTPが設定されていなければファイル出力（デモ・開発用のスタブ）。"""
        return CHANNEL_EMAIL if self.smtp_host else CHANNEL_FILE


def load_config() -> DeliveryConfig:
    """環境変数から配信設定を読む。

    MOEGI_SMTP_HOST が未設定なら、送信は data/outbox/ への .eml 書き出しになる
    （SMTPの資格情報が無い環境でも、キューが溜まって復帰で流れる挙動は同じに見える）。
    パスワードは戻り値の外へ出さない——ログにも例外にも載せないこと。
    """
    return DeliveryConfig(
        recipient=os.environ.get("MOEGI_DELIVERY_TO", "kanri@example.local"),
        smtp_host=os.environ.get("MOEGI_SMTP_HOST", ""),
        smtp_port=int(os.environ.get("MOEGI_SMTP_PORT", "587")),
        smtp_user=os.environ.get("MOEGI_SMTP_USER", ""),
        smtp_password=os.environ.get("MOEGI_SMTP_PASSWORD", ""),
        sender=os.environ.get("MOEGI_SMTP_FROM", "moegi@example.local"),
        probe=os.environ.get("MOEGI_DELIVERY_PROBE", DEFAULT_PROBE),
    )


# ---------------------------------------------------------------------------
# 回線の状態
# ---------------------------------------------------------------------------


def is_online(config: DeliveryConfig, *, timeout: float = 2.0) -> bool:
    """回線が使えるかを、TCP接続が張れるかだけで判定する（G8の「復帰の検知」）。

    プローブ先はSMTPホストがあればそれ、無ければ設定のprobe。**データは送らない。**
    ここで嘘の「オンライン」を返すと、送信のたびにSMTPのタイムアウトを待つことになり、
    デモの復帰演出が固まって見える。
    """
    if not config.smtp_host and config.probe == DEFAULT_PROBE:
        # SMTP未設定＝data/outbox/ へのファイル書き出しで、回線を使わない。プローブ先を
        # 利用者が明示していない限り、インターネットに出られない閉じたLANでも流す。
        # （回線断のデモをしたいときは MOEGI_DELIVERY_PROBE に到達できない宛先を指定する）
        return True
    target = f"{config.smtp_host}:{config.smtp_port}" if config.smtp_host else config.probe
    host, _, port = target.partition(":")
    try:
        with socket.create_connection((host, int(port or 0)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# 送信先（差し替え可能）
# ---------------------------------------------------------------------------


class FileSender:
    """data/outbox/ に .eml を書き出すスタブ。SMTPの資格情報が無い環境用。"""

    channel = CHANNEL_FILE

    def __init__(self, config: DeliveryConfig):
        self._config = config

    def send(self, delivery: dict) -> None:
        outbox = Path(self._config.outbox_dir)
        outbox.mkdir(parents=True, exist_ok=True)
        message = _build_message(self._config, delivery)
        (outbox / f"delivery-{delivery['row_id']:06d}.eml").write_bytes(message.as_bytes())


class SmtpSender:
    """SMTPで送る。認証情報は DeliveryConfig からのみ取る。"""

    channel = CHANNEL_EMAIL

    def __init__(self, config: DeliveryConfig):
        self._config = config

    def send(self, delivery: dict) -> None:
        config = self._config
        if config.smtp_port == 465:
            # 465番は最初から暗号化された接続（SMTPS）。STARTTLS ではない。
            client_cm = smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=10)
        else:
            client_cm = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=10)
        with client_cm as client:
            if config.smtp_port != 465:
                client.ehlo()
                if not client.has_extn("starttls"):
                    # 暗号化できないサーバーへパスワードや本文を平文で送らない。
                    raise smtplib.SMTPNotSupportedError("STARTTLS 非対応のサーバーです")
                client.starttls()
                client.ehlo()
            if config.smtp_user:
                client.login(config.smtp_user, config.smtp_password)
            client.send_message(_build_message(config, delivery))


def _build_message(config: DeliveryConfig, delivery: dict) -> EmailMessage:
    message = EmailMessage()
    message["From"] = config.sender
    message["To"] = delivery["recipient"]
    message["Subject"] = delivery["subject"]
    message.set_content(delivery["body"])
    return message


def build_sender(config: DeliveryConfig):
    return SmtpSender(config) if config.smtp_host else FileSender(config)


# ---------------------------------------------------------------------------
# キュー操作
# ---------------------------------------------------------------------------


def _screen_body(text: str, *, today: tuple[int, int, int]) -> list[str]:
    """外部へ出す本文に識別子が混じっていないかを決定論的に検査する。

    ここで見つかる＝「そもそもキューに積んではいけない内容」なので、種別だけを返す。
    値そのものは返さない（呼び出し側が例外メッセージへ載せて漏らすのを防ぐ）。
    """
    normalized = unicodedata.normalize("NFKC", text)
    return sorted({d.type for d in rules.detect(normalized, today=today)})


def screen_notice(rows: list[dict], *, today: tuple[int, int, int]) -> list[str]:
    """承認通知（件名・本文）に識別子らしき文字列が含まれていないかを検査し、検出種別を返す。

    承認・編集の保存の前に呼び、空でなければ**承認も保存もしない**（配信だけ止めて、FAX や
    日報のチェックリストには載る、という食い違いを作らないため）。値そのものは返さない。
    """
    subject, body = format_proposal_notice(rows)
    return _screen_body(f"{subject}\n{body}", today=today)


def enqueue(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    subject: str,
    body: str,
    proposal_row_ids: list[int],
    config: DeliveryConfig,
    today: tuple[int, int, int],
) -> int:
    """配信キューに1件積む。

    積む前に本文を検査し、識別子・秘密情報が1つでも見つかったら DeliveryRefused を投げる
    （fail-closed）。「送信直前に止める」のではなく「キューに入る前に止める」ので、
    キューの中身は常に送ってよいものだけになる。
    """
    found = _screen_body(f"{subject}\n{body}", today=today)
    if found:
        raise DeliveryRefused(
            "外部送信できない情報が含まれているため配信キューに積みませんでした"
            f"（検出種別: {', '.join(found)}）"
        )
    existing = store.find_pending_delivery(conn, subject=subject, body=body)
    if existing is not None:
        return existing  # 承認ボタンの二度押し・戻る→再送信で、同じ通知を二重に積まない
    return store.save_delivery(
        conn,
        run_id=run_id,
        channel=config.channel,
        recipient=config.recipient,
        subject=subject,
        body=body,
        proposal_row_ids=proposal_row_ids,
    )


@dataclass
class FlushResult:
    sent: int = 0
    failed: int = 0
    skipped_offline: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.sent or self.failed)


def flush(
    conn: sqlite3.Connection, *, config: DeliveryConfig, sender=None, limit: int = 20
) -> FlushResult:
    """送信待ちを順に送る。失敗しても status は pending のまま残す。

    オフラインなら1件も試さずに戻る（毎回タイムアウトを待つと、復帰演出が固まって見える）。
    例外の中身はDBにもログにも入れない——宛先や本文が混じり得るため、
    種別だけを error_code として残す（design/moegi/privacy.md §10）。
    """
    result = FlushResult()
    pending = store.list_pending_deliveries(conn, limit=limit)
    if not pending:
        return result
    if not is_online(config):
        result.skipped_offline = True
        return result

    sender = sender or build_sender(config)
    for delivery in pending:
        # 読み出したあとに却下で取り消された通知は送らない（誤った手順を配らない）。
        if not store.is_delivery_pending(conn, delivery["row_id"]):
            continue
        try:
            sender.send(delivery)
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPNotSupportedError, smtplib.SMTPAuthenticationError):
            # 宛先拒否・STARTTLS 非対応など、待っても直らない失敗。この通知は試行回数を数え、
            # 上限で「送信失敗」にして、後ろの通知は流し続ける。
            store.mark_delivery_attempt_failed(conn, delivery["row_id"], "send_rejected")
            result.failed += 1
        except (OSError, smtplib.SMTPException):
            store.mark_delivery_attempt_failed(conn, delivery["row_id"], "send_failed")
            result.failed += 1
            # 接続できない（回線側の問題）なら残りも同じ理由で落ちるので打ち切る。
            break
        else:
            store.mark_delivery_sent(conn, delivery["row_id"])
            result.sent += 1
    return result


def format_proposal_notice(rows: list[dict]) -> tuple[str, str]:
    """承認済み更新案から、配信本文（件名, 本文）を組み立てる。

    入れるのはチェックリスト項目のラベルと更新後の文言、そして通達の条文パスだけ。
    通達原文の引用（evidence_quote）は入れない——長いうえに、配布物としては
    「何がどう変わるか」だけで足りるため。
    """
    lines = ["チェックリストの更新案が承認されました。", ""]
    for row in rows:
        target = row.get("sitedoc_item_id") or "（新規項目）"
        lines.append(f"■ 対象項目: {target}")
        lines.append(f"  変更前: {row.get('before') or '（この項目は現在存在しません）'}")
        lines.append(f"  変更後: {row.get('after', '')}")
        lines.append(f"  根拠条文: {row.get('new_path', '')}")
        lines.append("")
    lines.append("※ この通知は承認済みの更新案のみを含みます。利用者の記録は含まれません。")
    subject = f"【MOEGI】チェックリスト更新案の承認通知（{len(rows)}件）"
    return subject, "\n".join(lines)
