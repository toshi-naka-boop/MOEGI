#!/usr/bin/env python3
"""Reference eval harness runner for the eval-loop skill.

eval-loop スキルのリファレンス実装。プロジェクトに `eval/run_eval.py` としてコピーして使う。

このファイルは **コピー時点のスナップショット** である。スキル側が更新されても
自動では追従しない。追従するかどうかはプロジェクトの判断でよい（コピー後の
プロジェクト固有の改造の方が優先される）。

依存: PyYAML（必須） / jsonschema（json_schema チェックを使う場合のみ）

使い方:
    python eval/run_eval.py                      # eval/eval-config.yaml を読んで全件実行
    python eval/run_eval.py -c path/to/cfg.yaml
    python eval/run_eval.py --only c001,c007     # ケース ID を絞って実行

終了コード: 全件パスなら 0、1 件でも失敗なら 1。
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

# 失敗カテゴリ（SKILL.md の「失敗カテゴリと対策」表と対応）
CAT_FORMAT = "フォーマット崩れ"
CAT_MISSING = "抽出漏れ"
CAT_MEANING = "解釈違い"
CAT_ERROR = "実行エラー"
CAT_UNKNOWN = "要分析"

# 1 ケースに複数の失敗が出たとき、どのカテゴリを主因とみなすか（上ほど優先）。
# 内訳は「失敗行」ではなく「ケース」で数える（対策はケース単位で打つため）。
CATEGORY_ORDER = [CAT_ERROR, CAT_FORMAT, CAT_MISSING, CAT_MEANING, CAT_UNKNOWN]

# 出力を JSON として解釈できないと実行できないチェック
NEEDS_JSON = {"json_parse", "json_schema", "exact_match"}


def resolve(spec: str):
    """"module.path:func" 形式の文字列から呼び出し可能オブジェクトを解決する。"""
    mod_name, _, attr = spec.partition(":")
    if not attr:
        raise ValueError(f"entrypoint は 'module.path:func' 形式で書くこと: {spec!r}")
    return getattr(importlib.import_module(mod_name), attr)


def load_dataset(path: Path) -> list[dict]:
    cases = []
    with path.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            try:
                cases.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{lineno} が JSON として読めない: {e}") from e
    return cases


def as_obj(output):
    """出力を dict/list に正規化する。パースできなければ None を返す。"""
    if isinstance(output, (dict, list)):
        return output
    if isinstance(output, str):
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return None
    return None


def run_checks(case: dict, output, checks: list[dict], base: Path) -> list[dict]:
    """プログラム的チェックを実行し、失敗の一覧を返す（空なら合格）。"""
    failures = []
    obj = as_obj(output)

    # パース不能は 1 件だけ記録し、JSON を要するチェックは飛ばす
    # （同じ原因を各チェックが重複計上すると、カテゴリ内訳が対策の判断材料にならない）
    if obj is None and any(c["type"] in NEEDS_JSON for c in checks):
        failures.append({"check": "json_parse", "category": CAT_FORMAT,
                         "detail": "出力が JSON として解釈できない"})
        checks = [c for c in checks if c["type"] not in NEEDS_JSON]

    for check in checks:
        kind = check["type"]

        if kind == "json_parse":
            pass  # ここに来る時点でパース済み

        elif kind == "json_schema":
            try:
                import jsonschema
            except ImportError:
                raise SystemExit("json_schema チェックには jsonschema が必要: pip install jsonschema")
            schema = json.loads((base / check["schema_path"]).read_text(encoding="utf-8"))
            try:
                jsonschema.validate(obj, schema)
            except jsonschema.ValidationError as e:
                # required 違反はフィールドが出てこなかった＝抽出漏れ（対策が型崩れと違う）
                category = CAT_MISSING if e.validator == "required" else CAT_FORMAT
                failures.append({"check": kind, "category": category,
                                 "detail": f"スキーマ不適合: {e.message}"})

        elif kind == "exact_match":
            expected = case.get("expected") or {}
            fields = check.get("fields") or list(expected.keys())
            for field in fields:
                if field not in expected:
                    continue
                actual = obj.get(field) if isinstance(obj, dict) else None
                if actual == expected[field]:
                    continue
                # 欠落＝抽出漏れ、値違い＝解釈違い（対策が異なるので分ける）
                category = CAT_MISSING if actual in (None, "", [], {}) else CAT_MEANING
                failures.append({"check": kind, "category": category,
                                 "detail": f"{field}: expected={expected[field]!r} actual={actual!r}"})

        elif kind == "contains":
            text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
            for needle in check["values"]:
                if needle not in text:
                    failures.append({"check": kind, "category": CAT_MISSING,
                                     "detail": f"出力に {needle!r} が含まれない"})

        else:
            raise SystemExit(f"未知のチェック種別: {kind}")

    return failures


def run_case(case: dict, target, checks: list[dict], base: Path, judge, rubric: str) -> dict:
    case_id = case.get("id") or "(id なし)"
    started = time.time()
    try:
        output = target(case["input"])
    except Exception:
        return {"id": case_id, "passed": False, "output": None,
                "failures": [{"check": "call", "category": CAT_ERROR,
                              "detail": traceback.format_exc(limit=3).strip()}],
                "elapsed": round(time.time() - started, 3)}

    failures = run_checks(case, output, checks, base)

    # LLM-as-judge はプログラム的チェックで代替できない箇所のみ（case 側で明示的に有効化）
    if judge and case.get("judge"):
        verdict = judge(case, output, rubric)
        if not verdict.get("pass"):
            failures.append({"check": "judge", "category": CAT_UNKNOWN,
                             "detail": verdict.get("reason", "(理由なし)")})

    return {"id": case_id, "passed": not failures, "output": output,
            "failures": failures, "elapsed": round(time.time() - started, 3)}


def primary_category(failures: list[dict]) -> str:
    """1 ケースの主因カテゴリを返す（CATEGORY_ORDER の優先順）。"""
    found = {f["category"] for f in failures}
    return next((c for c in CATEGORY_ORDER if c in found), CAT_UNKNOWN)


def previous_run(runs_dir: Path, current: Path) -> dict[str, bool]:
    """直前の run の ケースID→合否 を返す（無ければ空）。"""
    others = sorted(p for p in runs_dir.iterdir() if p.is_dir() and p != current)
    if not others:
        return {}
    results = others[-1] / "results.jsonl"
    if not results.exists():
        return {}
    with results.open(encoding="utf-8") as f:
        return {r["id"]: r["passed"] for r in (json.loads(line) for line in f if line.strip())}


def write_summary(path: Path, results: list[dict], prev: dict[str, bool], cfg_path: Path) -> None:
    total = len(results)
    passed = sum(r["passed"] for r in results)
    categories: dict[str, int] = {}
    for r in results:
        if r["failures"]:
            cat = primary_category(r["failures"])
            categories[cat] = categories.get(cat, 0) + 1

    regressions = [r["id"] for r in results if not r["passed"] and prev.get(r["id"]) is True]
    fixed = [r["id"] for r in results if r["passed"] and prev.get(r["id"]) is False]

    lines = [
        f"# eval run: {path.parent.name}",
        f"config: {cfg_path}",
        "",
        f"- 正答率: **{passed}/{total}** ({passed / total:.1%})" if total else "- ケースなし",
    ]
    if prev:
        before = sum(prev.get(r["id"], False) for r in results)
        lines.append(f"- 前回比: {before}/{total} → {passed}/{total}")
        lines.append(f"- 回帰（前回パス→今回失敗）: {', '.join(regressions) or 'なし'}")
        lines.append(f"- 改善（前回失敗→今回パス）: {', '.join(fixed) or 'なし'}")
    else:
        lines.append("- 前回比: 初回のため比較なし")

    lines += ["", "## 失敗カテゴリ内訳（ケース数・主因で分類）", ""]
    lines += [f"- {c}: {n}" for c, n in sorted(categories.items(), key=lambda x: -x[1])] or ["- なし"]
    lines += ["", "## 失敗ケース", ""]
    for r in results:
        if r["passed"]:
            continue
        lines.append(f"### {r['id']} — 主因: {primary_category(r['failures'])}")
        for f in r["failures"]:
            lines.append(f"- [{f['category']}] {f['check']}: {f['detail']}")
        lines.append("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="eval/eval-config.yaml")
    ap.add_argument("--only", help="実行するケース ID をカンマ区切りで指定")
    args = ap.parse_args()

    cfg_path = Path(args.config).resolve()
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    base = Path(cfg.get("base_dir") or ".").resolve()
    sys.path.insert(0, str(base))

    target = resolve(cfg["target"]["entrypoint"])
    checks = cfg.get("checks") or []

    judge = rubric = None
    if cfg.get("judge"):
        judge = resolve(cfg["judge"]["entrypoint"])
        rubric = (base / cfg["judge"]["rubric_path"]).read_text(encoding="utf-8")

    cases = load_dataset(base / cfg["dataset"]["path"])
    if args.only:
        wanted = {s.strip() for s in args.only.split(",")}
        cases = [c for c in cases if c.get("id") in wanted]
    if not cases:
        raise SystemExit("実行するケースがない")

    runs_dir = base / (cfg.get("run", {}).get("output_dir") or "eval/runs")
    run_dir = runs_dir / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_dir.mkdir(parents=True, exist_ok=True)

    workers = int(cfg.get("run", {}).get("concurrency") or 4)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(
            lambda case: run_case(case, target, checks, base, judge, rubric), cases))

    with (run_dir / "results.jsonl").open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    write_summary(run_dir / "summary.md", results, previous_run(runs_dir, run_dir), cfg_path)

    passed = sum(r["passed"] for r in results)
    print(f"{passed}/{len(results)} passed -> {run_dir / 'summary.md'}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
