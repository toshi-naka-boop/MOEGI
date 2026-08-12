"""eval-loop の target.entrypoint。本番コード（pipeline.run）をそのまま呼び出す。

1つの eval case は (label, item_label) の1セルだが、本番の呼び出し単位はlabelごと
（match_requirementsが8項目まとめて処理し、orchestrateも同じlabel内の候補同士で
横断裁定する）。そのため process 内キャッシュでlabelごとに1回だけ実行し、
同じlabelの複数セルはキャッシュから引く（LLM呼び出し回数を本番と同じに保つ）。
"""

from __future__ import annotations

import functools
from pathlib import Path

from moegi import ingest, llm, pipeline

_REPO_ROOT = Path(__file__).resolve().parents[1]


@functools.lru_cache(maxsize=None)
def _run_label(label: str) -> dict[str, str]:
    targets = ingest.load_allowlist(str(_REPO_ROOT / "config" / "allowlist.toml"))
    old_notice = ingest.load_notice(
        str(_REPO_ROOT / "data" / "notices" / "old_hohatsu0305-12.pdf"),
        id="old", title="旧通達", issued_on="", version="old", source_url="",
    )
    new_notice = ingest.load_notice(
        str(_REPO_ROOT / "data" / "notices" / "new_hohatsu0305-19.pdf"),
        id="new", title="新通達", issued_on="", version="new", source_url="",
    )
    old_sections = ingest.apply_allowlist(old_notice, targets)
    new_sections = ingest.apply_allowlist(new_notice, targets)
    sitedoc = pipeline.load_sitedoc()

    with llm.LLMAdapter() as adapter:
        result = pipeline.run(
            old_sections=old_sections,
            new_sections=new_sections,
            sitedoc=sitedoc,
            adapter=adapter,
            run_id=f"eval-{label}",
            labels={label},
            batch=False,
            orchestrate=True,
        )

    predicted: dict[str, str] = {}
    for item in sitedoc.items:
        matches = [i for i in result["impacts"] if i.sitedoc_item_id == item.label]
        if any(i.verdict == "要更新" for i in matches):
            predicted[item.label] = "要更新"
        elif any(i.verdict == "判定不能" for i in matches):
            predicted[item.label] = "判定不能"
        else:
            predicted[item.label] = "影響なし"
    return predicted


def judge_cell(case_input: dict) -> dict:
    predicted = _run_label(case_input["label"])
    return {"verdict": predicted[case_input["item_label"]]}
