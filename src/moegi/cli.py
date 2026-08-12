"""MOEGI CLI（design.md §10 D2完了条件）。

    python -m moegi.cli diff --new <pdf> --old <pdf> --out <json>
    python -m moegi.cli diff --new <pdf> --old <pdf> --out <json> --label c
    python -m moegi.cli diff --new <pdf> --old <pdf> --out <json> --single-shot

通達の新旧PDFペアから、allowlist適用済みの条文を差分抽出→要件抽出→突合→更新案生成の
4工程に通し、更新案（Proposal）をJSONで書き出す。

--label（design.md §2「投入単位」・v10）: allowlistのlabelを1つ指定すると、
その項目だけを処理する。突合が1件あたり最大52.7秒かかった実測（D2）があり、
デモ台本の秒数に収めるには通達ペア一括ではなく項目単位で流す経路が要る。
省略時は従来どおり全項目を一括処理する。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path

from moegi import ingest, llm, pipeline, store

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_ALLOWLIST_PATH = _REPO_ROOT / "config" / "allowlist.toml"


def _asdict_list(objs: list) -> list[dict]:
    return [dataclasses.asdict(o) for o in objs]


def _run_diff(args: argparse.Namespace) -> None:
    targets = ingest.load_allowlist(args.allowlist)

    labels: set[str] | None = None
    if args.label is not None:
        # design.md §2「投入単位」（v10）: --label は allowlist の label を指定する。
        available = {t["label"] for t in targets}
        if args.label not in available:
            raise SystemExit(
                f"--label {args.label!r} はallowlistに存在しません（利用可能: {sorted(available)}）"
            )
        labels = {args.label}

    old_notice = ingest.load_notice(
        args.old, id="old", title="旧通達", issued_on="", version="old", source_url=""
    )
    new_notice = ingest.load_notice(
        args.new, id="new", title="新通達", issued_on="", version="new", source_url=""
    )
    old_sections = ingest.apply_allowlist(old_notice, targets)
    new_sections = ingest.apply_allowlist(new_notice, targets)

    sitedoc = pipeline.load_sitedoc(args.sitedoc)

    conn = store.connect(args.db)
    run_id = store.new_run_id()

    with llm.LLMAdapter(config_path=args.models_config) as adapter:
        result = pipeline.run(
            old_sections=old_sections,
            new_sections=new_sections,
            sitedoc=sitedoc,
            adapter=adapter,
            run_id=run_id,
            store_conn=conn,
            labels=labels,
            batch=False if args.single_shot else None,
            # eval-loop（2026-08-12深夜・N-8対策）で45/48=93.8%まで実測検証した設定。
            orchestrate=True,
        )

    output = {
        "run_id": run_id,
        "changes": _asdict_list(result["changes"]),
        "requirements": _asdict_list(result["requirements"]),
        "impacts": _asdict_list(result["impacts"]),
        "proposals": _asdict_list(result["proposals"]),
    }

    Path(args.out).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"更新案 {len(result['proposals'])} 件（影響判定 {len(result['impacts'])} 件 / "
        f"差分 {len(result['changes'])} 件）を {args.out} に出力しました（run_id={run_id}）"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="moegi")
    subparsers = parser.add_subparsers(dest="command", required=True)

    diff_parser = subparsers.add_parser("diff", help="通達新旧PDFから更新案を生成する")
    diff_parser.add_argument("--new", required=True, help="新版通達PDFのパス")
    diff_parser.add_argument("--old", required=True, help="旧版通達PDFのパス")
    diff_parser.add_argument("--out", required=True, help="更新案JSONの出力先パス")
    diff_parser.add_argument("--allowlist", default=str(_DEFAULT_ALLOWLIST_PATH))
    diff_parser.add_argument(
        "--label",
        default=None,
        help="allowlistのlabel（例: c）を1つ指定し、その項目だけを処理する。省略時は全項目を一括処理する",
    )
    diff_parser.add_argument(
        "--single-shot",
        action="store_true",
        help=(
            "突合・更新案生成を1項目=1呼び出しの単発モードで実行する（デバッグ用）。"
            "省略時は models.toml の batch_matching に従う"
        ),
    )
    diff_parser.add_argument("--sitedoc", default=str(pipeline.DEFAULT_SITEDOC_PATH))
    diff_parser.add_argument("--models-config", default=str(llm.DEFAULT_CONFIG_PATH))
    diff_parser.add_argument("--db", default=str(store.DEFAULT_DB_PATH))
    diff_parser.set_defaults(func=_run_diff)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
