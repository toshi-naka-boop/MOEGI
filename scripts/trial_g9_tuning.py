"""G9再現率の改善トライアル（design.md §3 N-9・半日タイムボックス・1回限り）。

ledger N-9のユーザー決定: 「few-shot強化＋14B級ローカルモデル1本の比較を1回だけ実施。
改善すれば採用、しなくても§6.3対比表の材料として記録」。

ベースライン（single-local, config/goldset.toml）は再現率0.0%（0/6）だった。
このスクリプトは本番のプロンプト・設定には触れず、突合(matching)工程だけを
差し替えて同じゴールドセットで測り直す。改善が確認できた場合のみ、
pipeline.py / models.toml へ反映するかどうかを別途判断する。

差し替えの内容:
  - モデル: 7shi/tanuki-dpo-v1.0（6.2GB）→ gemma4:26b（18GB、既にOllamaに導入済み。
    14B級として指定された候補が未導入のため、既存の最大モデルで代替）
  - プロンプト: _MATCH_ITEM_PROMPT に検証済み引用の具体例を1件追加（few-shot）

    python -m scripts.trial_g9_tuning
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from moegi import ingest, llm, pipeline

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ALLOWLIST_PATH = _REPO_ROOT / "config" / "allowlist.toml"
_GOLDSET_PATH = _REPO_ROOT / "config" / "goldset.toml"
_OLD_PDF = _REPO_ROOT / "data" / "notices" / "old_hohatsu0305-12.pdf"
_NEW_PDF = _REPO_ROOT / "data" / "notices" / "new_hohatsu0305-19.pdf"

TRIAL_MODEL = "gemma4:26b"

_FEWSHOT_EXAMPLE = """
【判定例（このとおりの形式・粒度で回答すること）】
現場への要求事項: 「訪問看護記録書に、実際に提供した指定訪問看護の開始時刻及び終了時刻を記載すること」
通達原文（第4の6）: 「...訪問看護ステーションにおける日々の訪問看護利用者氏名、訪問場所、訪問時間（実際の指定訪問看護の開始時刻及び終了時刻）及び訪問人数等について記録し、保管しておくこと。」
判定対象のチェックリスト項目: 「訪問開始・終了時刻の記録」
出力: {"verdict": "要更新", "rationale": "既存項目は一般的な開始終了時刻の記録に留まり、『実際の指定訪問看護の』という限定が文言に無いため。", "confidence": 0.7, "evidence_quote": "訪問時間（実際の指定訪問看護の開始時刻及び終了時刻）"}
（evidence_quoteは通達原文を一字一句そのまま抜き出すこと。要約・言い換え・改行の追加をしないこと。）
"""

_FEWSHOT_MARKER = "\x00FEWSHOT\x00"
_MATCH_ITEM_PROMPT_FEWSHOT_TEMPLATE = pipeline._MATCH_ITEM_PROMPT.replace(
    "【出力形式】", _FEWSHOT_MARKER + "\n【出力形式】"
)


def _build_fewshot_prompt(**kwargs) -> str:
    # few-shot例の中に生のJSON中括弧が入っているため、.format()には渡さず
    # プレースホルダをformat後に置換する（KeyErrorを避ける）。
    return _MATCH_ITEM_PROMPT_FEWSHOT_TEMPLATE.format(**kwargs).replace(_FEWSHOT_MARKER, _FEWSHOT_EXAMPLE)


def _load_goldset() -> dict[str, dict]:
    with open(_GOLDSET_PATH, "rb") as f:
        data = tomllib.load(f)
    return {
        c["label"]: {"kind": c["kind"], "items": {i["item_label"]: i["verdict"] for i in c["items"]}}
        for c in data["changes"]
    }


def main() -> None:
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
    goldset = _load_goldset()

    cells = []
    with llm.LLMAdapter() as adapter:  # dev_local_only=trueの実物設定をそのまま使う
        for target in targets:
            label = target["label"]
            gold = goldset.get(label)
            if gold is None:
                continue
            change = pipeline.build_changes(
                {label: old_sections.get(label)}, {label: new_sections[label]}
            )[0]
            # ③-a要件抽出も本番と同じ手順で実行し、その出力をそのまま突合に渡す
            # （requirement_textを手で作ると、本番のパイプラインとは別物を測ることになる）。
            pipeline.extract_change_summaries([change], adapter, run_id="trial")
            requirement_text = pipeline.extract_requirements([change], adapter, run_id="trial")[0].text

            for item_label, gold_verdict in gold["items"].items():
                item = next(i for i in sitedoc.items if i.label == item_label)
                prompt = _build_fewshot_prompt(
                    item_label=item.label,
                    requirement_text=requirement_text,
                    path=change.new_section.path,
                    notice_text=change.new_section.text,
                )
                result = adapter.complete_raw("local", TRIAL_MODEL, prompt, {"local_timeout": 180.0})
                predicted = "影響なし"
                if not result.get("timeout"):
                    entry = pipeline._extract_json_object(result["text"], pipeline._MATCH_ITEM_EXPECTED_KEYS)
                    if entry and entry.get("verdict") == "要更新":
                        predicted = "要更新" if pipeline._verified_evidence(entry, change) else "影響なし"
                    elif entry is None:
                        predicted = "判定不能"
                cells.append({"label": label, "item": item_label, "gold": gold_verdict, "predicted": predicted})
                print(f"  {label}/{item_label[:20]}: gold={gold_verdict} pred={predicted}", flush=True)

    positives = [c for c in cells if c["gold"] == "要更新"]
    measurable = [c for c in positives if c["predicted"] != "判定不能"]
    tp = [c for c in measurable if c["predicted"] == "要更新"]
    recall = len(tp) / len(measurable) if measurable else None
    negatives = [c for c in cells if c["gold"] == "影響なし"]
    fp = [c for c in negatives if c["predicted"] == "要更新"]
    print(f"\n=== トライアル結果（{TRIAL_MODEL} + few-shot） ===")
    print(f"再現率: {recall:.1%}" if recall is not None else "再現率: 測定不能")
    print(f"誤検出率: {len(fp)/len(negatives):.1%}" if negatives else "")
    print(f"見逃し: {[c['label']+'/'+c['item'] for c in positives if c['predicted']=='影響なし']}")


if __name__ == "__main__":
    main()
