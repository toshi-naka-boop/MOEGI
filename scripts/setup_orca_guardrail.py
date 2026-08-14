"""ORCA Guardrails のプロビジョニング（ledger R-91・design/moegi/privacy.md §9）。

MOEGIのクラウド経路（②差分抽出・③-a要件抽出・③-b突合・④更新案生成）に対する
**第二防衛線**をORCAゲートウェイ側に設定する。第一防衛線はローカルの Privacy Gate
（privacy/）で、これは代替ではない——万一クラウド行きのプロンプトへ識別子・秘密情報が
混入した場合に、事業所の外（ゲートウェイ）でもう一度止めるための層。

使い方:
    # ポリシー定義とサンドボックス検証（コンソールのアクセストークンが必要）
    ORCA_CONSOLE_TOKEN=... .venv/bin/python scripts/setup_orca_guardrail.py

    # トークンが無い場合は、コンソールに貼るルールJSONと手順を印字するだけ
    .venv/bin/python scripts/setup_orca_guardrail.py

⚠️ 認証について（実測・2026-08-15）:
    管理API（/api/guardrail/*）はリレー用APIキー（sk-orca-...）では認証できない
    （HTTP 200だが {"message":"Unauthorized, invalid access token"}）。
    コンソール（https://www.orcarouter.ai/console）のアクセストークンが必要で、
    これはブラウザでのログインでしか取得できない。トークンはこのスクリプトの
    引数・出力・ログのどこにも保存しない。

⚠️ 正規表現はRE2（線形時間・後方参照なし・**lookaround不可**）。
    privacy/rules.py のPythonパターンは lookbehind を使っているためそのまま流用できず、
    ここでRE2安全な形に書き直している。テスト（tests/test_guardrail_policy.py）が
    lookaround・後方参照の混入を機械的に検査する。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

MANAGEMENT_BASE_URL = os.environ.get("ORCA_MANAGEMENT_BASE_URL", "https://api.orcarouter.ai")
CONSOLE_TOKEN_ENV = "ORCA_CONSOLE_TOKEN"

GUARDRAIL_NAME = "moegi-cloud-boundary"

# ---------------------------------------------------------------------------
# ポリシー定義（このスクリプトの本体。コンソールに貼る場合もこのJSONが正）
#
# 方針は privacy/rules.py の仮名化ポリシー（design/moegi/privacy.md §6）と同じ:
#   - 秘密情報（APIキー・JWT・AWSキー）と保険証・診察券番号 → block
#   - 連絡先（メール・電話・IP）・利用者ID・和暦生年月日 → mask（MOEGIと同じ型付きタグ）
# 通達本文・チェックリスト項目ラベルにはこれらが現れないはずなので、
# 発火した時点で「想定外の混入」＝監査対象（Matches feed に残る）。
# ---------------------------------------------------------------------------

#: 日本向けカスタム検出器（RE2安全・**mask対象のみ**）。ORCAの組み込みentityは
#: 米国中心（ssn・credit_card等）なので、日本の識別子はここで補う。
#:
#: ⚠️ block対象（保険証・診察券番号）は**ここに入れない**（JP_BLOCK_PATTERN が唯一の担当）。
#: コンソール実測（2026-08-15）: ルールは**逐次実行**で、mask側に同じ対象の検出器があると
#: 先に走ったmaskが番号を書き換え、後段のblock regexが原文を見られず発火しない。
#: 「多重防御としてmask側にも残す」案はこの実測で否定された。
CUSTOM_ENTITIES = [
    {
        "name": "jp_patient_id",
        "pattern": r"(利用者ID|利用者番号|患者ID|患者番号|利用者コード)[ 　:：＝=]{0,3}[0-9０-９A-Za-z\-−]{2,24}",
        "mask_with": "[[PATIENT_ID]]",
    },
    {
        # 日本の電話番号（ハイフン区切りのみ。lookaround無しで裸の11桁を拾うと
        # 誤検出が多すぎるため、第二防衛線としては区切りありに限定する）
        "name": "jp_phone",
        "pattern": r"0[0-9]{1,4}(-|−|ー|－)[0-9]{1,4}(-|−|ー|－)[0-9]{3,4}",
        "mask_with": "[[PHONE]]",
    },
    {
        # 郵便番号（〒必須。lookaround無しで7桁数字を拾うと誤検出が多すぎる）
        "name": "jp_postal_code",
        "pattern": r"〒[ 　]{0,1}[0-9０-９]{3}(-|−|ー|－)[0-9０-９]{4}",
        "mask_with": "[[POSTAL_CODE]]",
    },
    {
        # 和暦の生年月日
        "name": "jp_wareki_birthdate",
        "pattern": r"(明治|大正|昭和|平成|令和)(元|[0-9]{1,2})年[0-9]{1,2}月[0-9]{1,2}日",
        "mask_with": "[[BIRTHDATE]]",
    },
]

#: block対象（保険証・診察券番号）をまとめた正規表現。
#: ⚠️ per-entity override（entity_actions）は**組み込みエンティティ専用**で、カスタム
#: エンティティには適用できない（コンソール実測・2026-08-15）。そのためblockしたい
#: カスタム2種は「正規表現ルール（action=block）」として別ルールに分離する。
JP_BLOCK_PATTERN = (
    r"(被保険者証番号|被保険者番号|保険証番号|保険者番号|記号番号|診察券番号|診察券|カルテ番号|カルテNo)"
    r"[ 　:：＝=.]{0,3}[0-9０-９A-Za-z\-−]{4,24}"
)

#: ルール本体。stage=input（クラウドへ出るリクエストの遮断が目的。outputは通達の
#: 更新案文言でPIIが出る余地が薄く、streaming時のmask未対応も踏まえ input に絞る）。
#:
#: ⚠️ 順序について（コンソール実測・2026-08-15）: ルールは**逐次実行**（先に走った
#: maskの結果を後段が見る）。コンソールUIはルールの並び替えができないため、
#: 「blockを先頭に」には頼らず、**mask側からblock対象の検出器を外す**構成にした
#: （どの順序でもblock regexが原文を見られる）。
RULES = [
    {
        # 保険証・診察券番号はブロック（ローカルの仮名化ポリシー §6 と同方針）
        "type": "regex",
        "stage": "input",
        "action": "block",
        "pattern": JP_BLOCK_PATTERN,
    },
    {
        "type": "pii",
        "stage": "input",
        "action": "mask",
        # ORCA組み込みentity: メール・電話(米式)・IPはmask、キー・JWTはblock
        # （entity_actionsは組み込みエンティティにのみ有効）
        "entities": ["email", "phone", "ip", "api_key_openai", "aws_access_key", "jwt"],
        "entity_actions": {
            "api_key_openai": "block",
            "aws_access_key": "block",
            "jwt": "block",
        },
        # ⚠️ block対象（保険証・診察券番号）の検出器をここに混ぜないこと。
        # ルールは逐次実行なので、maskが先に走る並びだと番号が書き換わり、
        # block regexが原文を見られず発火しなくなる（コンソール実測・2026-08-15）。
        "custom_entities": CUSTOM_ENTITIES,
    },
]


def build_guardrail_payload(*, mode: str = "enforce") -> dict:
    """ガードレール定義を組み立てる。

    mode="observe" は全アクションを flag に落とす（ORCAの段階導入・enforcement-modes
    ドキュメントの observe 段階。Privacy Gate の observe/shadow/enforce と同じ思想）。
    """
    rules = json.loads(json.dumps(RULES))  # deep copy
    if mode == "observe":
        for rule in rules:
            rule["action"] = "flag"
            if "entity_actions" in rule:
                rule["entity_actions"] = {k: "flag" for k in rule["entity_actions"]}
    return {
        "name": GUARDRAIL_NAME,
        "enabled": True,
        # 既定ガードレールにはしない（本番トラフィックへの適用はコンソールでキーに
        # 紐づけた瞬間に始まる。その最後の1操作は人が明示的に行う）
        "is_default": False,
        "rules": rules,
    }


#: サンドボックス検証（POST /api/guardrail/test は何も永続化しない）に使う合成サンプル。
#: 実在の個人情報は含まない。expect は「発火すべきか」。
SANDBOX_SAMPLES = [
    ("被保険者証番号 12345678 を確認した", True, "保険証番号 → block"),
    ("連絡先は 090-1234-5678 です", True, "日本の電話番号 → mask"),
    ("kazoku@example.com へ送付", True, "メール → mask"),
    ("昭和18年5月3日生まれ", True, "和暦生年月日 → mask"),
    (
        "指定訪問看護は、1回あたり30分から1時間30分を標準として行うものとする。",
        False,
        "通達本文（本来の入力）→ 発火しない",
    ),
    ("血圧128/78、体温36.5度、脈拍70", False, "バイタル → 発火しない"),
]


def _request(method: str, path: str, token: str, body: dict | None = None) -> tuple[int, dict]:
    url = f"{MANAGEMENT_BASE_URL}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
            return resp.status, payload
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            payload = {}
        return exc.code, payload


def _is_unauthorized(status: int, payload: dict) -> bool:
    if status in (401, 403):
        return True
    message = str(payload.get("message", "")).lower()
    return payload.get("success") is False and "unauthorized" in message


def print_manual_instructions(payload: dict) -> None:
    print(
        f"""
────────────────────────────────────────────────────────────────
 コンソールのアクセストークンが無いため、API経由の作成は行いません。
 以下をコンソールで実施してください（5分・docs.orcarouter.ai/features/guardrails 参照）:

 1. https://www.orcarouter.ai/console/guardrails → New guardrail
    名前: {GUARDRAIL_NAME}
 2. ルールを**2本・この順序で**設定（①regex/Block ②PII/Mask。順序が重要）:
""".rstrip()
    )
    print(json.dumps(payload["rules"], ensure_ascii=False, indent=2))
    print(
        f"""
 3. Test タブで検証（下のサンプルが期待どおり発火するか）:
      発火する: 「被保険者証番号 12345678 を確認した」→ block
      発火する: 「連絡先は 090-1234-5678 です」→ mask
      発火しない: 通達本文・バイタル値
 4. 保存 → /console/token でAPIキーの Guardrail に {GUARDRAIL_NAME} を選択
    （この操作をした瞬間から全クラウド呼び出しに適用されます）

 適用後のクライアント側挙動（実装済み・llm.py）:
   遮断は HTTP 400 guardrail_blocked → cloud_fallbackを試さずローカルへ
   フォールバックし、cost_records.guardrail_blocked=1 で監査に残ります。
────────────────────────────────────────────────────────────────
""".rstrip()
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="ORCA Guardrails のプロビジョニング（R-91）")
    parser.add_argument(
        "--mode",
        choices=["observe", "enforce"],
        default="enforce",
        help="observe は全ルールを flag（記録のみ）にする段階導入用",
    )
    args = parser.parse_args()

    payload = build_guardrail_payload(mode=args.mode)
    token = os.environ.get(CONSOLE_TOKEN_ENV, "").strip()
    if not token:
        print_manual_instructions(payload)
        return 0

    # 1) 既存を探す（冪等: 同名があれば更新、無ければ作成）
    status, listing = _request("GET", "/api/guardrail/", token)
    if _is_unauthorized(status, listing):
        print(f"認証に失敗しました（{CONSOLE_TOKEN_ENV} が無効）。手動手順を表示します。")
        print_manual_instructions(payload)
        return 1
    existing = next(
        (
            g
            for g in (listing.get("data") or listing.get("guardrails") or [])
            if isinstance(g, dict) and g.get("name") == GUARDRAIL_NAME
        ),
        None,
    )

    # 2) サンドボックスで検証（何も永続化されない・アップストリーム呼び出しなし）
    print(f"=== サンドボックス検証（{args.mode}モード） ===")
    failures = 0
    for text, expect_fire, label in SANDBOX_SAMPLES:
        status, result = _request(
            "POST",
            "/api/guardrail/test",
            token,
            {"rules": payload["rules"], "stage": "input", "text": text},
        )
        fired = bool(
            result.get("matched")
            or result.get("matches")
            or result.get("verdict") not in (None, "allow", "pass")
        )
        ok = fired == expect_fire
        failures += 0 if ok else 1
        print(f"  {'✅' if ok else '❌'} {label}（HTTP {status}）")
    if failures:
        print(f"⚠️ サンドボックス検証 {failures} 件不一致。作成は中止します。")
        return 1

    # 3) 作成 or 更新
    if existing is not None:
        payload["id"] = existing.get("id")
        status, result = _request("PUT", "/api/guardrail/", token, payload)
        action = "更新"
    else:
        status, result = _request("POST", "/api/guardrail/", token, payload)
        action = "作成"
    if status >= 400:
        print(f"❌ ガードレールの{action}に失敗（HTTP {status}）: {result.get('message', '')}")
        return 1
    print(f"✅ ガードレール {GUARDRAIL_NAME} を{action}しました（enabled・is_default=false）")
    print(
        "残り1操作: /console/token でAPIキーの Guardrail にこのポリシーを選択してください"
        "（選択した瞬間から全クラウド呼び出しに適用されます）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
