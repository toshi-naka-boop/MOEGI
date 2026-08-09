"""原価・実測レコードの永続化（design.md §6.4 CostRecord）。SQLiteに保存する。

design.md §6.4:
    CostRecord 原価 { run_id, stage, backend, tokens_in, tokens_out, cost_jpy,
                      latency_ms, degraded }

これに加えて、llm.py が全呼び出しで記録する resolved_model / fallback_level
（design.md §6.1）、および timeout（design.md §6.6・v10。ローカル呼び出しが
タイムアウトした場合のフラグ。残さないと⑥の実測にも障害分析にも使えない）
も一緒に永続化する。
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

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
"""


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """SQLiteに接続し、テーブルが無ければ作成する。

    既存DB（timeout列が無い旧スキーマ）に対しては ALTER TABLE で列を追加する
    （design.md §6.6・v10で追加された timeout フラグ）。
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(_SCHEMA)
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
