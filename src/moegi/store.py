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
    sitedoc_item_id TEXT,
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
) -> None:
    conn.execute(
        """
        INSERT INTO cost_records
            (run_id, stage, backend, tokens_in, tokens_out, cost_jpy,
             latency_ms, degraded, resolved_model, fallback_level, timeout)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                (run_id, change_row_id, sitedoc_item_id, verdict, rationale, confidence,
                 evidence_path, evidence_quote, evidence_offset)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                change_row_id_by_label[impact.change_id],
                impact.sitedoc_item_id,
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
    impacts.sitedoc_item_id, impacts.verdict, impacts.rationale, impacts.confidence,
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
    conn: sqlite3.Connection, item_label: str, *, limit: int = 3
) -> list[dict[str, Any]]:
    """design.md §6.7(b)・ledger N-8: 同じチェックリスト項目について過去に人間がレビュー済みの
    判断を、新しい突合判定への弱い参考情報として返す。`sitedoc_item_id`をタグとして扱う
    （新しいベクトルDB等は使わず、既存のimpacts/proposalsをそのまま再利用する）。

    `未承認`（人間がまだ見ていない）は判断とみなさないため除外する。
    """
    rows = conn.execute(
        """
        SELECT proposals.status, changes.summary
        FROM proposals
        JOIN impacts ON impacts.row_id = proposals.impact_row_id
        JOIN changes ON changes.row_id = impacts.change_row_id
        WHERE impacts.sitedoc_item_id = ? AND proposals.status != '未承認'
        ORDER BY proposals.row_id DESC
        LIMIT ?
        """,
        (item_label, limit),
    ).fetchall()
    return [{"status": row[0], "change_summary": row[1]} for row in rows]


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
    )
