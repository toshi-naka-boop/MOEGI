"""diff工程の候補モデル比較スクリプト（design.md §14 スコープ6）。

同じ差分整形サンプルを、diff工程の候補モデルに順に投げ、
出力・latency・cost_jpy・resolved_model・fallback_levelを並べて表示する。

ORCA接続情報（ORCA_BASE_URL / ORCA_API_KEY）が未設定の候補は、
落ちずに「未設定のためスキップ」と表示する。

使い方:
    /Users/toshixa/MOEGI/.venv/bin/python scripts/compare_models.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from moegi.llm import LLMAdapter, OrcaCredentialsMissing, OrcaUnavailable  # noqa: E402

# design.md §2 c「評価の記録」（第4の4・完全新設）の実データをサンプルに使う。
SAMPLE_PROMPT = """以下は訪問看護に関する厚生労働省通知の条文です。今回の改定で新設された条文であり、旧版には対応する記載がありません。
現場向けに、何が新しく定められたのかを1〜2文の日本語で要約してください。前置きや記号は付けず、要約文のみを出力すること。

【条文パス】第4の4
【新版の条文】
指定訪問看護の提供に当たっては、その目標達成の程度及びその効果等について評価を行い、その内容を訪問看護記録書に記録すること。
"""

# design.md §14 スコープ6: diff工程の候補（未確定）。config/models.tomlのコメントと対応。
CANDIDATES: list[tuple[str, str]] = [
    ("orca", "anthropic/claude-haiku-4.5"),
    ("orca", "google/gemini-3.6-flash"),
    ("orca", "deepseek/deepseek-v4-flash"),
    ("orca", "orcarouter/fusion"),
    ("local", "7shi/tanuki-dpo-v1.0:latest"),
]


def main() -> None:
    adapter = LLMAdapter()
    rows: list[tuple[str, ...]] = []

    for backend, model in CANDIDATES:
        try:
            result = adapter.complete_raw(backend, model, SAMPLE_PROMPT)
        except (OrcaCredentialsMissing, OrcaUnavailable):
            rows.append((backend, model, "未設定のためスキップ", "-", "-", "-", "-"))
            continue
        except Exception as exc:  # noqa: BLE001 - 比較スクリプトなので1候補の失敗で全体を落とさない
            rows.append((backend, model, f"エラー: {exc}", "-", "-", "-", "-"))
            continue

        output_preview = result["text"].strip().replace("\n", " ")[:60]
        rows.append(
            (
                backend,
                model,
                output_preview,
                f"{result['latency_ms']:.0f}ms",
                f"{result['cost_jpy']:.2f}円",
                result["resolved_model"],
                str(result["fallback_level"]),
            )
        )

    adapter.close()

    header = ("backend", "model", "output(先頭60字)", "latency", "cost_jpy", "resolved_model", "fallback_level")
    table = [header, *rows]
    widths = [max(len(str(row[i])) for row in table) for i in range(len(header))]
    for row in table:
        print(" | ".join(str(v).ljust(w) for v, w in zip(row, widths)))


if __name__ == "__main__":
    main()
