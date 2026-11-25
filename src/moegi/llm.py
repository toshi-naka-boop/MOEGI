"""LLMアダプタ層（design.md §6.1）。

パイプラインはORCAのSDKに直結せず、必ずこのモジュールを経由する。

契約:
    complete(stage, prompt, opts) -> {text, usage, cost_jpy, backend, degraded, timeout,
                                       latency_ms, resolved_model, fallback_level}
    stage    : "diff" | "requirement" | "matching" | "draft"
    backend  : "orca:<model>" | "local:<model>"
    degraded : このアダプタ自身がローカルへフォールバックした場合 True
    timeout  : ローカル呼び出しがtimeoutした場合 True（v10・design.md §6.6）。
               この場合 text は空文字列。呼び出し元は「判定不能」として扱うこと
    guardrail_blocked : ORCA Guardrails（ゲートウェイ側の第二防衛線・ledger R-91）が
               入力を遮断し、ローカルへフォールバックした場合 True。skip-retryのため
               cloud_fallback_model は試さない。circuit breakerも増やさない（ゲートウェイ
               自体は健全に応答しているため）

接続情報（design.md §14）:
    ORCA_BASE_URL — 未設定なら DEFAULT_ORCA_BASE_URL。末尾の /v1 の有無はどちらでもよい
    ORCA_API_KEY  — 未設定なら macOSキーチェーン（service=KEYCHAIN_SERVICE）から取得する。
                    キー本文はログ・例外メッセージ・戻り値のどこにも出さない

design.md §6.1「ルーティングの第一原則」（v13・ledger N-11）:
    要配慮個人情報（患者の医療・看護情報）を扱う工程は、設定に関わらずローカル固定
    （LOCAL_ONLY_STAGESで指定。現状は空——matching/draftはSiteDoc/ChecklistItemの
    項目ラベルしか扱わず要配慮個人情報を含まないためクラウド既定に変更した）。
    設定でクラウドを指定されても拒否するのは LOCAL_ONLY_STAGES に載っている工程だけ。

design.md §6.6「障害時の状態遷移」（v14・ledger R-79で3段に拡張）:
    - 各クラウド呼び出しにtimeout（実測の3倍・上限20秒）。超過で即座に次段へフォールバック
    - 主モデル呼び出しが失敗したら、cloud_fallback_model（設定されていれば）でORCAに
      再試行する。ORCAはAnthropic/Gemini/Qwen等を1エンドポイントで呼び分けるルーターで
      （実測確認済み・ledger R-79）、主モデルだけがレート制限等で落ちているケースでは
      ローカル7Bより高品質な結果を維持できる。この再試行が成功した場合は
      「ORCAへの到達性」自体は健全と判断し、circuit breakerの失敗カウントは増やさない
    - 主モデル・cloud_fallback_model の両方が失敗して初めてローカルへフォールバックし、
      その時点でcircuit breakerの失敗カウントを1つ増やす（ORCAエンドポイント自体が
      不調である可能性を示すのはこのケースだけのため）
    - 連続N回失敗でcircuit breakerを開き、以降はcloud_fallback_modelも試さずローカル直行
      （毎回2回ぶんのtimeoutを待たない）
    - breakerはcooldown経過後、次の1回だけ試行を許す（半開）
    - v10: ローカル呼び出し自体がtimeoutした場合はフォールバック先が無い。例外を上げず、
      text=""・timeout=Trueを付けて結果を返す（呼び出し元が「判定不能」として続行する）。
      工程ごとの出力トークン上限（models.tomlのmax_output_tokens）はこれを未然に防ぐ側の対策
"""

from __future__ import annotations

import os
import subprocess
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

STAGES = ("diff", "requirement", "matching", "draft", "nippou", "pseudonymize")

# design.md §6.1 第一原則（v13・ledger N-11）: ローカル固定が必要なのは要配慮個人情報
# （患者の医療・看護情報）を扱う工程だけ。SiteDoc/ChecklistItemは項目ラベルのテンプレート
# 定義であり患者データを含まないため、matching/draftは他のクラウド工程と同じ扱いにする
# （v12までは事業所文書の有無だけでこの2工程を強制ローカルにしていたが、その制約は廃止）。
# nippou（日報エージェントのstructure()）は患者の実際の看護記録を扱う工程なので
# LOCAL_ONLY_STAGESに残す（design.md §6.5・nippou.py）。
# pseudonymize（仮名化のローカルLLM補完検出）は**まだ仮名化されていない発話**を
# 扱うため、nippouより更に強い理由でローカル固定（design/moegi/privacy.md §5.3）。
LOCAL_ONLY_STAGES: frozenset[str] = frozenset({"nippou", "pseudonymize"})

# models.toml にセクションが無くても既定値で動く工程。既存の設定ファイル・テストを
# 壊さずに工程を増やすための逃げ道で、既定値は必ずローカル固定側に倒す。
OPTIONAL_STAGES: frozenset[str] = frozenset({"pseudonymize"})
_OPTIONAL_STAGE_DEFAULTS: dict[str, dict[str, Any]] = {
    "pseudonymize": {
        "backend": "local",
        "model": "gemma4:26b",
        "reasoning_effort": "none",
        "max_output_tokens": 1200,
        "local_timeout_seconds": 120.0,
    },
}

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = _REPO_ROOT / "config" / "models.toml"

# design.md §6.6: クラウドtimeoutは実測の3倍・上限20秒。
# 「実測」はプロセス内でのEMA（指数移動平均）で近似する。初回呼び出し用の初期値は保守的に設定。
CLOUD_TIMEOUT_CAP_SECONDS = 20.0
CLOUD_TIMEOUT_MULTIPLIER = 3.0
CLOUD_LATENCY_SEED_SECONDS = 5.0
_LATENCY_EMA_ALPHA = 0.3  # 新しい実測値の重み

# design.md §6.6: 連続N回失敗でcircuit breakerを開く
CIRCUIT_BREAKER_THRESHOLD = 3
CIRCUIT_BREAKER_COOLDOWN_SECONDS = 30.0

LOCAL_CALL_TIMEOUT_SECONDS = 60.0  # ローカルはフォールバック先そのものなのでtimeoutは緩め

DEFAULT_ORCA_BASE_URL = "https://api.orcarouter.ai/v1"
# 認証情報の解決順: 環境変数 > macOSキーチェーン。
# シェルのexport継承に頼ると、起動経路（CC経由／ターミナル直／スクリプト）の違いで
# 当日いきなり認証が切れる。キーチェーンを最後の拠り所に置いてその事故を構造的に消す。
KEYCHAIN_SERVICE = "orca-api"


class ConfigError(ValueError):
    """models.toml の設定不備（第一原則違反など）。"""


class OrcaCredentialsMissing(RuntimeError):
    """ORCA_BASE_URL / ORCA_API_KEY が未設定（design.md: 接続情報未入手の間はこの経路を通す）。"""


class OrcaUnavailable(RuntimeError):
    """circuit breakerが開いていて、ORCAへの呼び出し自体を試みなかった。"""


class OrcaGuardrailBlocked(RuntimeError):
    """ORCA Guardrails が入力を遮断した（HTTP 400 / error code `guardrail_blocked`）。

    ledger R-91: ORCAゲートウェイ側の第二防衛線。ゲートウェイはこの応答を
    skip-retry（同じプロンプトを再送しても同じ判定）としてマークするため、
    呼び出し側は cloud_fallback_model への再試行を**行ってはいけない**
    （同一キー＝同一ガードレールで、また遮断されるだけ）。
    このメッセージにプロンプト本文・検出値は決して含めない。
    """


@dataclass
class StageConfig:
    backend: str  # "orca" | "local"
    model: str
    fallback_backend: str = "local"
    fallback_model: str = ""
    price_jpy_per_1k_in: float = 0.0
    price_jpy_per_1k_out: float = 0.0
    max_output_tokens: int | None = None  # design.md §6.6: 工程ごとの出力トークン上限
    local_timeout_seconds: float | None = None
    reasoning_effort: str | None = None  # opus批評C-11・ledger N-11: gemma4:26b等の
    # reasoningモデルは、これを付けないと出力がreasoningトークンに全消費されcontentが
    # 空になる（実測: max_tokens=900で21秒・本文0字 → "none"指定で10秒・正しいJSON）。
    cloud_fallback_model: str = ""  # ledger R-79: 主モデル失敗時にローカルへ落ちる前に
    # ORCA経由で試す別プロバイダ（例: "orcarouter/auto"）。空文字なら2段（従来どおり
    # 主モデル→ローカル）のまま。LOCAL_ONLY_STAGES（backend="local"固定）には無関係。


def load_model_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """config/models.toml を読み込む。LOCAL_ONLY_STAGESの工程にorcaが指定されていたら拒否する。

    単価は ORCA の単価表どおり $/1M で書き、円/1k への換算はここ1箇所で行う
    （usd_jpy_rate は前提。変えれば §6.3 の原価対比表がまとめて追従する）。
    """
    with open(path, "rb") as f:
        data = tomllib.load(f)

    dev_local_only = bool(data.get("dev_local_only", True))
    batch_matching = bool(data.get("batch_matching", True))
    usd_jpy_rate = float(data.get("usd_jpy_rate", 0.0))
    stages: dict[str, StageConfig] = {}
    for stage in STAGES:
        raw = data.get(stage)
        if raw is None and stage in OPTIONAL_STAGES:
            raw = _OPTIONAL_STAGE_DEFAULTS[stage]
        if raw is None:
            raise ConfigError(f"models.toml に [{stage}] セクションがありません")
        cfg = StageConfig(
            backend=raw["backend"],
            model=raw["model"],
            fallback_backend=raw.get("fallback_backend", "local"),
            fallback_model=raw.get("fallback_model", raw["model"] if raw["backend"] == "local" else ""),
            price_jpy_per_1k_in=float(raw.get("price_usd_per_1m_in", 0.0)) * usd_jpy_rate / 1000,
            price_jpy_per_1k_out=float(raw.get("price_usd_per_1m_out", 0.0)) * usd_jpy_rate / 1000,
            max_output_tokens=(
                int(raw["max_output_tokens"]) if raw.get("max_output_tokens") is not None else None
            ),
            local_timeout_seconds=(
                float(raw["local_timeout_seconds"])
                if raw.get("local_timeout_seconds") is not None
                else None
            ),
            reasoning_effort=raw.get("reasoning_effort"),
            cloud_fallback_model=raw.get("cloud_fallback_model", ""),
        )
        if stage in LOCAL_ONLY_STAGES and cfg.backend != "local":
            raise ConfigError(
                f'[{stage}] は要配慮個人情報を含む工程のため設計上ローカル固定です（design.md §6.1 v13）。'
                f' backend = "{cfg.backend}" は設定できません。'
            )
        stages[stage] = cfg

    return {
        "dev_local_only": dev_local_only,
        "batch_matching": batch_matching,
        "usd_jpy_rate": usd_jpy_rate,
        "stages": stages,
    }


def _api_key_from_keychain() -> str:
    """macOSキーチェーンからORCA APIキーを取得する。取得できなければ空文字を返す。

    キー本文は戻り値以外のどこにも出さない。security の stderr も握り潰す
    （失敗理由をログに流すと、環境によってはキーの断片が混じり得るため）。
    """
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return ""  # macOS以外・security不在・タイムアウト
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _orca_chat_completions_url(base_url: str) -> str:
    """ORCAのチャット補完URLを組み立てる。

    ORCAが案内するエンドポイントは `https://api.orcarouter.ai/v1` と `/v1` 込みだが、
    `/v1` を含まないホスト表記で渡されることもある。どちらで渡されても
    `/v1/v1/...` にならないよう、末尾の `/v1` をいったん剥がしてから付け直す。
    """
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return f"{base}/v1/chat/completions"


def _extract_usage(data: dict) -> dict:
    usage = data.get("usage") or {}
    return {
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
    }


class LLMAdapter:
    """design.md §6.1 のLLMアダプタ層本体。工程ごとのルーティング・原価計測・障害時フォールバックを持つ。"""

    def __init__(
        self,
        config_path: str | Path = DEFAULT_CONFIG_PATH,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        cfg = load_model_config(config_path)
        self._dev_local_only: bool = cfg["dev_local_only"]
        self._stages: dict[str, StageConfig] = cfg["stages"]
        # design.md §7「投入単位と突合のバッチ化」(v11)。単発モードはデバッグ用に残す。
        self.batch_matching: bool = cfg["batch_matching"]

        self._orca_base_url = os.environ.get("ORCA_BASE_URL") or DEFAULT_ORCA_BASE_URL
        # キーチェーン参照は初回のORCA呼び出しまで遅延させる（ローカル固定で走る
        # 開発・テスト時に許可ダイアログを出さないため）。
        self._orca_api_key_cache: str | None = None
        self._ollama_base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")

        self._client = http_client if http_client is not None else httpx.Client()
        self._owns_client = http_client is None

        # circuit breaker状態（ORCA接続そのものの健全性。工程横断で共有する）
        self._consecutive_failures = 0
        self._breaker_open_until: float | None = None
        # timeout算出用のEMA（プロセス内の実測値）
        self._cloud_latency_ema_seconds = CLOUD_LATENCY_SEED_SECONDS
        #: ローカルLLMへ接続できなかった（または応答が読めなかった）回数。
        #: 呼び出し側（Web画面）が「AIに接続できない」と案内するために読む。
        self.local_unavailable_count = 0
        #: ローカル呼び出しのうち、タイムアウトした回数・呼び出し総数（画面が「判定不能だらけ」を検知する）
        self.local_timeout_count = 0
        self.local_call_count = 0

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "LLMAdapter":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- 公開API -----------------------------------------------------------

    def complete(self, stage: str, prompt: str, opts: dict | None = None) -> dict:
        """design.md §6.1 の契約どおり complete(stage, prompt, opts) -> dict を実装する。"""
        if stage not in STAGES:
            raise ValueError(f"未知のstage: {stage!r}（{STAGES} のいずれかを指定すること）")
        opts = opts or {}
        stage_cfg = self._stages[stage]
        # design.md §6.6: 工程ごとの出力トークン上限・ローカルtimeout
        # （呼び出し側がoptsで明示指定していれば優先）
        if stage_cfg.max_output_tokens is not None and "max_tokens" not in opts:
            opts = {**opts, "max_tokens": stage_cfg.max_output_tokens}
        if stage_cfg.local_timeout_seconds is not None and "local_timeout" not in opts:
            opts = {**opts, "local_timeout": stage_cfg.local_timeout_seconds}
        if stage_cfg.reasoning_effort is not None and "reasoning_effort" not in opts:
            opts = {**opts, "reasoning_effort": stage_cfg.reasoning_effort}

        # 第一原則: LOCAL_ONLY_STAGES所属、またはdev_local_only中、または設定自体がlocalなら常にローカル。
        # ここでは「設定を信用せず毎回判定する」ことが重要（第一原則を実装で守るため）。
        if stage in LOCAL_ONLY_STAGES or self._dev_local_only or stage_cfg.backend == "local":
            model = stage_cfg.model if stage_cfg.backend == "local" else stage_cfg.fallback_model
            return self._call_local(model, prompt, opts, degraded=False, fallback_level=0)

        # ここに来るのはbackend="orca"かつdev_local_only=falseの場合（既定では全工程）。
        # fallback_level: 0=クラウド主モデル成功 / 1=cloud_fallback_model成功 /
        # 2=主・cloud_fallback共に失敗してローカルへ / 3=breakerが開いておりどちらも試さずローカルへ
        if self._breaker_is_open():
            return self._call_local(
                stage_cfg.fallback_model, prompt, opts, degraded=True, fallback_level=3
            )

        try:
            return self._call_orca(
                stage_cfg.model,
                prompt,
                opts,
                price_jpy_per_1k_in=stage_cfg.price_jpy_per_1k_in,
                price_jpy_per_1k_out=stage_cfg.price_jpy_per_1k_out,
            )
        except OrcaGuardrailBlocked:
            # ledger R-91: ゲートウェイの第二防衛線が発火。skip-retryのため
            # cloud_fallback_model は試さない（同一キー＝同一ガードレール）。
            # ローカルならデータは事業所外へ出ないので、完走優先（§6.6）でローカルへ落とす。
            # ゲートウェイ自体は健全に応答しているので circuit breaker は増やさない。
            result = self._call_local(
                stage_cfg.fallback_model, prompt, opts, degraded=True, fallback_level=2
            )
            result["guardrail_blocked"] = True
            return result
        except (httpx.HTTPError, OrcaCredentialsMissing):
            pass  # 主モデル失敗。ledger R-79: ローカルに落ちる前にORCA別プロバイダを試す

        if stage_cfg.cloud_fallback_model:
            try:
                result = self._call_orca(
                    stage_cfg.cloud_fallback_model,
                    prompt,
                    opts,
                    # orcarouter/auto等は解決先モデルが実行時まで不明で単価表が引けないため
                    # 0円扱い（ローカルのcost_jpy=0と同じ簡略化）。
                    price_jpy_per_1k_in=0.0,
                    price_jpy_per_1k_out=0.0,
                )
                result["degraded"] = True
                result["fallback_level"] = 1
                return result
            except OrcaGuardrailBlocked:
                result = self._call_local(
                    stage_cfg.fallback_model, prompt, opts, degraded=True, fallback_level=2
                )
                result["guardrail_blocked"] = True
                return result
            except (httpx.HTTPError, OrcaCredentialsMissing):
                pass  # 別プロバイダも失敗＝ORCAエンドポイント自体が不調の可能性

        # 主モデル・cloud_fallback_model（設定されていれば）の両方が失敗した場合のみ
        # 「ORCAへの到達性」に疑いありと記録する（片方の成功はエンドポイント健全の証拠）。
        self._record_cloud_failure()
        return self._call_local(
            stage_cfg.fallback_model, prompt, opts, degraded=True, fallback_level=2
        )

    def complete_raw(self, backend: str, model: str, prompt: str, opts: dict | None = None) -> dict:
        """backend/modelを直接指定して呼び出す（stageルーティングを経由しない）。

        scripts/compare_models.py のような候補モデル比較用途に限定して使うこと。
        design.md §6.1の第一原則（LOCAL_ONLY_STAGESのローカル固定）はstage単位の制約なので、
        ここでは適用対象外（呼び出し元が明示的にbackendを選ぶ）。
        """
        opts = opts or {}
        if backend == "local":
            return self._call_local(model, prompt, opts, degraded=False, fallback_level=0)
        if backend == "orca":
            if self._breaker_is_open():
                raise OrcaUnavailable("circuit breakerが開いています")
            try:
                return self._call_orca(model, prompt, opts, price_jpy_per_1k_in=0.0, price_jpy_per_1k_out=0.0)
            except (httpx.HTTPError, OrcaCredentialsMissing):
                self._record_cloud_failure()
                raise
        raise ValueError(f"未知のbackend: {backend!r}")

    # -- 内部実装 -----------------------------------------------------------

    def _call_orca(
        self,
        model: str,
        prompt: str,
        opts: dict,
        *,
        price_jpy_per_1k_in: float,
        price_jpy_per_1k_out: float,
    ) -> dict:
        api_key = self._resolve_api_key()
        if not api_key:
            # 環境変数にもキーチェーンにも無い。実際に接続を試みず即座にこの例外を投げる。
            raise OrcaCredentialsMissing(
                "ORCA APIキーが見つかりません"
                f"（環境変数 ORCA_API_KEY / キーチェーン service={KEYCHAIN_SERVICE} のいずれにも無い）"
            )

        timeout = min(self._cloud_latency_ema_seconds * CLOUD_TIMEOUT_MULTIPLIER, CLOUD_TIMEOUT_CAP_SECONDS)
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": opts.get("temperature", 0),
        }
        if "max_tokens" in opts:
            body["max_tokens"] = opts["max_tokens"]

        start = time.monotonic()
        response = self._client.post(
            _orca_chat_completions_url(self._orca_base_url),
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
            timeout=timeout,
        )
        # ledger R-91: ORCA Guardrails の遮断は HTTP 400 + error code `guardrail_blocked`。
        # 通常のHTTPエラー（フォールバック対象）と区別する。判定に失敗したら通常エラー扱い。
        if getattr(response, "status_code", None) == 400:
            try:
                error_code = (response.json().get("error") or {}).get("code", "")
            except Exception:  # noqa: BLE001 — 本文がJSONでない400は通常エラーとして扱う
                error_code = ""
            if error_code == "guardrail_blocked":
                raise OrcaGuardrailBlocked("ORCA Guardrailsが入力を遮断しました")
        response.raise_for_status()
        latency_ms = (time.monotonic() - start) * 1000

        # 呼び出しに成功したのでbreaker/EMAを更新
        self._consecutive_failures = 0
        self._breaker_open_until = None
        self._cloud_latency_ema_seconds = (
            1 - _LATENCY_EMA_ALPHA
        ) * self._cloud_latency_ema_seconds + _LATENCY_EMA_ALPHA * (latency_ms / 1000)

        try:
            data = response.json()
            text = data["choices"][0]["message"]["content"]
            usage = _extract_usage(data)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            # 200なのに本文の形が想定外。HTTPErrorとして扱い、呼び出し元の
            # フォールバック（別プロバイダ→ローカル）に乗せる。
            raise httpx.DecodingError("ORCAの応答の形式が想定外です") from exc

        # design.md §6.1: レスポンスヘッダを全呼び出しで必ず記録する
        resolved_model = response.headers.get("X-Orca-Resolved-Model", model)
        try:
            fallback_level = int(response.headers.get("X-Orca-Fallback-Level", "0"))
        except ValueError:
            fallback_level = 0

        cost_jpy = (
            usage["prompt_tokens"] / 1000 * price_jpy_per_1k_in
            + usage["completion_tokens"] / 1000 * price_jpy_per_1k_out
        )

        return {
            "text": text,
            "usage": usage,
            "cost_jpy": cost_jpy,
            "backend": f"orca:{model}",
            "degraded": False,
            "timeout": False,
            "guardrail_blocked": False,
            "latency_ms": latency_ms,
            "resolved_model": resolved_model,
            "fallback_level": fallback_level,
        }

    def _call_local(
        self, model: str, prompt: str, opts: dict, *, degraded: bool, fallback_level: int
    ) -> dict:
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": opts.get("temperature", 0),
        }
        if "max_tokens" in opts:
            body["max_tokens"] = opts["max_tokens"]
        if "reasoning_effort" in opts:
            # opus批評C-11・ledger N-11: reasoningモデル（gemma4:26b等）は、これが無いと
            # 出力トークン予算がreasoningに全消費されcontentが空になる（実測確認済み）。
            body["reasoning_effort"] = opts["reasoning_effort"]

        local_timeout = opts.get("local_timeout", LOCAL_CALL_TIMEOUT_SECONDS)
        start = time.monotonic()
        self.local_call_count += 1
        try:
            response = self._client.post(
                f"{self._ollama_base_url.rstrip('/')}/v1/chat/completions",
                json=body,
                timeout=local_timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # design.md §6.6（v10）: ローカルはフォールバック先が無い。例外を上げると
            # パイプラインごと落ちるため、timeout=Trueを付けて返す。呼び出し元が
            # 当該項目を「判定不能」として記録し、パイプラインを続行させる。
            latency_ms = (time.monotonic() - start) * 1000
            if isinstance(exc, httpx.TimeoutException):
                self.local_timeout_count += 1
            else:
                self.local_unavailable_count += 1
            return {
                "text": "",
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                "cost_jpy": 0.0,
                "backend": f"local:{model}",
                "degraded": True,
                "timeout": True,
                # 接続拒否・HTTPエラー（Ollama停止など）はtimeoutと同じく「判定不能」で返す。
                # unavailable=True なら呼び出し元は「AIに接続できない」と案内できる。
                "unavailable": not isinstance(exc, httpx.TimeoutException),
                "guardrail_blocked": False,
                "latency_ms": latency_ms,
                "resolved_model": model,
                "fallback_level": fallback_level,
            }
        latency_ms = (time.monotonic() - start) * 1000

        try:
            data = response.json()
            text = data["choices"][0]["message"]["content"] or ""
            usage = _extract_usage(data)
        except (KeyError, IndexError, TypeError, ValueError):
            self.local_unavailable_count += 1
            return {
                "text": "",
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                "cost_jpy": 0.0,
                "backend": f"local:{model}",
                "degraded": True,
                "timeout": True,
                "unavailable": True,
                "guardrail_blocked": False,
                "latency_ms": latency_ms,
                "resolved_model": model,
                "fallback_level": fallback_level,
            }

        return {
            "text": text,
            "usage": usage,
            "cost_jpy": 0.0,  # design.md §6.3: ローカルは電気代のみ
            "backend": f"local:{model}",
            "degraded": degraded,
            "timeout": False,
            "guardrail_blocked": False,
            "latency_ms": latency_ms,
            "resolved_model": model,
            "fallback_level": fallback_level,
        }

    def _resolve_api_key(self) -> str:
        """環境変数 > キーチェーンの順でAPIキーを解決し、プロセス内でキャッシュする。"""
        if self._orca_api_key_cache is None:
            self._orca_api_key_cache = os.environ.get("ORCA_API_KEY") or _api_key_from_keychain()
        return self._orca_api_key_cache

    def _breaker_is_open(self) -> bool:
        if self._breaker_open_until is None:
            return False
        if time.monotonic() >= self._breaker_open_until:
            # cooldown経過。次の1回は試行を許す（半開）
            self._breaker_open_until = None
            self._consecutive_failures = 0
            return False
        return True

    def _record_cloud_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
            self._breaker_open_until = time.monotonic() + CIRCUIT_BREAKER_COOLDOWN_SECONDS
