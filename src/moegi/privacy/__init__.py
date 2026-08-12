"""仮名化・安全検査層（design/moegi/privacy.md）。

既存の日報・SOAP構造化処理の**前段**に入る。既存処理のロジックには手を入れず、
「仮名化済みテキストを渡す」「passed でなければ呼ばない」の2点だけで接続する。

    from moegi import privacy

    result = privacy.run_gate(text, config=privacy.load_privacy_config(), adapter=adapter)
    if privacy.cleared_for_downstream(result):
        record = agent.structure(result.sanitized_text)   # 既存処理（無変更）

⚠️ 用語について: このモジュールが行うのは「直接識別子の仮名化」であって匿名化ではない。
   仮名化後も病状・服薬・処置・家族状況などの要配慮個人情報は残る。コード・UI・
   ドキュメントで「匿名化済み」「個人情報ではない」「完全に安全」とは書かないこと。
"""

from moegi.privacy.config import (
    MODE_ENV_VAR,
    MODES,
    PrivacyConfig,
    PrivacyConfigError,
    load_privacy_config,
)
from moegi.privacy.gate import (
    ExternalTransmissionBlocked,
    GateResult,
    PrivacyGateStatus,
    assert_cleared_for_external_transmission,
    cleared_for_downstream,
    cleared_for_external_transmission,
    run_gate,
)
from moegi.privacy.logsafe import (
    ALLOWED_AUDIT_FIELDS,
    audit_fields,
    install_log_sanitizer,
    sanitize,
)

__all__ = [
    "ALLOWED_AUDIT_FIELDS",
    "ExternalTransmissionBlocked",
    "GateResult",
    "MODES",
    "MODE_ENV_VAR",
    "PrivacyConfig",
    "PrivacyConfigError",
    "PrivacyGateStatus",
    "assert_cleared_for_external_transmission",
    "audit_fields",
    "cleared_for_downstream",
    "cleared_for_external_transmission",
    "install_log_sanitizer",
    "load_privacy_config",
    "run_gate",
    "sanitize",
]
