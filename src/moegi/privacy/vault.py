"""仮名対応表の保管（design/moegi/privacy.md §9）。

原文DB（data/moegi.db）とは**別のSQLiteファイル**に置く。同じDBに入れてしまうと、
「更新案を見るためのDB接続」がそのまま対応表への到達経路になる。物理的に分ける方が、
アクセス権の付け外しをファイル単位でできて確実。

保存時暗号化: AES-256-GCM（cryptographyパッケージ。独自方式は作らない）。
鍵の解決順:
    1. 環境変数 MOEGI_VAULT_KEY（base64url・32バイト）
    2. 鍵ファイル data/.vault_key（無ければ生成・パーミッション0600）
ソースコードにもDBにも鍵は書かない。鍵ファイルは data/ 配下＝.gitignore済み。

このモジュールは対応表の平文を返す関数（decrypt_mapping）を持つが、webapp.py からは
呼ばない。再結合が必要な場合はローカル環境の管理者がCLIから実行する運用にしてある
（画面に実名を出す経路を作らない＝権限のない利用者に元の値が見えない）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sqlite3
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from moegi import paths

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_VAULT_DB_PATH = paths.data_dir() / "privacy_vault.db"
DEFAULT_KEY_PATH = paths.data_dir() / ".vault_key"

KEY_ENV_VAR = "MOEGI_VAULT_KEY"
_KEY_BYTES = 32  # AES-256
_NONCE_BYTES = 12  # GCM推奨長

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pseudonym_maps (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL UNIQUE,
    site_id TEXT NOT NULL DEFAULT '',
    policy_version TEXT NOT NULL,
    nonce BLOB NOT NULL,
    ciphertext BLOB NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pseudonym_maps_created_at ON pseudonym_maps(created_at);
"""


class VaultError(RuntimeError):
    """対応表の保存・読み出しに失敗した（呼び出し側は fail-closed で扱うこと）。

    メッセージには対応表の中身も鍵も含めない。
    """


def _load_or_create_key(key_path: Path = DEFAULT_KEY_PATH) -> bytes:
    """鍵を解決する。環境変数 > 鍵ファイル（無ければ生成）。

    鍵ファイルは所有者のみ読み書き（0600）で作る。既存ファイルのパーミッションが
    緩い場合は締め直す——「作った時は正しかったが後で緩んだ」を放置しない。
    """
    env_value = os.environ.get(KEY_ENV_VAR)
    if env_value:
        try:
            key = base64.urlsafe_b64decode(env_value)
        except (ValueError, TypeError) as exc:
            raise VaultError(f"{KEY_ENV_VAR} がbase64として解釈できません") from exc
        if len(key) != _KEY_BYTES:
            raise VaultError(f"{KEY_ENV_VAR} は{_KEY_BYTES}バイトの鍵をbase64で指定すること")
        return key

    if key_path.exists():
        key = base64.urlsafe_b64decode(key_path.read_text(encoding="utf-8").strip())
        if len(key) != _KEY_BYTES:
            raise VaultError("鍵ファイルの内容が不正です（長さが合いません）")
        try:
            key_path.chmod(0o600)
        except OSError:
            pass  # Windows等でchmodが効かない環境でも動作自体は止めない
        return key

    key = AESGCM.generate_key(bit_length=_KEY_BYTES * 8)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    # 生成と同時に0600にする（一瞬でも0644で存在させない）
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, base64.urlsafe_b64encode(key))
    finally:
        os.close(fd)
    return key


def stable_pseudonym(
    code: str, *, prefix: str = "PATIENT", key_path: Path = DEFAULT_KEY_PATH
) -> str:
    """事業所内の利用者コードから、日をまたいで安定した仮名を導出する。

    月報（1ヶ月分の日報の集約）には「同じ人の記録をまとめる鍵」が要る。しかし
    Privacy Gate が採番する `PATIENT_001` は1リクエスト内でしか一貫しないため、
    別に安定した鍵が必要になる。

    設計:
      - 鍵つきHMAC-SHA256。鍵を持たない者はコードから仮名を作れないし、仮名から
        コードを逆算することもできない（総当たりを鍵で塞ぐのが平文ハッシュとの違い。
        利用者コードは桁数が少なく、素のSHA256だと総当たりで逆引きできてしまう）
      - 同じコードからは常に同じ仮名が出る（決定論的＝月報の集計が再現できる）
      - 鍵は仮名対応表と同じもの。**鍵が変わると過去の日報と結びつかなくなる**ので、
        鍵ファイルは対応表DBと同じ重みで保全すること
    """
    key = _load_or_create_key(key_path)
    digest = hmac.new(key, f"{prefix}:{code}".encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{prefix}_{digest[:12]}"


def connect(db_path: str | Path = DEFAULT_VAULT_DB_PATH) -> sqlite3.Connection:
    """対応表DBに接続し、テーブルが無ければ作る。DBファイルは0600にする。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return conn


def save_mapping(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    mapping: dict[str, str],
    policy_version: str,
    site_id: str = "",
    key_path: Path = DEFAULT_KEY_PATH,
) -> None:
    """仮名 → 実名 の対応表を暗号化して保存する。

    同じ request_id で2回呼ばれた場合は上書きする（再検査でやり直したケース）。
    保存に失敗したら例外を上げる——呼び出し側はこれを fail-closed の理由にする
    （「対応表が残らないまま仮名化テキストだけが下流へ進む」を許さない）。
    """
    try:
        key = _load_or_create_key(key_path)
        plaintext = json.dumps(mapping, ensure_ascii=False).encode("utf-8")
        nonce = os.urandom(_NONCE_BYTES)
        # AAD に request_id を結びつけ、別レコードの暗号文をすり替えても復号できないようにする
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, request_id.encode("utf-8"))
        conn.execute(
            """
            INSERT INTO pseudonym_maps (request_id, site_id, policy_version, nonce, ciphertext)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(request_id) DO UPDATE SET
                site_id = excluded.site_id,
                policy_version = excluded.policy_version,
                nonce = excluded.nonce,
                ciphertext = excluded.ciphertext
            """,
            (request_id, site_id, policy_version, nonce, ciphertext),
        )
        conn.commit()
    except VaultError:
        raise
    except Exception:  # noqa: BLE001 — 例外の中身を外へ出さないための再送出
        raise VaultError("仮名対応表の保存に失敗しました") from None


def decrypt_mapping(
    conn: sqlite3.Connection, request_id: str, *, key_path: Path = DEFAULT_KEY_PATH
) -> dict[str, str] | None:
    """対応表を復号して返す（ローカル環境の管理者用。画面からは呼ばない）。"""
    row = conn.execute(
        "SELECT nonce, ciphertext FROM pseudonym_maps WHERE request_id = ?", (request_id,)
    ).fetchone()
    if row is None:
        return None
    key = _load_or_create_key(key_path)
    try:
        plaintext = AESGCM(key).decrypt(row[0], row[1], request_id.encode("utf-8"))
    except Exception:  # noqa: BLE001 — 鍵違い・改ざんの区別を外に出さない
        raise VaultError("仮名対応表の復号に失敗しました") from None
    return json.loads(plaintext.decode("utf-8"))


def delete_mapping(conn: sqlite3.Connection, request_id: str) -> int:
    """1リクエスト分の対応表を削除する（§9「削除処理を実装する」）。"""
    cursor = conn.execute("DELETE FROM pseudonym_maps WHERE request_id = ?", (request_id,))
    conn.commit()
    return cursor.rowcount


def purge_expired(conn: sqlite3.Connection, retention_days: int) -> int:
    """保存期間を過ぎた対応表を削除する（§9「保存期間を設定可能にする」）。"""
    cursor = conn.execute(
        "DELETE FROM pseudonym_maps WHERE created_at < datetime('now', ?)",
        (f"-{int(retention_days)} days",),
    )
    conn.commit()
    return cursor.rowcount


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM pseudonym_maps").fetchone()[0]
