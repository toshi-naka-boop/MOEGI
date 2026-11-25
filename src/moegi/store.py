"""原価実測・承認パイプライン結果の永続化（design.md §6.4）。SQLiteに保存する。

design.md §6.4:
    CostRecord 原価 { run_id, stage, backend, tokens_in, tokens_out, cost_jpy,
                      latency_ms, degraded }
    Change / Impact / Proposal はD4で追加（承認UI・原文トレースの永続化）。
    pipeline.py のデータクラスが持つ id（allowlistラベル起点の文字列。run跨ぎで
    一意ではない）とは別に、テーブル側は AUTOINCREMENT の行IDを主キーにする
    （run_idをまたいだ一意性と、外部キー参照の単純さのため）。

これに加えて、llm.py が全呼び出しで記録する resolved_model / fallback_level
（design.md §6.1）、および timeout（design.md §6.6・v10。ローカル呼び出しが
タイムアウトした場合のフラグ。残さないと⑥の実測にも障害分析にも使えない）
も一緒に永続化する。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = _REPO_ROOT / "data" / "moegi.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cost_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    backend TEXT NOT NULL,
    tokens_in INTEGER NOT NULL,
    tokens_out INTEGER NOT NULL,
    cost_jpy REAL NOT NULL,
    latency_ms REAL NOT NULL,
    degraded INTEGER NOT NULL,
    resolved_model TEXT NOT NULL,
    fallback_level INTEGER NOT NULL,
    timeout INTEGER NOT NULL DEFAULT 0,
    guardrail_blocked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS changes (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    label TEXT NOT NULL,
    old_path TEXT,
    old_text TEXT,
    new_path TEXT NOT NULL,
    new_text TEXT NOT NULL,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS impacts (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    change_row_id INTEGER NOT NULL REFERENCES changes(row_id),
    -- ⚠️ sitedoc_item_id は**表示用のラベル**（可変）。同一性の判定に使わないこと。
    --    突き合わせは sitedoc_item_key（不変ID・ledger R-88）で行う。
    sitedoc_item_id TEXT,
    sitedoc_item_key TEXT,
    verdict TEXT NOT NULL,
    rationale TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence_path TEXT,
    evidence_quote TEXT,
    evidence_offset INTEGER
);

CREATE TABLE IF NOT EXISTS proposals (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    impact_row_id INTEGER NOT NULL REFERENCES impacts(row_id),
    before TEXT NOT NULL,
    after TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT '未承認',
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS fax_deliveries (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    proposal_row_ids TEXT NOT NULL,
    file_path TEXT NOT NULL,
    generated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 配布のキュー型チャネル（design.md G8・ledger R-89）。store-and-forward。
-- ⚠️ ここに積んでよいのは**承認済み更新案**だけ（チェックリスト項目ラベル＋通達原文の引用）。
--    日報・SOAP・文字起こし原文・仮名対応表は積まない。delivery.enqueue() が
--    ルールベースの検出器で本文を検査し、識別子が混じっていたら積むこと自体を拒否する。
CREATE TABLE IF NOT EXISTS delivery_queue (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    proposal_row_ids TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error_code TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_delivery_queue_status ON delivery_queue(status, row_id);

-- 日報・SOAPの構造化結果（月報の集計材料）。
-- ⚠️ 保存するのは **Privacy Gate を通過した仮名化済みの構造化結果だけ**。
--    文字起こし原文・音声はここにも他のどこにも保存しない。
-- ⚠️ 利用者コードそのものは保存しない。保存するのは鍵つきHMACで導出した安定仮名
--    （vault.stable_pseudonym）だけなので、このテーブル単体からは利用者を逆引きできない。
CREATE TABLE IF NOT EXISTS nippou_records (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    subject_pseudonym TEXT NOT NULL,
    visited_on TEXT NOT NULL,
    gate_status TEXT NOT NULL,
    checklist_version TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    items TEXT NOT NULL,
    evaluation TEXT,
    procedures TEXT NOT NULL,
    soap TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_nippou_records_subject_month
    ON nippou_records(subject_pseudonym, visited_on);

-- 仮名化処理の監査記録（design/moegi/privacy.md §10）。
-- ⚠️ 本文（原文・仮名化済み全文・日報/SOAP全文）と仮名対応表はこのテーブルに入れない。
--    detected_counts は「種別 → 件数」のJSONで、検出した値そのものは含まない。
--    仮名対応表は別DBファイル（data/privacy_vault.db・暗号化）に置く。
CREATE TABLE IF NOT EXISTS privacy_audits (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    site_id TEXT NOT NULL DEFAULT '',
    gate_status TEXT NOT NULL,
    mode TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    detected_counts TEXT NOT NULL,
    needs_human_review INTEGER NOT NULL,
    residual_risk TEXT NOT NULL,
    reason_codes TEXT NOT NULL,
    duration_ms REAL NOT NULL,
    local_model TEXT NOT NULL DEFAULT '',
    proceeded_to_structuring INTEGER NOT NULL,
    external_transmission INTEGER NOT NULL DEFAULT 0,
    occurred_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """SQLiteに接続し、テーブルが無ければ作成する。

    既存DB（timeout列が無い旧スキーマ）に対しては ALTER TABLE で列を追加する
    （design.md §6.6・v10で追加された timeout フラグ）。
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(cost_records)")}
    if "timeout" not in columns:
        conn.execute("ALTER TABLE cost_records ADD COLUMN timeout INTEGER NOT NULL DEFAULT 0")
    # ledger R-91: ORCA Guardrails（ゲートウェイ側の第二防衛線）が遮断してローカルへ
    # フォールバックした呼び出しのフラグ。残さないと「第二防衛線が実際に働いたか」を
    # 後から監査できない。
    if "guardrail_blocked" not in columns:
        conn.execute(
            "ALTER TABLE cost_records ADD COLUMN guardrail_blocked INTEGER NOT NULL DEFAULT 0"
        )
    # ledger R-88: 既存DB（不変IDを持たない旧スキーマ）へ列を追加する。既存行の
    # sitedoc_item_key は NULL のままにする——過去の判定はラベルでしか記録されておらず、
    # あとから正しい不変IDを推測することはできない。突き合わせ側がラベルへ
    # フォールバックする（pipeline._lookup_item / checklist.apply_approved）。
    impact_columns = {row[1] for row in conn.execute("PRAGMA table_info(impacts)")}
    if "sitedoc_item_key" not in impact_columns:
        conn.execute("ALTER TABLE impacts ADD COLUMN sitedoc_item_key TEXT")
    conn.commit()
    return conn


def new_run_id() -> str:
    """パイプライン1回の実行を識別するrun_idを発行する。"""
    return uuid.uuid4().hex


def save_cost_record(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    stage: str,
    backend: str,
    tokens_in: int,
    tokens_out: int,
    cost_jpy: float,
    latency_ms: float,
    degraded: bool,
    resolved_model: str,
    fallback_level: int,
    timeout: bool = False,
    guardrail_blocked: bool = False,
) -> None:
    conn.execute(
        """
        INSERT INTO cost_records
            (run_id, stage, backend, tokens_in, tokens_out, cost_jpy,
             latency_ms, degraded, resolved_model, fallback_level, timeout, guardrail_blocked)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            stage,
            backend,
            tokens_in,
            tokens_out,
            cost_jpy,
            latency_ms,
            int(degraded),
            resolved_model,
            fallback_level,
            int(timeout),
            int(guardrail_blocked),
        ),
    )
    conn.commit()


def save_pipeline_result(conn: sqlite3.Connection, run_id: str, result: dict[str, list]) -> None:
    """design.md §6.4「承認UI」向けに、pipeline.run() の戻り値を永続化する。

    result は pipeline.run() の戻り値の形（changes/requirements/impacts/proposals）。
    requirements は承認UIで使わないので保存しない。change_id・impact_id
    （pipeline.py側の文字列id）から、この関数内で払い出した行IDへの対応表を
    作りながら insert する（外部キーはAUTOINCREMENTの行ID）。
    """
    change_row_id_by_label: dict[str, int] = {}
    for change in result["changes"]:
        cursor = conn.execute(
            """
            INSERT INTO changes (run_id, label, old_path, old_text, new_path, new_text, kind, summary)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                change.id,
                change.old_section.path if change.old_section else None,
                change.old_section.text if change.old_section else None,
                change.new_section.path,
                change.new_section.text,
                change.kind,
                change.summary,
            ),
        )
        change_row_id_by_label[change.id] = cursor.lastrowid

    impact_row_id_by_id: dict[str, int] = {}
    for impact in result["impacts"]:
        evidence = impact.evidence[0] if impact.evidence else None
        cursor = conn.execute(
            """
            INSERT INTO impacts
                (run_id, change_row_id, sitedoc_item_id, sitedoc_item_key, verdict, rationale,
                 confidence, evidence_path, evidence_quote, evidence_offset)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                change_row_id_by_label[impact.change_id],
                impact.sitedoc_item_id,
                impact.sitedoc_item_key,
                impact.verdict,
                impact.rationale,
                impact.confidence,
                evidence.notice_section_path if evidence else None,
                evidence.quote if evidence else None,
                evidence.offset if evidence else None,
            ),
        )
        impact_row_id_by_id[impact.id] = cursor.lastrowid

    for proposal in result["proposals"]:
        conn.execute(
            """
            INSERT INTO proposals (run_id, impact_row_id, before, after, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                run_id,
                impact_row_id_by_id[proposal.impact_id],
                proposal.before,
                proposal.after,
                proposal.status,
            ),
        )
    conn.commit()


_PROPOSAL_REVIEW_QUERY = """
SELECT
    proposals.row_id, proposals.before, proposals.after, proposals.status,
    impacts.sitedoc_item_id, impacts.sitedoc_item_key,
    impacts.verdict, impacts.rationale, impacts.confidence,
    impacts.evidence_path, impacts.evidence_quote, impacts.evidence_offset,
    changes.label, changes.kind, changes.summary,
    changes.old_path, changes.old_text, changes.new_path, changes.new_text
FROM proposals
JOIN impacts ON impacts.row_id = proposals.impact_row_id
JOIN changes ON changes.row_id = impacts.change_row_id
WHERE proposals.run_id = ?
ORDER BY proposals.row_id
"""

_PROPOSAL_COLUMNS = (
    "row_id",
    "before",
    "after",
    "status",
    "sitedoc_item_id",
    "sitedoc_item_key",
    "verdict",
    "rationale",
    "confidence",
    "evidence_path",
    "evidence_quote",
    "evidence_offset",
    "change_label",
    "change_kind",
    "change_summary",
    "old_path",
    "old_text",
    "new_path",
    "new_text",
)


def list_proposals_for_review(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    """design.md §3 G2「根拠が辿れる」・承認UIの一覧表示用に、更新案を関連情報つきで返す。

    1行に Proposal・Impact・Change を結合済みで持たせ、承認UIが原文トレース
    （evidence_quote / old_text / new_text）へ**1操作（同じ行の展開）で到達**できるようにする。
    """
    rows = conn.execute(_PROPOSAL_REVIEW_QUERY, (run_id,)).fetchall()
    return [dict(zip(_PROPOSAL_COLUMNS, row)) for row in rows]


def get_proposal_for_review(conn: sqlite3.Connection, proposal_row_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        _PROPOSAL_REVIEW_QUERY.replace("WHERE proposals.run_id = ?", "WHERE proposals.row_id = ?"),
        (proposal_row_id,),
    ).fetchone()
    return dict(zip(_PROPOSAL_COLUMNS, row)) if row else None


def list_reviewed_precedents_for_item(
    conn: sqlite3.Connection, item_key: str, *, limit: int = 3
) -> list[dict[str, Any]]:
    """design.md §6.7(b)・ledger N-8: 同じチェックリスト項目について過去に人間がレビュー済みの
    判断を、新しい突合判定への弱い参考情報として返す
    （新しいベクトルDB等は使わず、既存のimpacts/proposalsをそのまま再利用する）。

    ledger R-88: 突き合わせは不変ID（`sitedoc_item_key`）で行う。ラベルで引くと、
    更新案が承認されて文言が変わった瞬間に過去の判断を見失う。ただし不変ID導入前に
    記録された行は key が NULL なので、ラベル一致も併せて拾う（移行期間の互換）。

    `未承認`（人間がまだ見ていない）は判断とみなさないため除外する。
    """
    rows = conn.execute(
        """
        SELECT proposals.status, changes.summary
        FROM proposals
        JOIN impacts ON impacts.row_id = proposals.impact_row_id
        JOIN changes ON changes.row_id = impacts.change_row_id
        WHERE (impacts.sitedoc_item_key = ?
               OR (impacts.sitedoc_item_key IS NULL AND impacts.sitedoc_item_id = ?))
          AND proposals.status != '未承認'
        ORDER BY proposals.row_id DESC
        LIMIT ?
        """,
        (item_key, item_key, limit),
    ).fetchall()
    return [{"status": row[0], "change_summary": row[1]} for row in rows]


def get_proposal_run_id(conn: sqlite3.Connection, proposal_row_id: int) -> str | None:
    """更新案の所属 run_id。存在しない更新案なら None（呼び出し側が404にする）。"""
    row = conn.execute("SELECT run_id FROM proposals WHERE row_id = ?", (proposal_row_id,)).fetchone()
    return row[0] if row else None


def run_exists(conn: sqlite3.Connection, run_id: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM proposals WHERE run_id = ? LIMIT 1", (run_id,)).fetchone()
        is not None
    )


def list_applied_proposals(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """承認・編集済の更新案を、全 run 分まとめて古い順に返す（日報入力のチェックリスト改定用）。

    同じ項目への更新案が複数あるときは、新しい（row_idが大きい）ものが後から適用される。
    """
    rows = conn.execute(
        _PROPOSAL_REVIEW_QUERY.replace(
            "WHERE proposals.run_id = ?", "WHERE proposals.status IN ('承認', '編集済')"
        )
    ).fetchall()
    return [dict(zip(_PROPOSAL_COLUMNS, row)) for row in rows]


def cancel_pending_deliveries_for_proposal(conn: sqlite3.Connection, proposal_row_id: int) -> int:
    """更新案が却下・再編集されたとき、その更新案を含む「送信待ち」を取り消す。

    送信済みのものは取り消せない（取り消すと嘘の履歴になる）ので触らない。
    戻り値は取り消した件数。
    """
    cancelled = 0
    for row in conn.execute(
        "SELECT row_id, proposal_row_ids FROM delivery_queue WHERE status = 'pending'"
    ).fetchall():
        if proposal_row_id in json.loads(row[1]):
            conn.execute("UPDATE delivery_queue SET status = 'cancelled' WHERE row_id = ?", (row[0],))
            cancelled += 1
    conn.commit()
    return cancelled


def has_sent_delivery_for_proposal(conn: sqlite3.Connection, proposal_row_id: int) -> bool:
    for (ids,) in conn.execute("SELECT proposal_row_ids FROM delivery_queue WHERE status = 'sent'"):
        if proposal_row_id in json.loads(ids):
            return True
    return False


def update_proposal_status(
    conn: sqlite3.Connection, proposal_row_id: int, status: str, after: str | None = None
) -> None:
    """design.md §6.4 Proposal.status（未承認|承認|却下|編集済）を更新する。

    after を渡した場合（編集済）は文言も一緒に更新する。
    """
    if after is not None:
        conn.execute(
            "UPDATE proposals SET status = ?, after = ?, decided_at = datetime('now') WHERE row_id = ?",
            (status, after, proposal_row_id),
        )
    else:
        conn.execute(
            "UPDATE proposals SET status = ?, decided_at = datetime('now') WHERE row_id = ?",
            (status, proposal_row_id),
        )
    conn.commit()


def save_fax_delivery(
    conn: sqlite3.Connection, *, run_id: str, proposal_row_ids: list[int], file_path: str
) -> None:
    """design.md §5.3 即時チャネルの配信履歴（監査ログ）を1件記録する。

    FAX様式PDFはリクエストのたびに再生成されるため、「いつ・どのproposalを含んだPDFを
    作ったか」はこのテーブルにしか残らない（ユーザー指摘・2026-08-11、ledger R-76）。
    """
    conn.execute(
        "INSERT INTO fax_deliveries (run_id, proposal_row_ids, file_path) VALUES (?, ?, ?)",
        (run_id, json.dumps(proposal_row_ids), file_path),
    )
    conn.commit()


def list_fax_deliveries(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT row_id, proposal_row_ids, file_path, generated_at "
        "FROM fax_deliveries WHERE run_id = ? ORDER BY row_id DESC",
        (run_id,),
    ).fetchall()
    return [
        {
            "row_id": row[0],
            "proposal_row_ids": json.loads(row[1]),
            "file_path": row[2],
            "generated_at": row[3],
        }
        for row in rows
    ]


_NIPPOU_COLUMNS = (
    "row_id",
    "request_id",
    "subject_pseudonym",
    "visited_on",
    "gate_status",
    "checklist_version",
    "policy_version",
    "items",
    "evaluation",
    "procedures",
    "soap",
    "created_at",
)


def save_nippou_record(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    subject_pseudonym: str,
    visited_on: str,
    gate_status: str,
    checklist_version: str,
    policy_version: str,
    items: list[dict[str, Any]],
    procedures: list[str],
    evaluation: dict[str, Any] | None = None,
    soap: dict[str, Any] | None = None,
) -> int:
    """日報・SOAPの構造化結果を1件保存する（月報の集計材料）。

    dataclassではなくdict/listで受けるのは、store.py が moegi.nippou を import すると
    nippou → pipeline → store の循環になるため。呼び出し側が dataclasses.asdict() で
    ほどいてから渡す。

    ⚠️ 渡してよいのは Privacy Gate を通過した仮名化済みの構造化結果だけ。
       文字起こし原文を渡す引数はこの関数に存在しない。
    """
    cursor = conn.execute(
        """
        INSERT INTO nippou_records
            (request_id, subject_pseudonym, visited_on, gate_status, checklist_version,
             policy_version, items, evaluation, procedures, soap)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            request_id,
            subject_pseudonym,
            visited_on,
            gate_status,
            checklist_version,
            policy_version,
            json.dumps(items, ensure_ascii=False),
            json.dumps(evaluation, ensure_ascii=False) if evaluation is not None else None,
            json.dumps(procedures, ensure_ascii=False),
            json.dumps(soap, ensure_ascii=False) if soap is not None else None,
        ),
    )
    conn.commit()
    return cursor.lastrowid


def _nippou_row_to_dict(row: tuple) -> dict[str, Any]:
    record = dict(zip(_NIPPOU_COLUMNS, row))
    record["items"] = json.loads(record["items"])
    record["procedures"] = json.loads(record["procedures"])
    record["evaluation"] = json.loads(record["evaluation"]) if record["evaluation"] else None
    record["soap"] = json.loads(record["soap"]) if record["soap"] else None
    return record


def list_nippou_records_for_month(
    conn: sqlite3.Connection, *, subject_pseudonym: str, year: int, month: int
) -> list[dict[str, Any]]:
    """指定した利用者（安定仮名）の、指定年月の日報をすべて返す（訪問日の昇順）。"""
    rows = conn.execute(
        f"SELECT {', '.join(_NIPPOU_COLUMNS)} FROM nippou_records "  # noqa: S608 — 列名は定数
        "WHERE subject_pseudonym = ? AND visited_on LIKE ? "
        "ORDER BY visited_on, row_id",
        (subject_pseudonym, f"{year:04d}-{month:02d}-%"),
    ).fetchall()
    return [_nippou_row_to_dict(row) for row in rows]


def list_nippou_months(conn: sqlite3.Connection, subject_pseudonym: str) -> list[str]:
    """その利用者の日報が存在する年月（"YYYY-MM"）を新しい順で返す。"""
    rows = conn.execute(
        "SELECT DISTINCT substr(visited_on, 1, 7) FROM nippou_records "
        "WHERE subject_pseudonym = ? ORDER BY 1 DESC",
        (subject_pseudonym,),
    ).fetchall()
    return [row[0] for row in rows]


_DELIVERY_COLUMNS = (
    "row_id",
    "run_id",
    "channel",
    "recipient",
    "subject",
    "body",
    "proposal_row_ids",
    "status",
    "attempts",
    "last_error_code",
    "created_at",
    "sent_at",
)


def save_delivery(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    channel: str,
    recipient: str,
    subject: str,
    body: str,
    proposal_row_ids: list[int],
) -> int:
    """配信キューに1件積む（design.md G8・ledger R-89）。

    積んだ時点では status='pending'。実際に送るのは delivery.flush()。
    """
    cursor = conn.execute(
        """
        INSERT INTO delivery_queue (run_id, channel, recipient, subject, body, proposal_row_ids)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (run_id, channel, recipient, subject, body, json.dumps(proposal_row_ids)),
    )
    conn.commit()
    return cursor.lastrowid


def _delivery_row_to_dict(row: tuple) -> dict[str, Any]:
    record = dict(zip(_DELIVERY_COLUMNS, row))
    record["proposal_row_ids"] = json.loads(record["proposal_row_ids"])
    return record


def list_pending_deliveries(conn: sqlite3.Connection, *, limit: int = 100) -> list[dict[str, Any]]:
    """送信待ちを古い順に返す（キューなので先入れ先出し）。"""
    rows = conn.execute(
        f"SELECT {', '.join(_DELIVERY_COLUMNS)} FROM delivery_queue "  # noqa: S608 — 列名は定数
        "WHERE status = 'pending' ORDER BY row_id LIMIT ?",
        (limit,),
    ).fetchall()
    return [_delivery_row_to_dict(row) for row in rows]


def count_pending_deliveries(conn: sqlite3.Connection) -> int:
    """「送信待ち n件」バッジ（G8）の n。"""
    return conn.execute("SELECT COUNT(*) FROM delivery_queue WHERE status = 'pending'").fetchone()[0]


def list_deliveries(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {', '.join(_DELIVERY_COLUMNS)} FROM delivery_queue "  # noqa: S608 — 列名は定数
        "ORDER BY row_id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_delivery_row_to_dict(row) for row in rows]


def mark_delivery_sent(conn: sqlite3.Connection, row_id: int) -> None:
    conn.execute(
        "UPDATE delivery_queue SET status = 'sent', attempts = attempts + 1, "
        "last_error_code = NULL, sent_at = datetime('now') WHERE row_id = ?",
        (row_id,),
    )
    conn.commit()


def mark_delivery_attempt_failed(conn: sqlite3.Connection, row_id: int, error_code: str) -> None:
    """送信に失敗した。**status は pending のまま**にする（G8: 消えたのではなく待っている）。

    error_code は語彙が決まった短い文字列だけを渡すこと（例外メッセージを入れない。
    宛先や本文が混じり得るため——design/moegi/privacy.md §10）。
    """
    conn.execute(
        "UPDATE delivery_queue SET attempts = attempts + 1, last_error_code = ? WHERE row_id = ?",
        (error_code, row_id),
    )
    conn.commit()


def save_privacy_audit(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    gate_status: str,
    mode: str,
    policy_version: str,
    detected_counts: dict[str, int],
    needs_human_review: bool,
    residual_risk: str,
    reason_codes: list[str],
    duration_ms: float,
    proceeded_to_structuring: bool,
    site_id: str = "",
    local_model: str = "",
    external_transmission: bool = False,
) -> None:
    """仮名化処理の監査記録を1件残す（design/moegi/privacy.md §10）。

    引数を明示的なキーワードだけに絞ってあるのは、呼び出し側が誤って本文
    （原文・仮名化済みテキスト）を渡せないようにするため。**args/kwargs で
    受けるとログ許可リストが意味を失う。
    """
    conn.execute(
        """
        INSERT INTO privacy_audits
            (request_id, site_id, gate_status, mode, policy_version, detected_counts,
             needs_human_review, residual_risk, reason_codes, duration_ms, local_model,
             proceeded_to_structuring, external_transmission)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            request_id,
            site_id,
            gate_status,
            mode,
            policy_version,
            json.dumps(detected_counts, ensure_ascii=False),
            int(needs_human_review),
            residual_risk,
            json.dumps(reason_codes, ensure_ascii=False),
            duration_ms,
            local_model,
            int(proceeded_to_structuring),
            int(external_transmission),
        ),
    )
    conn.commit()


def list_privacy_audits(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, Any]]:
    """監査記録を新しい順に返す（本文は元々入っていないのでそのまま画面に出せる）。"""
    rows = conn.execute(
        """
        SELECT request_id, site_id, gate_status, mode, policy_version, detected_counts,
               needs_human_review, residual_risk, reason_codes, duration_ms, local_model,
               proceeded_to_structuring, external_transmission, occurred_at
        FROM privacy_audits ORDER BY row_id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    columns = (
        "request_id",
        "site_id",
        "gate_status",
        "mode",
        "policy_version",
        "detected_counts",
        "needs_human_review",
        "residual_risk",
        "reason_codes",
        "duration_ms",
        "local_model",
        "proceeded_to_structuring",
        "external_transmission",
        "occurred_at",
    )
    results = []
    for row in rows:
        record = dict(zip(columns, row))
        record["detected_counts"] = json.loads(record["detected_counts"])
        record["reason_codes"] = json.loads(record["reason_codes"])
        results.append(record)
    return results


def record_from_llm_result(conn: sqlite3.Connection, run_id: str, stage: str, result: dict) -> None:
    """llm.LLMAdapter.complete() の戻り値からCostRecordを1件保存する。"""
    usage = result.get("usage") or {}
    save_cost_record(
        conn,
        run_id=run_id,
        stage=stage,
        backend=result["backend"],
        tokens_in=usage.get("prompt_tokens", 0),
        tokens_out=usage.get("completion_tokens", 0),
        cost_jpy=result["cost_jpy"],
        latency_ms=result["latency_ms"],
        degraded=result["degraded"],
        resolved_model=result.get("resolved_model", ""),
        fallback_level=result.get("fallback_level", 0),
        timeout=result.get("timeout", False),
        guardrail_blocked=result.get("guardrail_blocked", False),
    )
