"""デモモード（環境変数 MOEGI_DEMO=1）。

Ollama・ORCA・通達PDFが無いPCでも、承認 → 配信キュー → FAX PDF → 日報 → 月報の画面の流れを
一周して試せるようにするためのもの。**AIは動いていません。**

- 分析結果: 通達の読み取りは行わず、合成した更新案2件（実在の通達の内容ではありません）を入れます。
- 日報・SOAP・Privacy Gate の補完検査: AIの代わりに、文の単純な照合規則で答える `DemoAdapter` を使います。
  日報の項目照合の質は実際のAIより大きく劣ります。Privacy Gate の規則検査・辞書検査・
  再検査（コードによる検査）は本物のままです。
- 記録の置き場所は scripts/demo.sh が別の場所（data/demo/）に切り替えるので、本番の記録とは混ざりません。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3

from moegi import store

_DEMO_ENV = "MOEGI_DEMO"


def is_demo() -> bool:
    return os.environ.get(_DEMO_ENV, "") == "1"


DEMO_BANNER = (
    "デモモードです。表示されている更新案・日報の判定は、AIによるものではなく、動作確認用の"
    "合成データと単純な規則です（実在の通達の内容ではありません）。実際の運用では使えません。"
)

_DEMO_NEW_TEXT = (
    "（デモ用の架空の条文）訪問時間が標準を下回る場合は、その理由と利用者・家族への説明を記録に残すこと。"
    "また、看護目標に対する評価（達成の程度と効果）を毎回の訪問後に記録すること。"
)

_DEMO_PROPOSALS = (
    {
        "label": "DEMO-1",
        "title": "標準時間を下回る訪問の記録（デモ）",
        "kind": "追記",
        "item_label": "1回の訪問時間が標準（30分〜1時間30分）を下回る場合の理由記録",
        "item_key": "short-visit-reason",
        "before": "1回の訪問時間が標準（30分〜1時間30分）を下回る場合の理由記録",
        "after": "1回の訪問時間が標準（30分〜1時間30分）を下回る場合の理由と、利用者・家族への説明の記録",
        "rationale": "（デモ用の合成データ）条文に「理由と説明を記録に残す」とあるため、現行項目に説明の記録を加える案です。",
        "quote": "訪問時間が標準を下回る場合は、その理由と利用者・家族への説明を記録に残すこと。",
    },
    {
        "label": "DEMO-2",
        "title": "評価の記録（デモ）",
        "kind": "新設",
        "item_label": None,
        "item_key": None,
        "before": "",
        "after": "目標に対する評価の記録（達成の程度と効果）",
        "rationale": "（デモ用の合成データ）条文に「看護目標に対する評価を記録する」とあるが、現行のチェックリストに該当する項目が無いため、新しい項目を提案します。",
        "quote": "看護目標に対する評価（達成の程度と効果）を毎回の訪問後に記録すること。",
    },
)


def seed_run(conn: sqlite3.Connection) -> str:
    """合成した更新案（未承認）を1回分保存し、run_id を返す。"""
    run_id = store.new_run_id()
    conn.execute(
        "INSERT INTO cost_records (run_id, stage, backend, tokens_in, tokens_out, cost_jpy,"
        " latency_ms, degraded, resolved_model, fallback_level) VALUES (?, 'demo', 'demo', 0, 0, 0, 0, 0, 'demo', 0)",
        (run_id,),
    )
    for spec in _DEMO_PROPOSALS:
        change = conn.execute(
            "INSERT INTO changes (run_id, label, old_path, old_text, new_path, new_text, kind, summary)"
            " VALUES (?, ?, NULL, NULL, ?, ?, ?, ?)",
            (
                run_id,
                spec["label"],
                f"デモ条文（{spec['label']}）",
                _DEMO_NEW_TEXT,
                spec["kind"],
                f"【デモ用の合成データ】{spec['title']}",
            ),
        ).lastrowid
        impact = conn.execute(
            "INSERT INTO impacts (run_id, change_row_id, sitedoc_item_id, sitedoc_item_key, verdict,"
            " rationale, confidence, evidence_path, evidence_quote, evidence_offset)"
            " VALUES (?, ?, ?, ?, '要更新', ?, 0.90, ?, ?, ?)",
            (
                run_id,
                change,
                spec["item_label"],
                spec["item_key"],
                spec["rationale"],
                f"デモ条文（{spec['label']}）",
                spec["quote"],
                _DEMO_NEW_TEXT.find(spec["quote"]),
            ),
        ).lastrowid
        conn.execute(
            "INSERT INTO proposals (run_id, impact_row_id, before, after, status) VALUES (?, ?, ?, ?, '未承認')",
            (run_id, impact, spec["before"], spec["after"]),
        )
    conn.commit()
    return run_id


# ---------------------------------------------------------------------------
# AIの代わりの単純な規則
# ---------------------------------------------------------------------------


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"[。\n]", text) if s.strip()]


def _bigrams(text: str) -> set[str]:
    chars = re.sub(r"[\s、，,・（）()：:]", "", text)
    return {chars[i : i + 2] for i in range(len(chars) - 1)}


#: 項目の文言に出てくる語 → 発話の中で手がかりにする語（単純な対応表）
_HINTS = {
    "バイタル": ("血圧", "体温", "脈拍", "呼吸", "SpO2", "サチュ"),
    "時刻": ("時", "分"),
    "時間": ("分", "時間"),
    "特記": ("特記", "気になる", "変化", "家族", "説明"),
    "目標": ("目標",),
    "評価": ("順調", "達成", "改善", "経過"),
    "理由": ("ため", "理由", "ので"),
}


def _match_item(label: str, sentences: list[str]) -> str:
    label_grams = _bigrams(label)
    best, best_score = "", 0
    for sentence in sentences:
        score = len(label_grams & _bigrams(sentence))
        for key, hints in _HINTS.items():
            if key in label and any(h in sentence for h in hints):
                score += 2
        if score > best_score:
            best, best_score = sentence, score
    return best if best_score >= 2 else ""


def _structure_reply(prompt: str) -> dict:
    text = prompt.split("【発話内容】", 1)[1].split("【チェックリスト項目】", 1)[0]
    block = prompt.split("【チェックリスト項目】", 1)[1]
    sentences = _sentences(text)
    items = []
    for match in re.finditer(r"^(\d+)\. (.+?)（種別: ", block, flags=re.MULTILINE):
        value = _match_item(match.group(2), sentences)
        items.append({"item_id": match.group(1), "filled": bool(value), "value": value})
    payload: dict = {"items": items}
    if "【評価対象の看護目標】" in prompt:
        achievement = next((s for s in sentences if any(w in s for w in ("順調", "達成", "改善"))), "")
        effect = next((s for s in sentences if any(w in s for w in ("和らい", "軽減", "楽に"))), "")
        payload["evaluation"] = {"achievement": achievement, "effect": effect}
    return payload


def _soap_reply(prompt: str) -> dict:
    text = prompt.split("【発話内容】", 1)[1].split("【各区分の意味】", 1)[0]
    sentences = _sentences(text)

    def pick(*words: str) -> str:
        return "。".join(s for s in sentences if any(w in s for w in words))

    return {
        "subjective": pick("おっしゃ", "訴え", "痛み", "話"),
        "objective": pick("血圧", "体温", "脈拍", "呼吸", "処置", "実施"),
        "assessment": pick("順調", "良好", "改善", "経過"),
        "plan": pick("継続", "次回", "指導", "予定"),
    }


def _detect_reply(prompt: str) -> dict:
    text = prompt.split("【対象テキスト】\n", 1)[1].split("\n\n【出力形式】", 1)[0]
    return {
        "sanitized_text": text,
        "detected_entities": [],
        "residual_risk": "low",
        "needs_human_review": False,
    }


class DemoAdapter:
    """llm.LLMAdapter の代わり（complete の契約だけを満たす）。外部通信もAI呼び出しもしない。"""

    local_unavailable_count = 0
    local_timeout_count = 0
    local_call_count = 0

    def complete(self, stage: str, prompt: str, opts: dict | None = None) -> dict:
        if stage == "pseudonymize":
            payload = _detect_reply(prompt)
        elif "【チェックリスト項目】" in prompt:
            payload = _structure_reply(prompt)
        elif "SOAP" in prompt:
            payload = _soap_reply(prompt)
        else:
            payload = {}
        return {
            "text": json.dumps(payload, ensure_ascii=False),
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            "cost_jpy": 0.0,
            "backend": "demo",
            "degraded": False,
            "timeout": False,
            "guardrail_blocked": False,
            "latency_ms": 0.0,
            "resolved_model": "demo",
            "fallback_level": 0,
        }

    def close(self) -> None:
        return None

    def __enter__(self) -> "DemoAdapter":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None
