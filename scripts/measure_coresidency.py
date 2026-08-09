"""D1 性能実測ゲート: Ollama(Tanuki) と faster-whisper の同時常駐を測る。

design.md §6.2 の「新構成（ASR並走・突合の同時常駐）の確認」に対応する。
焦点は2点だけ:
  1. 24GB に両モデルが載るか（スワップしないか）
  2. ASR 並走時に突合（③-b）の推論がどれだけ劣化するか

突合のプロンプトは、実際に allowlist で抽出した通達本文（c: 評価の記録）と
架空事業所のチェックリストで組む。D2 の実装を先取りしないよう、
ここでは「代表的な負荷をかけて時間を測る」ことだけを行う。
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from moegi.ingest import apply_allowlist, load_allowlist, load_notice  # noqa: E402

OLLAMA = "http://127.0.0.1:11434"
LLM_MODEL = "7shi/tanuki-dpo-v1.0:latest"
ASR_MODEL = "small"  # 日本語・22秒の発話に対する現実的な下限として small から測る
AUDIO = ROOT / "data" / "audio" / "nippou_sample.wav"
RUNS = 3

# 架空事業所のチェックリスト（公開様式ベースの想定・design.md §9）
CHECKLIST = [
    "訪問日時を記入する",
    "バイタルサイン（血圧・体温・脈拍・呼吸数）を記入する",
    "実施した看護内容を記入する",
    "使用した衛生材料を記入する",
    "利用者・家族への説明内容を記入する",
    "次回訪問予定を記入する",
    "特記事項を記入する",
]


def build_matching_prompt(notice_text: str) -> str:
    """③-b 突合の代表プロンプト。通達本文 × 事業所チェックリストの非構造マッピング。"""
    items = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(CHECKLIST))
    return (
        "あなたは訪問看護事業所の管理者を補助するアシスタントです。\n"
        "以下の通達本文の要求と、事業所のチェックリストを突き合わせ、"
        "各項目が要求を満たしているか、追加が必要な項目は何かを日本語で簡潔に答えてください。\n\n"
        f"【通達本文】\n{notice_text}\n\n"
        f"【事業所のチェックリスト】\n{items}\n"
    )


def ollama_generate(prompt: str) -> tuple[float, int]:
    """1回の推論。(所要秒, 出力トークン数) を返す。"""
    t0 = time.perf_counter()
    r = httpx.post(
        f"{OLLAMA}/api/generate",
        json={"model": LLM_MODEL, "prompt": prompt, "stream": False,
              "options": {"num_predict": 256, "temperature": 0}},
        timeout=600.0,
    )
    r.raise_for_status()
    body = r.json()
    return time.perf_counter() - t0, body.get("eval_count", 0)


def ollama_ps() -> str:
    return subprocess.run(["ollama", "ps"], capture_output=True, text=True).stdout.strip()


def memory_snapshot() -> dict:
    """ページング状況。swapouts が増えていないことが「載っている」の判定根拠。"""
    out = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True).stdout.strip()
    return {"swapusage": out}


def load_notice_section() -> str:
    """allowlist で抽出済みの c（評価の記録）を実データとして使う。"""
    targets = load_allowlist(ROOT / "config" / "allowlist.toml")
    notice = load_notice(
        ROOT / "data" / "notices" / "new_hohatsu0305-19.pdf",
        id="hohatsu0305-19",
        title="訪問看護療養費に係る指定訪問看護の費用の額の算定方法の一部改正に伴う実施上の留意事項について",
        issued_on="2026-03-05",
        version="new",
        source_url="https://www.mhlw.go.jp/content/12400000/001686845.pdf",
    )
    picked = apply_allowlist(notice, targets)
    section = picked.get("c")
    if section is None:
        raise SystemExit("allowlist から c（評価の記録）を取得できませんでした")
    return section.text


def main() -> None:
    print("=" * 72)
    print("D1 性能実測ゲート: Ollama(Tanuki) + faster-whisper 同時常駐")
    print("=" * 72)

    notice_text = load_notice_section()
    prompt = build_matching_prompt(notice_text)
    print(f"\n突合プロンプト: {len(prompt)} 文字（うち通達本文 {len(notice_text)} 文字）")

    print(f"\n[0] 起動前のスワップ: {memory_snapshot()['swapusage']}")

    # --- 1. 突合のみ（ベースライン） ---
    print(f"\n[1] 突合のみ ×{RUNS}（ASR停止中）")
    ollama_generate(prompt)  # ウォームアップ（モデルロードを計上しない）
    base = []
    for i in range(RUNS):
        sec, toks = ollama_generate(prompt)
        base.append(sec)
        print(f"    run{i + 1}: {sec:6.2f} 秒 / {toks} tok")
    print(f"    中央値: {statistics.median(base):.2f} 秒")
    print(f"    ollama ps:\n{ollama_ps()}")

    # --- 2. ASR 単体 ---
    print(f"\n[2] ASR 単体（{AUDIO.name} / faster-whisper {ASR_MODEL}）")
    from faster_whisper import WhisperModel

    t0 = time.perf_counter()
    asr = WhisperModel(ASR_MODEL, device="cpu", compute_type="int8")
    print(f"    モデルロード: {time.perf_counter() - t0:.2f} 秒")

    t0 = time.perf_counter()
    segments, info = asr.transcribe(str(AUDIO), language="ja", beam_size=1)
    text = "".join(s.text for s in segments)
    asr_alone = time.perf_counter() - t0
    print(f"    文字起こし: {asr_alone:.2f} 秒（音声 {info.duration:.1f} 秒）")
    print(f"    結果: {text.strip()[:80]}...")

    # --- 3. ASR 並走時の突合 ---
    print(f"\n[3] ASR 並走時の突合 ×{RUNS}")
    stop = threading.Event()
    asr_runs: list[float] = []

    def asr_loop() -> None:
        while not stop.is_set():
            t = time.perf_counter()
            segs, _ = asr.transcribe(str(AUDIO), language="ja", beam_size=1)
            "".join(s.text for s in segs)
            asr_runs.append(time.perf_counter() - t)

    th = threading.Thread(target=asr_loop, daemon=True)
    th.start()
    time.sleep(1.0)  # ASR を確実に走行状態にしてから測る

    par = []
    for i in range(RUNS):
        sec, toks = ollama_generate(prompt)
        par.append(sec)
        print(f"    run{i + 1}: {sec:6.2f} 秒 / {toks} tok")
    stop.set()
    th.join(timeout=120)

    print(f"    中央値: {statistics.median(par):.2f} 秒")
    if asr_runs:
        print(f"    並走中のASR: 中央値 {statistics.median(asr_runs):.2f} 秒 ×{len(asr_runs)}回")
    print(f"    ollama ps:\n{ollama_ps()}")
    print(f"\n[4] 実測後のスワップ: {memory_snapshot()['swapusage']}")

    # --- 判定 ---
    b, p = statistics.median(base), statistics.median(par)
    print("\n" + "=" * 72)
    print(f"突合（単独）      : {b:6.2f} 秒")
    print(f"突合（ASR並走）   : {p:6.2f} 秒")
    print(f"劣化幅            : {p - b:+6.2f} 秒（{(p / b - 1) * 100:+.1f}%）")
    print(f"ASR（単独）       : {asr_alone:6.2f} 秒 / 音声 {info.duration:.1f} 秒")
    print("=" * 72)

    (ROOT / "data" / "measure_result.json").write_text(
        json.dumps(
            {
                "machine": "Mac mini M4 Pro 24GB",
                "llm": LLM_MODEL,
                "asr": f"faster-whisper {ASR_MODEL} (int8, cpu)",
                "prompt_chars": len(prompt),
                "matching_alone_sec": round(b, 2),
                "matching_with_asr_sec": round(p, 2),
                "degradation_sec": round(p - b, 2),
                "degradation_pct": round((p / b - 1) * 100, 1),
                "asr_alone_sec": round(asr_alone, 2),
                "audio_sec": round(info.duration, 2),
                "swapusage_after": memory_snapshot()["swapusage"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print("→ data/measure_result.json に保存しました")


if __name__ == "__main__":
    main()
