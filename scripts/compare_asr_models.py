"""ASR モデルの日本語精度と所要時間を比べる。

D1 実測で faster-whisper small の日本語が崩れた（褥瘡→仕留めかさ 等）。
デモでは文字起こし結果が画面に出る（design.md §7 #6）ため、
専門用語が崩れると現場ドメインのデモとして成立しない。
どのサイズなら実用に足るかを、精度・時間・メモリで比べる。
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from faster_whisper import WhisperModel

ROOT = Path(__file__).resolve().parents[1]
AUDIO = ROOT / "data" / "audio" / "nippou_sample.wav"

# 発話に含まれる、崩れると困る語（訪問看護の専門用語・数値）
KEYWORDS = ["訪問", "血圧", "体温", "呼吸数", "褥瘡", "処置", "発赤", "服薬", "次回訪問"]

MODELS = ["small", "medium", "large-v3"]


def swap_used_mb() -> float:
    out = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True).stdout
    for tok in out.split():
        if tok.endswith("M") and "used" in out.split(tok)[0][-8:]:
            pass
    # "total = 3072.00M  used = 2299.81M  free = 772.19M"
    parts = out.replace("=", " ").split()
    try:
        return float(parts[parts.index("used") + 1].rstrip("M"))
    except (ValueError, IndexError):
        return -1.0


def main() -> None:
    print(f"音声: {AUDIO.name}")
    print(f"確認する語: {' / '.join(KEYWORDS)}\n")
    print(f"{'model':10} {'load(s)':>8} {'asr(s)':>8} {'hit':>6}  swap(MB)  文字起こし")
    print("-" * 110)

    for name in MODELS:
        t0 = time.perf_counter()
        model = WhisperModel(name, device="cpu", compute_type="int8")
        load = time.perf_counter() - t0

        t0 = time.perf_counter()
        segments, _ = model.transcribe(str(AUDIO), language="ja", beam_size=1)
        text = "".join(s.text for s in segments).strip()
        asr = time.perf_counter() - t0

        hit = sum(1 for k in KEYWORDS if k in text)
        print(f"{name:10} {load:8.1f} {asr:8.2f} {hit:>3}/{len(KEYWORDS)}  {swap_used_mb():8.0f}  {text[:60]}")
        print(f"{'':10} {'':8} {'':8} {'':6}            {text[60:130]}")
        del model


if __name__ == "__main__":
    main()
