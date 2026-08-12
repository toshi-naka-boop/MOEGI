"""仮名化・Privacy Gate の設定読み込み（design/moegi/privacy.md）。

modeの変更経路を config/privacy.toml と環境変数 MOEGI_PRIVACY_MODE の2つに限定する。
HTTPリクエストからmodeを受け取る関数はこのモジュールに存在しない——一般利用者が
リクエスト単位で安全検査を無効化できないようにするための構造的な制約であって、
呼び出し側の作法に頼らない。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = _REPO_ROOT / "config" / "privacy.toml"

MODES = ("observe", "shadow", "enforce")
RISK_LEVELS = ("low", "medium", "high")

#: modeを上書きできる唯一の環境変数（管理者が起動時に指定する運用）。
MODE_ENV_VAR = "MOEGI_PRIVACY_MODE"


class PrivacyConfigError(ValueError):
    """privacy.toml の設定不備。"""


@dataclass(frozen=True)
class PrivacyConfig:
    policy_version: str
    mode: str
    max_input_chars: int
    llm_detection: bool
    max_residual_risk: str
    max_removal_ratio: float
    retention_days: int
    dictionary_path: Path

    def risk_exceeds_threshold(self, risk: str) -> bool:
        """residual_risk が許容値を超えているか。未知の値は「超えている」とみなす（fail-closed）。"""
        if risk not in RISK_LEVELS:
            return True
        return RISK_LEVELS.index(risk) > RISK_LEVELS.index(self.max_residual_risk)


def load_privacy_config(path: str | Path = DEFAULT_CONFIG_PATH) -> PrivacyConfig:
    """config/privacy.toml を読み込む。

    mode は環境変数 MOEGI_PRIVACY_MODE が設定されていればそちらを優先する
    （管理者が起動時に段階導入を切り替えるための経路）。未知のmode・risk値は
    例外にする——「設定ミスで検査が緩む」のが一番危ないので、静かに既定値へ
    落とさず起動時に落とす。
    """
    with open(path, "rb") as f:
        data = tomllib.load(f)

    mode = os.environ.get(MODE_ENV_VAR) or str(data.get("mode", "enforce"))
    if mode not in MODES:
        raise PrivacyConfigError(f"mode は {MODES} のいずれかを指定すること（指定値: {mode!r}）")

    max_residual_risk = str(data.get("max_residual_risk", "low"))
    if max_residual_risk not in RISK_LEVELS:
        raise PrivacyConfigError(
            f"max_residual_risk は {RISK_LEVELS} のいずれかを指定すること（指定値: {max_residual_risk!r}）"
        )

    dictionary_path = Path(str(data.get("dictionary_path", "config/pseudonym_dictionary_sample.toml")))
    if not dictionary_path.is_absolute():
        dictionary_path = _REPO_ROOT / dictionary_path

    return PrivacyConfig(
        policy_version=str(data.get("policy_version", "unversioned")),
        mode=mode,
        max_input_chars=int(data.get("max_input_chars", 20000)),
        llm_detection=bool(data.get("llm_detection", True)),
        max_residual_risk=max_residual_risk,
        max_removal_ratio=float(data.get("max_removal_ratio", 0.5)),
        retention_days=int(data.get("retention_days", 90)),
        dictionary_path=dictionary_path,
    )
