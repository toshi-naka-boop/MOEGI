"""LLM出力からJSONオブジェクトを取り出す共通処理（pipeline・nippou・privacy が共有する）。"""

from __future__ import annotations

import json


def extract_json_payload(text: str) -> dict | None:
    """LLM出力から最初の完結したJSONオブジェクトを取り出す（配列・ネストを含んでよい）。

    一括判定の応答は judgements 配列を持つネスト構造なので、_extract_json_object の
    非貪欲マッチでは最初の要素の "}" で切れてしまう。ここでは文字列リテラルを
    読み飛ばしながら波括弧の対応を数え、閉じた時点で json.loads を試す。
    """
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break  # このstartからは組み立てられない。次の "{" を試す
                    return data if isinstance(data, dict) else None
        start = text.find("{", start + 1)
    return None
