"""データの置き場所。環境変数 MOEGI_DATA_DIR で、リポジトリ内の data/ 以外へ切り替えられる。

デモモード（scripts/demo.sh）が、本番用の記録と混ざらないよう別の場所を使うために使う。
通達PDF（data/notices/）だけは取得スクリプトの置き場所なので対象外。
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    override = os.environ.get("MOEGI_DATA_DIR", "").strip()
    return Path(override).expanduser().resolve() if override else REPO_ROOT / "data"
