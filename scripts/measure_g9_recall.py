"""G9再現率の計測（design.md §3 G9・§10 D3）。

config/goldset.toml（人手で付けた正解表）に対し、単発/一括 × クラウド経路/
ローカル経路の4パターンで pipeline.run() を実行し、再現率（見逃さない率）を計る。

    python -m scripts.measure_g9_recall --mode single-local
    python -m scripts.measure_g9_recall --mode single-local,single-cloud,batch-local,batch-cloud
    python -m scripts.measure_g9_recall --mode all

出力: 標準出力にモードごとの集計、--out 指定時は生の判定結果もJSONで書き出す
（design.md §10 D3完了条件「正解表ができ、再現率がクラウド/ローカル両方で出る」の根拠）。

再現率の定義: gold で「要更新」の (change, item) セルのうち、実際に
verdict="要更新" のImpactが生成できた割合。「判定不能」になったセルは
分母から除外し、別途「計測不能率」として報告する（黙って見逃し扱いにしない）。
"""

from __future__ import annotations

import argparse
import json
import re
import tempfile
import tomllib
from pathlib import Path

from moegi import ingest, llm, pipeline, store

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ALLOWLIST_PATH = _REPO_ROOT / "config" / "allowlist.toml"
_MODELS_CONFIG_PATH = _REPO_ROOT / "config" / "models.toml"
_GOLDSET_PATH = _REPO_ROOT / "config" / "goldset.toml"
_OLD_PDF = _REPO_ROOT / "data" / "notices" / "old_hohatsu0305-12.pdf"
_NEW_PDF = _REPO_ROOT / "data" / "notices" / "new_hohatsu0305-19.pdf"

MODES = ("single-local", "single-cloud", "batch-local", "batch-cloud")


def _load_goldset(path: Path = _GOLDSET_PATH) -> dict[str, dict]:
    """{label: {"kind": ..., "items": {item_label: verdict}}} に変換する。"""
    with open(path, "rb") as f:
        data = tomllib.load(f)
    result: dict[str, dict] = {}
    for change in data["changes"]:
        result[change["label"]] = {
            "kind": change["kind"],
            "items": {item["item_label"]: item["verdict"] for item in change["items"]},
        }
    return result


def _make_config_path(tmpdir: str, *, dev_local_only: bool) -> str:
    """実物のmodels.tomlを元に dev_local_only だけ差し替えた一時設定を作る。

    モデル指定・単価・タイムアウトなど他の設定はすべて実物のまま使う
    （測定条件を本番設定からずらさないため）。
    """
    text = _MODELS_CONFIG_PATH.read_text(encoding="utf-8")
    replaced = re.sub(
        r"^dev_local_only\s*=.*$",
        f"dev_local_only = {'true' if dev_local_only else 'false'}",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    out_path = Path(tmpdir) / "models.toml"
    out_path.write_text(replaced, encoding="utf-8")
    return str(out_path)


def _predicted_verdict(impacts: list[pipeline.Impact], item_label: str) -> str:
    """あるchangeのimpacts群から、既存項目item_labelに対する予測ラベルを引く。

    要更新のImpactが無ければ「影響なし」とみなす。これは
    「evidence不成立で捨てられた要更新の試み」も含めて実際の挙動（承認UIに
    何も出ない）に一致させるための定義。
    """
    matches = [i for i in impacts if i.sitedoc_item_id == item_label]
    if any(i.verdict == "要更新" for i in matches):
        return "要更新"
    if any(i.verdict == "判定不能" for i in matches):
        return "判定不能"
    return "影響なし"


def run_mode(
    mode: str, goldset: dict[str, dict], db_path: str | None = None, orchestrate: bool = False
) -> dict:
    batch = mode.startswith("batch")
    dev_local_only = mode.endswith("local")

    targets = ingest.load_allowlist(str(_ALLOWLIST_PATH))
    old_notice = ingest.load_notice(
        str(_OLD_PDF), id="old", title="旧通達", issued_on="", version="old", source_url=""
    )
    new_notice = ingest.load_notice(
        str(_NEW_PDF), id="new", title="新通達", issued_on="", version="new", source_url=""
    )
    old_sections = ingest.apply_allowlist(old_notice, targets)
    new_sections = ingest.apply_allowlist(new_notice, targets)
    sitedoc = pipeline.load_sitedoc()

    cells: list[dict] = []  # 生の判定結果（--out用）
    store_conn = store.connect(db_path) if db_path else None

    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = _make_config_path(tmpdir, dev_local_only=dev_local_only)
        with llm.LLMAdapter(config_path=config_path) as adapter:
            for target in targets:
                label = target["label"]
                gold = goldset.get(label)
                if gold is None:
                    continue
                result = pipeline.run(
                    old_sections=old_sections,
                    new_sections=new_sections,
                    sitedoc=sitedoc,
                    adapter=adapter,
                    run_id=f"g9-{mode}-{label}",
                    labels={label},
                    batch=batch,
                    store_conn=store_conn,
                    orchestrate=orchestrate,
                )
                for item_label, gold_verdict in gold["items"].items():
                    predicted = _predicted_verdict(result["impacts"], item_label)
                    cells.append(
                        {
                            "mode": mode,
                            "label": label,
                            "item": item_label,
                            "gold": gold_verdict,
                            "predicted": predicted,
                        }
                    )
                print(f"  [{mode}] {label} 完了", flush=True)

    return _summarize(mode, cells)


def _summarize(mode: str, cells: list[dict]) -> dict:
    positives = [c for c in cells if c["gold"] == "要更新"]
    measurable_positives = [c for c in positives if c["predicted"] != "判定不能"]
    true_positives = [c for c in measurable_positives if c["predicted"] == "要更新"]
    undecidable_positives = [c for c in positives if c["predicted"] == "判定不能"]

    negatives = [c for c in cells if c["gold"] == "影響なし"]
    measurable_negatives = [c for c in negatives if c["predicted"] != "判定不能"]
    false_positives = [c for c in measurable_negatives if c["predicted"] == "要更新"]

    recall = len(true_positives) / len(measurable_positives) if measurable_positives else None
    false_positive_rate = len(false_positives) / len(measurable_negatives) if measurable_negatives else None

    misses = [c for c in positives if c["predicted"] == "影響なし"]

    return {
        "mode": mode,
        "cells": cells,
        "n_positive_gold": len(positives),
        "n_positive_measurable": len(measurable_positives),
        "n_positive_undecidable": len(undecidable_positives),
        "recall": recall,
        "misses": [f"{c['label']}/{c['item']}" for c in misses],
        "n_negative_gold": len(negatives),
        "n_false_positive": len(false_positives),
        "false_positive_rate": false_positive_rate,
    }


def _print_summary(summary: dict) -> None:
    mode = summary["mode"]
    recall = summary["recall"]
    fpr = summary["false_positive_rate"]
    print(f"\n=== {mode} ===")
    print(
        f"  再現率: {recall:.1%}" if recall is not None else "  再現率: 測定不能（分母0）",
        f"（{summary['n_positive_measurable']}件中 検出 / gold要更新={summary['n_positive_gold']}件"
        f"・判定不能={summary['n_positive_undecidable']}件は分母から除外）",
    )
    if summary["misses"]:
        print(f"  見逃し: {', '.join(summary['misses'])}")
    print(
        f"  誤検出率: {fpr:.1%}" if fpr is not None else "  誤検出率: 測定不能（分母0）",
        f"（gold影響なし={summary['n_negative_gold']}件中）",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        default="all",
        help=f"カンマ区切りで {MODES} のいずれか、または all",
    )
    parser.add_argument("--out", default=None, help="生の判定結果セルをJSONで書き出す先")
    parser.add_argument("--db", default=None, help="LLM生応答をCostRecordとして残すsqliteパス（監査用）")
    parser.add_argument(
        "--orchestrate",
        action="store_true",
        help="design.md §6.7(a)追記・ledger N-8（実験実装）。単発モードで「要更新」候補が"
        "複数出た場合、重複裁定のLLM呼び出しを追加する",
    )
    args = parser.parse_args()

    modes = list(MODES) if args.mode == "all" else args.mode.split(",")
    for m in modes:
        if m not in MODES:
            raise SystemExit(f"未知のmode: {m!r}（{MODES} のいずれか）")

    goldset = _load_goldset()
    summaries = []
    for mode in modes:
        print(f"--- {mode} 実行中 ---", flush=True)
        summary = run_mode(mode, goldset, db_path=args.db, orchestrate=args.orchestrate)
        _print_summary(summary)
        summaries.append(summary)

    if args.out:
        Path(args.out).write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n生データを {args.out} に出力しました")


if __name__ == "__main__":
    main()
