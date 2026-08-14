"""承認UI（design.md §10 D4）。

    uvicorn moegi.webapp:app --reload

design.md §14 の技術スタック決定どおり FastAPI・サーバーレンダリング＋素のJS
（CDN依存ゼロ）。テンプレートエンジンは使わず、素のPython文字列で組み立てる。

G2「根拠が辿れる」: 各更新案の行に <details> で原文トレース（新旧本文・
根拠の引用箇所）を埋め込み、開閉の1操作で到達できるようにする。

G9-b実測値（再現率・誤検出率）はUIには出さない（ユーザー決定・2026-08-11、ledger R-75）。
デモの邪魔になるため、ピッチ資料側で説明する。実測自体は引き続き config/g9_latest.json に残す。
"""

from __future__ import annotations

import asyncio
import dataclasses
import html
import re
import tempfile
from contextlib import asynccontextmanager, suppress
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from moegi import checklist, delivery, fax, ingest, llm, monthly, nippou, pipeline, privacy, store
from moegi.privacy import dictionary as privacy_dictionary
from moegi.privacy import vault as privacy_vault

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FAX_OUTPUT_DIR = _REPO_ROOT / "data" / "fax_previews"

# design/moegi/privacy.md §10: uvicorn・httpx を含む logging 経由の出力に
# サニタイザーを差し込む。アプリの起動より前（モジュール読み込み時）に行う。
privacy.install_log_sanitizer()

# 設定の読み込みに失敗したらアプリを起動させない（fail-closed）。
# 「検査設定が読めないまま検査なしで動いてしまう」のが最悪なので、静かに既定値へ落とさない。
PRIVACY_CONFIG = privacy.load_privacy_config()


#: 仮名（PATIENT_001等）と型付きタグ（[[PHONE]]等）。発話表示でハイライトする対象。
_PSEUDONYM_RE = re.compile(
    r"(?:PATIENT|NURSE|DOCTOR|FACILITY|FAMILY|PERSON)_[A-Za-z0-9]+|\[\[[A-Z_0-9]+\]\]"
)


def _highlight_pseudonyms(text: str) -> str:
    """仮名化済みテキストの仮名・タグを <span class='pseudo'> で目立たせる（handoff §⑤）。

    エスケープ後の文字列に対して差し込む。仮名もタグも英数字と [[ ]] だけで構成され
    html.escape で変化しないので、エスケープ後でも正規表現がそのまま一致する。
    """
    escaped = html.escape(text)
    return _PSEUDONYM_RE.sub(lambda m: f"<span class='pseudo'>{m.group(0)}</span>", escaped)


def _patient_code_options(selected: str = "") -> str:
    """利用者コードの選択肢（ledger R-84）。氏名は画面に出さない。

    実運用では認証つきの氏名表示に置き換わる想定（design/moegi/privacy.md §10-4）。
    辞書が読めない場合は空にする——選べないことで「誰の記録か不明なまま保存される」を防ぐ。
    """
    try:
        dictionary = privacy_dictionary.load_dictionary(PRIVACY_CONFIG.dictionary_path)
    except Exception:  # noqa: BLE001 — 辞書の中身を例外経由で外へ出さない
        return ""
    return "".join(
        f"<option value='{html.escape(code)}'{' selected' if code == selected else ''}>"
        f"{html.escape(code)}</option>"
        for code in privacy_dictionary.patient_codes(dictionary)
    )


def _normalize_visited_on(raw: str) -> str:
    """訪問日を YYYY-MM-DD に正規化する。空・不正なら当日を使う。"""
    try:
        return date.fromisoformat(raw.strip()).isoformat()
    except (ValueError, AttributeError):
        return date.today().isoformat()

# ledger R-77・R-80: 処置内容はLLMの自由記述抽出に任せず、ボタン選択式（複数選択可）にする
# （処置名の表記揺れ・誤認識リスクを避けるため）。design.md §6.5「処置入力のボタン選択UI
# （v6追加）」が既に「処置マスタ10項目固定」を規定済みだったため、項目数をそれに合わせた。
# 訪問看護の一般的な処置区分の暫定リスト——実運用では事業所側の語彙に差し替える想定。
_PROCEDURE_OPTIONS = (
    "創傷処置（褥瘡ケア等）",
    "点滴・注射",
    "吸引",
    "カテーテル管理",
    "リハビリテーション",
    "服薬管理・指導",
    "バイタル測定",
    "排泄ケア",
    "清潔ケア（入浴・清拭）",
    "その他",
)

#: 配信設定（design.md G8・ledger R-89）。認証情報は環境変数からのみ取る。
DELIVERY_CONFIG = delivery.load_config()

#: 回線復帰の検知間隔（秒）。デモの終幕（回線復帰→自動着弾）が「手を触れずに」
#: 起きて見える程度に短く、かつプローブが煩くない程度に長く。
DELIVERY_FLUSH_INTERVAL_SECONDS = 5.0


async def _delivery_flusher() -> None:
    """送信待ちを定期的に流す（design.md G8・R-16「手動再送ボタンは作らない」）。

    オフラインの間は is_online() が False を返すので1件も送らず、キューは溜まり続ける。
    回線が復帰した最初のティックでまとめて流れる。SQLite・SMTPはどちらもブロッキングなので
    to_thread に逃がし、イベントループを止めない。
    """
    while True:
        await asyncio.sleep(DELIVERY_FLUSH_INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(_flush_deliveries_once)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — 背景タスクを1回の失敗で止めない
            continue


def _flush_deliveries_once() -> delivery.FlushResult:
    conn = _db_conn()
    try:
        return delivery.flush(conn, config=DELIVERY_CONFIG)
    finally:
        conn.close()


@asynccontextmanager
async def _lifespan(_: FastAPI):
    task = asyncio.create_task(_delivery_flusher())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="MOEGI 承認UI", lifespan=_lifespan)


def _db_conn():
    return store.connect()


def _pending_delivery_badge() -> str:
    """G8「送信待ち n件」バッジ。0件のときは何も出さない。"""
    conn = _db_conn()
    try:
        pending = store.count_pending_deliveries(conn)
    finally:
        conn.close()
    if not pending:
        return ""
    return (
        f"<div class='queue-note'><span><strong>送信待ち {pending} 件</strong>。"
        "回線が復帰すると自動で送信されます（手動の再送操作は必要ありません）。</span>"
        " <a href='/deliveries'>配信キューを見る</a></div>"
    )


_PAGE_STYLE = """
<style>
  /* ==== デザイントークン（docs/design_handoff_moegi_ui/moegi-tokens.css v1.2 が正） ====
     文字色に使う組み合わせはすべて WCAG AA (4.5:1) 実測済み。値を変更しないこと。 */
  :root {
    --ink: #333A32;              /* 本文 11.1:1 on paper */
    --ink-soft: #565F4F;         /* 補助テキスト 6.3:1 */
    --paper: #FAF9F2;
    --paper-raised: #FFFFFF;
    --line: #DDE0D2;
    --moegi: #007B43;            /* 主ボタン地（常磐緑） */
    --moegi-ink: #FFFFFF;        /* 主ボタンの文字 5.4:1 */
    --moegi-hover: #00693A;
    --moegi-soft: #EEF3E1;
    --moegi-deep: #00532F;
    --moegi-accent: #AACF53;     /* 萌黄。装飾・タグ地・ハイライト専用。文字色に使わない */
    --link: #007B43;
    --link-hover: #00532F;
    --focus: #007B43;
    --warn: #8A5E00;
    --warn-soft: #FBF1E2;
    --warn-icon: #F8B500;        /* 山吹。アイコン・図形専用。文字不可 */
    --danger: #C0242F;
    --danger-soft: #FBEAEA;
    --status-approved: #2E6E45;  --status-approved-bg: #EAF4EA;
    --status-rejected: #5F6557;  --status-rejected-bg: #F1F1EC;
    --status-edited:   #33587F;  --status-edited-bg:   #EAF1F8;
    --gate-passed:  #2E6E45;  --gate-passed-bg:  #EAF4EA;
    --gate-review:  #8A5E00;  --gate-review-bg:  #FBF1E2;
    --gate-blocked: #C0242F;  --gate-blocked-bg: #FBEAEA;
    --gate-blocked-fill: #A8391F; /* 停止バナーの塗り。ページ内唯一の赤塗り */
    --gate-failed:  #C0242F;  --gate-failed-bg:  #FBEAEA;
    --diff-add: #E3F2E9;  --diff-add-border: #2E8B6E;
    --diff-del: #FBEAEA;  --diff-del-border: #D9333F;
    --diff-mod: #FEF5E0;  --diff-mod-border: #A87400;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --ink: #E9ECE0; --ink-soft: #ADB69D;
      --paper: #15180F; --paper-raised: #1E2216; --line: #3A4130;
      --moegi: #A9CE58;            /* ダークでは萌黄が主ボタン地 */
      --moegi-ink: #1C2114; --moegi-hover: #C0DC82;
      --moegi-soft: #262C1B; --moegi-deep: #C4DE85; --moegi-accent: #AACF53;
      --link: #C4DE85; --link-hover: #E0EDBC; --focus: #A9CE58;
      --warn: #E5B45B; --warn-soft: #2E2716; --warn-icon: #F8B500;
      --danger: #E98973; --danger-soft: #33201B;
      --status-approved: #9CCB8B;  --status-approved-bg: #22311F;
      --status-rejected: #A9AFA0;  --status-rejected-bg: #2A2D24;
      --status-edited:   #8FB4DE;  --status-edited-bg:   #1F2A38;
      --gate-passed:  #9CCB8B;  --gate-passed-bg:  #22311F;
      --gate-review:  #E5B45B;  --gate-review-bg:  #2E2716;
      --gate-blocked: #E98973;  --gate-blocked-bg: #33201B;
      --gate-blocked-fill: #A8391F;
      --gate-failed:  #E98973;  --gate-failed-bg:  #33201B;
      --diff-add: #172A24;  --diff-add-border: #57B08D;
      --diff-del: #2E1A1D;  --diff-del-border: #D96A6A;
      --diff-mod: #2B2413;  --diff-mod-border: #D9A03E;
    }
  }

  /* ==== ベース ==== */
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--paper); color: var(--ink);
    font-family: -apple-system, "Hiragino Sans", "Yu Gothic", sans-serif;
    font-size: 15px; line-height: 1.75;
  }
  main { max-width: 880px; margin: 0 auto; padding: 20px 20px 72px; }
  a { color: var(--link); text-underline-offset: 3px; }
  a:hover { color: var(--link-hover); }
  :focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; border-radius: 2px; }
  h1 { font-size: 24px; line-height: 1.4; margin: 28px 0 4px; overflow-wrap: anywhere; }
  h2 { font-size: 18px; margin: 32px 0 12px; }
  h3 { font-size: 15px; margin: 20px 0 8px; }
  .run-sub { color: var(--ink-soft); font-size: 13px; margin: 0 0 24px; }
  .home-nav { font-size: 14px; padding-top: 4px; }

  /* 配信キューのバッジ（G8・全ページ共通。pending 0件では出さない） */
  .queue-note {
    display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap;
    background: var(--warn-soft); border-left: 4px solid var(--warn-icon);
    border-radius: 0 8px 8px 0; padding: 10px 16px; margin-top: 16px; font-size: 14px;
  }

  /* ==== 更新案カード（②） ==== */
  .proposal {
    background: var(--paper-raised); border: 1px solid var(--line); border-radius: 12px;
    padding: 24px; margin: 0 0 20px; position: relative;
  }
  .proposal[data-status="approved"] { border-left: 4px solid var(--status-approved); }
  .proposal[data-status="edited"]   { border-left: 4px solid var(--status-edited); }
  .proposal[data-status="rejected"] { border-left: 4px solid var(--line); }
  .proposal[data-status="rejected"] > :not(.chips):not(.meta) { opacity: .55; }
  .meta { font-size: 13px; color: var(--ink-soft); display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .meta strong { color: var(--ink); font-weight: 600; }
  .chips { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin: 10px 0 14px; }
  .chip {
    display: inline-flex; align-items: center; gap: 4px;
    font-size: 12px; font-weight: 700; line-height: 1.5;
    border-radius: 999px; padding: 2px 12px;
    background: var(--moegi-soft); color: var(--moegi-deep);
  }
  .chip-need     { background: var(--moegi-soft); color: var(--moegi-deep); }
  .chip-unknown  { background: var(--warn-soft); color: var(--warn); }
  .chip-pending  { background: var(--paper); color: var(--ink-soft); border: 1px solid var(--line); }
  .chip-approved { background: var(--status-approved-bg); color: var(--status-approved); }
  .chip-rejected { background: var(--status-rejected-bg); color: var(--status-rejected); }
  .chip-edited   { background: var(--status-edited-bg); color: var(--status-edited); }
  .status-chip   { background: var(--status-rejected-bg); color: var(--status-rejected); } /* 互換（⑤⑧⑨が移行するまで） */
  /* 変更種別（通達Diffトークン。色だけに頼らず記号を併記） */
  .kind {
    display: inline-flex; align-items: center; gap: 4px;
    font-size: 12px; font-weight: 700; border-radius: 4px; padding: 1px 8px;
    background: var(--diff-mod); border: 1px solid var(--diff-mod-border); color: var(--ink);
  }
  .kind-add { background: var(--diff-add); border-color: var(--diff-add-border); }
  .confidence { font-size: 12px; color: var(--ink-soft); }
  .rationale { margin: 0 0 16px; }

  /* ==== Before / After ==== */
  .diff { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 16px; }
  .diff > div { background: var(--paper); border: 1px solid var(--line); border-radius: 8px; padding: 14px 16px; font-size: 14px; }
  .diff .before { color: var(--ink-soft); }
  .diff .after  { background: var(--diff-mod); border-color: var(--diff-mod-border); font-weight: 600; color: var(--ink); }
  .field-label {
    display: block; font-size: 11px; font-weight: 700; letter-spacing: .08em;
    color: var(--ink-soft); margin-bottom: 6px;
  }
  .diff .after .field-label { color: var(--ink); opacity: .7; }

  /* ==== 原文トレース ==== */
  .trace { margin-bottom: 16px; }
  .trace summary {
    cursor: pointer; color: var(--link); font-size: 14px; font-weight: 600;
    min-height: 44px; display: flex; align-items: center; width: fit-content; padding-right: 8px;
  }
  .trace summary:hover { color: var(--link-hover); }
  .trace-text {
    background: var(--paper); border: 1px solid var(--line); border-radius: 8px;
    padding: 14px 16px; font-size: 13px; color: var(--ink-soft); margin-top: 4px;
    white-space: pre-wrap;
  }
  mark {
    background: var(--moegi-soft); color: var(--ink);
    border-bottom: 2px solid var(--moegi-accent); padding: 0 2px;
  }

  /* ==== 操作 ==== */
  .actions { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }
  button {
    font: inherit; font-size: 15px; font-weight: 700; cursor: pointer;
    min-height: 44px; padding: 0 22px; border-radius: 8px;
    border: 1.5px solid transparent;
    background: var(--moegi); color: var(--moegi-ink); /* 既定=主ボタン（クラス未指定の旧画面互換） */
  }
  button:hover { background: var(--moegi-hover); color: var(--moegi-ink); }
  button:active { background: var(--moegi-deep); color: var(--moegi-ink); }
  .btn-secondary { background: transparent; color: var(--link); border-color: var(--moegi); }
  .btn-secondary:hover { background: var(--moegi-soft); color: var(--link); }
  button:disabled { background: var(--line); color: var(--ink-soft); cursor: not-allowed; border-color: transparent; }
  .edit-form .field-label { font-size: 13px; letter-spacing: 0; color: var(--ink); }
  .confirmed-text {
    background: var(--paper); border: 1px solid var(--line); border-radius: 8px;
    padding: 12px 14px; font-size: 14px; margin: 0;
  }
  .confirmed-note { font-size: 13px; color: var(--ink-soft); margin: 6px 0 0; }
  textarea {
    width: 100%; font: inherit; font-size: 14px; line-height: 1.7;
    color: var(--ink); background: var(--paper);
    border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px;
    min-height: 96px; resize: vertical;
  }
  textarea:focus-visible { outline: 2px solid var(--focus); outline-offset: 0; border-color: var(--focus); }
  .edit-form button { margin-top: 10px; }

  /* ==== ページ下部（②配信・出力） ==== */
  .run-footer { border-top: 1px solid var(--line); margin-top: 36px; padding-top: 24px; }
  .run-footer h2 { font-size: 18px; margin: 0 0 12px; }
  .delivery-list { list-style: none; margin: 16px 0 0; padding: 0; font-size: 14px; }
  .delivery-list li {
    display: flex; gap: 10px; flex-wrap: wrap; align-items: baseline;
    padding: 10px 0; border-bottom: 1px solid var(--line); color: var(--ink-soft);
  }

  /* ==== ページ共通の小物 ==== */
  .page-sub, .run-sub { color: var(--ink-soft); font-size: 13px; margin: 0 0 20px; }
  .home-nav a { display: inline-flex; align-items: center; min-height: 44px; }
  section { margin-bottom: 36px; }
  .field-hint { font-size: 13px; color: var(--ink-soft); margin: 6px 0 0; }
  .section-hint { font-size: 12px; color: var(--ink-soft); margin: 6px 0 0; }
  .empty-state {
    background: var(--paper-raised); border: 1px dashed var(--line);
    border-radius: 12px; padding: 40px 20px; text-align: center;
  }
  .empty-state .empty-icon { font-size: 32px; display: block; margin-bottom: 8px; }
  .empty-state p { margin: 0; color: var(--ink-soft); font-size: 14px; }
  .empty-state p strong { color: var(--ink); font-size: 16px; }
  .empty-state a { display: inline-flex; align-items: center; min-height: 44px; font-weight: 700; margin-top: 4px; }
  .pseudo {
    background: var(--moegi-soft); border-bottom: 2px solid var(--moegi-accent);
    border-radius: 3px; padding: 0 4px; font-weight: 600; color: var(--ink); white-space: nowrap;
  }

  /* ==== 進行表現（handoff: 唯一の意図的なJS is-submitting で切替） ==== */
  .progress-state { display: none; }
  .is-submitting .btn-primary, .is-submitting button[type="submit"] { display: none; }
  .is-submitting .progress-state {
    display: flex; align-items: center; gap: 12px;
    min-height: 48px; padding: 0 18px; border-radius: 8px;
    background: var(--moegi-soft); border: 1.5px solid var(--moegi);
    font-size: 15px; font-weight: 700; color: var(--moegi-deep);
    position: relative; overflow: hidden; flex: 1 1 auto;
  }
  .progress-state::before {
    content: ""; position: absolute; inset: 0;
    background: linear-gradient(90deg, transparent, color-mix(in oklch, var(--moegi) 18%, transparent), transparent);
    animation: progress-sweep 1.6s linear infinite;
  }
  @keyframes progress-sweep { from { transform: translateX(-100%); } to { transform: translateX(100%); } }
  .spinner {
    width: 18px; height: 18px; flex: none; border-radius: 50%;
    border: 2.5px solid var(--line); border-top-color: var(--moegi);
    animation: spin 0.9s linear infinite; position: relative;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .progress-state small { font-weight: 400; color: var(--ink-soft); font-size: 12px; }

  /* ==== ①ホーム ==== */
  h1 .brand-mark {
    width: 34px; height: 34px; border-radius: 8px; background: var(--moegi-accent); color: #333A32;
    display: inline-flex; align-items: center; justify-content: center; font-size: 18px; font-weight: 700; flex: none;
  }
  h1.brand { font-size: 26px; display: flex; align-items: center; gap: 12px; }
  .run-form {
    background: var(--paper-raised); border: 1px solid var(--line); border-radius: 12px;
    padding: 16px; display: flex; gap: 16px; align-items: flex-end; flex-wrap: wrap;
  }
  .run-form > div { flex: 1 1 320px; }
  .run-form select, .run-form input { width: 100%; }
  select, input[type="date"], input[type="month"] {
    font: inherit; font-size: 15px; color: var(--ink);
    background: var(--paper); border: 1px solid var(--line);
    border-radius: 8px; padding: 0 12px; min-height: 44px;
  }
  select:focus-visible, input:focus-visible { outline: 2px solid var(--focus); outline-offset: 0; border-color: var(--focus); }
  .run-hint { font-size: 12px; color: var(--ink-soft); margin: 8px 0 0; flex-basis: 100%; }
  .btn-primary { background: var(--moegi); color: var(--moegi-ink); flex: none; }
  .run-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
  .run-list li {
    background: var(--paper-raised); border: 1px solid var(--line); border-radius: 8px;
    padding: 10px 16px; display: flex; gap: 12px; align-items: center; flex-wrap: wrap; font-size: 14px;
  }
  .run-list .run-when { color: var(--ink-soft); font-size: 13px; flex: none; font-variant-numeric: tabular-nums; }
  .run-list .run-target { font-weight: 600; flex: 1 1 200px; }
  .run-list a { font-weight: 700; min-height: 44px; display: inline-flex; align-items: center; margin-left: auto; }
  .chip-progress { background: var(--status-edited-bg); color: var(--status-edited); }
  .chip-done { background: var(--status-approved-bg); color: var(--status-approved); }
  .nav-cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
  .nav-card {
    background: var(--paper-raised); border: 1px solid var(--line); border-radius: 12px;
    padding: 16px; text-decoration: none; display: flex; flex-direction: column; gap: 2px; min-height: 44px;
  }
  .nav-card:hover { background: var(--moegi-soft); border-color: var(--moegi); }
  .nav-card .nav-icon { font-size: 20px; }
  .nav-card .nav-name { font-weight: 700; color: var(--ink); }
  .nav-card .nav-desc { font-size: 12px; color: var(--ink-soft); line-height: 1.5; }

  /* ==== ③Before/After比較 ==== */
  .evidence-note {
    display: flex; gap: 10px; align-items: baseline;
    background: var(--moegi-soft); border: 1.5px solid var(--moegi);
    border-radius: 8px; padding: 12px 16px; font-size: 15px; font-weight: 700;
    margin-bottom: 24px;
  }
  .evidence-note small { font-weight: 400; font-size: 13px; color: var(--ink-soft); }
  .compare.diff > div { background: var(--paper-raised); border-radius: 12px; padding: 16px; }
  .compare.diff .after { background: var(--paper-raised); border-color: var(--moegi); font-weight: 400; }
  .col-head { font-size: 13px; font-weight: 700; color: var(--ink-soft); margin: 0 0 10px; display: flex; align-items: center; gap: 8px; }
  .compare.diff .after .col-head { color: var(--moegi-deep); }
  .check-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; font-size: 14px; }
  .check-list li { padding: 8px 12px; border-radius: 6px; border: 1px solid transparent; }
  .check-list li.hl-mod { background: var(--diff-mod); border-color: var(--diff-mod-border); }
  .check-list li.hl-add { background: var(--diff-add); border-color: var(--diff-add-border); }
  .check-list .kind { font-size: 11px; padding: 0 6px; margin-right: 6px; background: var(--paper-raised); border: 1px solid var(--diff-mod-border); color: var(--warn); }
  .check-list .kind.add { border-color: var(--diff-add-border); color: var(--diff-add-border); background: var(--paper-raised); }

  /* ==== ④日報の入力 ==== */
  details.run-warning {
    display: block; background: var(--moegi-soft); border: 1px solid var(--line);
    border-left: 1px solid var(--line); border-radius: 8px; margin: 0 0 10px; padding: 0;
  }
  details.run-warning summary {
    cursor: pointer; list-style: none; display: flex; align-items: center; gap: 8px;
    min-height: 44px; padding: 4px 14px; font-size: 14px; font-weight: 600;
  }
  details.run-warning summary::-webkit-details-marker { display: none; }
  details.run-warning summary::after {
    content: "開く"; margin-left: auto; font-size: 12px; font-weight: 400;
    color: var(--link); flex: none;
  }
  details.run-warning[open] summary::after { content: "閉じる"; }
  details.run-warning .warning-body { padding: 0 14px 12px; font-size: 13px; color: var(--ink-soft); margin: 0; }
  #nippou-form { background: none; border: 0; border-radius: 0; padding: 0; flex-direction: column; align-items: stretch; gap: 24px; margin-top: 20px; }
  #nippou-form > div, #nippou-form > fieldset { flex: 1 1 auto; }
  #nippou-form select, #nippou-form input[type="date"], #nippou-form textarea { font-size: 16px; min-height: 48px; background: var(--paper-raised); }
  #nippou-form textarea { min-height: 120px; padding: 12px 14px; }
  fieldset { border: 0; margin: 0; padding: 0; }
  legend.field-label { padding: 0; }
  .field-row { display: grid; grid-template-columns: 1fr; gap: 16px; }
  .procedure-buttons { display: flex; flex-wrap: wrap; gap: 10px; padding: 0; margin: 0; list-style: none; }
  .procedure-pill { position: relative; }
  .procedure-pill input {
    position: absolute; inset: 0; width: 100%; height: 100%;
    opacity: 0; margin: 0; cursor: pointer;
  }
  .procedure-pill span {
    display: inline-flex; align-items: center; gap: 6px;
    min-height: 44px; padding: 6px 18px;
    border: 1.5px solid var(--line); border-radius: 999px;
    background: var(--paper-raised); font-size: 15px; line-height: 1.4;
  }
  .procedure-pill span::before {
    content: ""; width: 14px; height: 14px; flex: none;
    border: 1.5px solid var(--ink-soft); border-radius: 4px;
  }
  .procedure-pill input:checked + span { background: var(--moegi-soft); border-color: var(--moegi); font-weight: 700; }
  .procedure-pill input:checked + span::before {
    content: "✓"; border-color: var(--moegi);
    background: var(--moegi); color: var(--moegi-ink);
    display: inline-flex; align-items: center; justify-content: center;
    font-size: 11px; font-weight: 700;
  }
  .procedure-pill input:focus-visible + span { outline: 2px solid var(--focus); outline-offset: 2px; }
  .record-controls {
    display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
    background: var(--paper-raised); border: 1px solid var(--line);
    border-radius: 12px; padding: 16px;
  }
  .record-btn {
    font: inherit; font-size: 16px; font-weight: 700; cursor: pointer;
    min-height: 48px; padding: 0 24px; border-radius: 999px;
    border: 1.5px solid var(--moegi); background: var(--paper-raised); color: var(--link);
    display: inline-flex; align-items: center; gap: 10px;
  }
  .record-btn:hover { background: var(--moegi-soft); color: var(--link); }
  .record-btn:active { background: var(--moegi-soft); color: var(--link); }
  .record-btn .rec-dot { width: 12px; height: 12px; border-radius: 50%; background: var(--danger); flex: none; }
  .record-controls.recording { border-color: var(--danger); background: var(--danger-soft); }
  .record-controls.recording .record-btn { border-color: var(--danger); color: var(--danger); background: var(--paper-raised); }
  .record-controls.recording .rec-dot { animation: rec-blink 1.2s ease-in-out infinite; }
  @keyframes rec-blink { 0%, 100% { opacity: 1; } 50% { opacity: .25; } }
  .record-status { font-size: 14px; color: var(--ink-soft); }
  .record-controls.recording .record-status { color: var(--danger); font-weight: 700; }
  .output-options {
    display: flex; flex-direction: column; gap: 8px;
    background: var(--paper-raised); border: 1px solid var(--line);
    border-radius: 12px; padding: 14px 16px; font-size: 15px;
  }
  .output-options .fixed { color: var(--ink-soft); display: flex; align-items: center; gap: 10px; min-height: 32px; }
  .output-options label { display: flex; align-items: center; gap: 10px; min-height: 44px; cursor: pointer; }
  .output-options input[type="checkbox"] { width: 22px; height: 22px; margin: 0; accent-color: var(--moegi); }
  .actions.stack { flex-direction: column; align-items: stretch; gap: 8px; }
  .actions.stack .btn-primary { width: 100%; min-height: 48px; font-size: 17px; }

  /* ==== ⑤⑥ Privacy Gate ==== */
  .gate-summary {
    border: 1px solid var(--line); border-radius: 12px;
    background: var(--paper-raised); padding: 16px; margin-bottom: 12px;
  }
  .gate-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .gate-icon { font-size: 20px; flex: none; }
  .gate-title { font-size: 16px; font-weight: 700; line-height: 1.4; }
  .gate-chip {
    display: inline-flex; align-items: center; gap: 4px;
    font-size: 12px; font-weight: 700; border-radius: 999px; padding: 2px 12px;
  }
  .gate-body { font-size: 14px; color: var(--ink-soft); margin: 8px 0 0; }
  .gate-detect { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
  .detect-chip {
    font-size: 12px; font-weight: 600; border-radius: 999px; padding: 2px 12px;
    background: var(--paper); border: 1px solid var(--line); color: var(--ink-soft);
  }
  .gate-summary.passed { border-color: var(--gate-passed); }
  .gate-summary.passed .gate-chip { background: var(--gate-passed-bg); color: var(--gate-passed); }
  .gate-summary.review { border-color: var(--gate-review); background: var(--gate-review-bg); }
  .gate-summary.review .gate-chip { background: var(--paper-raised); color: var(--gate-review); }
  .gate-summary.review .gate-body { color: var(--ink); }
  .gate-summary.blocked, .gate-summary.failed {
    border-color: var(--gate-blocked-fill);
    background: var(--gate-blocked-fill); color: #FFFFFF;
    padding: 20px 16px;
  }
  .gate-summary.blocked .gate-icon, .gate-summary.failed .gate-icon { font-size: 24px; }
  .gate-summary.blocked .gate-title, .gate-summary.failed .gate-title { font-size: 19px; }
  .gate-summary.blocked .gate-body, .gate-summary.failed .gate-body { color: #FFFFFF; opacity: .92; margin-top: 10px; }
  .gate-summary.blocked .gate-chip, .gate-summary.failed .gate-chip { background: #FFFFFF; color: var(--gate-blocked-fill); }
  .gate-summary.blocked .detect-chip, .gate-summary.failed .detect-chip {
    background: transparent; border-color: rgba(255,255,255,.55); color: #FFFFFF;
  }
  .no-send-note {
    display: flex; align-items: center; gap: 8px;
    font-size: 13px; color: var(--ink-soft);
    padding: 8px 4px; margin-bottom: 24px;
  }
  .save-note {
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
    background: var(--moegi-soft); border: 1px solid var(--line);
    border-radius: 8px; padding: 12px 16px; font-size: 14px; margin-bottom: 32px;
  }
  .save-note a { font-weight: 700; margin-left: auto; min-height: 44px; display: inline-flex; align-items: center; }
  .speech-text {
    background: var(--paper-raised); border: 1px solid var(--line); border-radius: 8px;
    padding: 14px 16px; font-size: 14px; line-height: 1.9;
  }
  .block-reasons {
    background: var(--paper-raised); border: 1px solid var(--gate-blocked);
    border-left: 4px solid var(--gate-blocked);
    border-radius: 0 8px 8px 0; padding: 14px 16px 14px 20px;
    margin: 0; font-size: 14px;
  }
  .block-reasons li { margin: 4px 0; }
  .block-reasons code {
    font-family: ui-monospace, monospace; font-size: 12px;
    background: var(--gate-blocked-bg); color: var(--gate-blocked);
    border-radius: 4px; padding: 1px 6px;
  }
  .tag-note { font-size: 12px; color: var(--ink-soft); margin: 0; }
  .tag-note code {
    font-family: ui-monospace, monospace; font-size: 11px;
    background: var(--moegi-soft); border-radius: 4px; padding: 1px 6px;
  }
  .back-link { font-size: 14px; }
  .back-link a { display: inline-flex; align-items: center; min-height: 44px; }

  /* ==== ⑤ 日報の結果 ==== */
  .nippou-table { display: flex; flex-direction: column; gap: 8px; }
  .nippou-row { background: var(--paper-raised); border: 1px solid var(--line); border-radius: 8px; padding: 10px 14px; }
  .nippou-item { font-size: 13px; font-weight: 700; display: block; }
  .nippou-value { display: flex; align-items: baseline; gap: 10px; margin-top: 2px; font-size: 14px; }
  .nippou-row.empty .nippou-value { color: var(--ink-soft); }
  .chip-filled { background: var(--gate-passed-bg); color: var(--gate-passed); }
  .chip-empty { background: var(--paper); border: 1px solid var(--line); color: var(--ink-soft); }
  .eval-list, .soap-list { display: flex; flex-direction: column; gap: 8px; }
  .eval-row, .soap-row { background: var(--paper-raised); border: 1px solid var(--line); border-radius: 8px; padding: 10px 14px; font-size: 14px; }
  .eval-label { font-size: 12px; font-weight: 700; color: var(--ink-soft); display: block; }
  .soap-key {
    display: inline-flex; align-items: center; justify-content: center;
    width: 28px; height: 28px; border-radius: 8px; flex: none;
    background: var(--moegi-soft); color: var(--moegi-deep);
    font-weight: 700; font-size: 14px; margin-right: 10px;
  }
  .soap-row { display: flex; align-items: flex-start; }
  .soap-row p { margin: 0; }

  /* ==== ⑦ 月報 ==== */
  .run-warning {
    display: flex; gap: 10px; align-items: baseline;
    background: var(--warn-soft); border-left: 4px solid var(--warn-icon);
    border-radius: 0 8px 8px 0; padding: 10px 16px; margin: 16px 0 0; font-size: 13px;
    color: var(--ink);
  }
  .method-note { font-size: 13px; color: var(--ink-soft); margin: 12px 0 28px; }
  .stat-chips { display: flex; gap: 12px; flex-wrap: wrap; }
  .stat-chip {
    display: inline-flex; align-items: baseline; gap: 8px;
    background: var(--moegi-soft); border: 1px solid var(--line);
    border-radius: 999px; padding: 8px 20px; font-size: 14px; font-weight: 600;
  }
  .stat-chip strong { font-size: 20px; font-weight: 700; color: var(--moegi-deep); }
  table {
    width: 100%; border-collapse: collapse; font-size: 14px;
    background: var(--paper-raised); border: 1px solid var(--line); border-radius: 8px; overflow: hidden;
  }
  th, td { text-align: left; padding: 8px 14px; border-bottom: 1px solid var(--line); }
  thead th { font-size: 12px; font-weight: 700; color: var(--ink-soft); background: var(--paper); }
  tbody tr:last-child td { border-bottom: 0; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  .rate-cell { display: flex; align-items: center; gap: 12px; }
  .rate-bar {
    flex: 1 1 120px; height: 10px; border-radius: 999px;
    background: var(--paper); border: 1px solid var(--line); overflow: hidden;
  }
  .rate-fill { height: 100%; background: var(--moegi); border-radius: 999px; }
  .rate-bar.zero { background: repeating-linear-gradient(-45deg, var(--paper), var(--paper) 4px, var(--line) 4px, var(--line) 5px); }
  .rate-num { flex: none; font-variant-numeric: tabular-nums; font-size: 13px; min-width: 84px; text-align: right; }
  .vital-chart { background: var(--paper-raised); border: 1px solid var(--line); border-radius: 8px; padding: 14px 16px; margin-bottom: 12px; }
  .vital-chart .chart-title { font-size: 13px; font-weight: 700; margin: 0 0 6px; }
  .vital-chart svg { width: 100%; height: auto; display: block; }
  .vital-chart .chart-note { font-size: 12px; color: var(--ink-soft); margin: 6px 0 0; }
  .table-scroll { overflow-x: auto; }
  .table-scroll table { min-width: 560px; }

  /* ==== ⑧ 配信キュー ==== */
  .pending-summary {
    display: flex; align-items: center; gap: 14px;
    background: var(--warn-soft); border: 1.5px solid var(--warn-icon);
    border-radius: 12px; padding: 16px 20px; margin-bottom: 8px;
  }
  .pending-summary .count { font-size: 32px; font-weight: 700; color: var(--warn); line-height: 1; }
  .pending-summary .desc { font-size: 14px; }
  .pending-summary .desc strong { display: block; font-size: 15px; }
  .auto-note { font-size: 13px; color: var(--ink-soft); margin: 0 0 24px; }
  .delivery-grid li {
    display: grid; grid-template-columns: auto 1fr auto; gap: 4px 16px; align-items: center;
    padding: 12px 16px;
  }
  .delivery-grid li.pending { border-left: 4px solid var(--warn-icon); }
  .delivery-grid .chip { justify-self: end; }
  .chip-sent { background: var(--status-approved-bg); color: var(--status-approved); }
  .chip-queued { background: var(--warn-soft); color: var(--warn); }
  .delivery-when { font-size: 13px; color: var(--ink-soft); font-variant-numeric: tabular-nums; }
  .delivery-meta { grid-column: 1 / -1; font-size: 12px; color: var(--ink-soft); }

  /* ==== ⑨ 監査記録 ==== */
  .audit { background: var(--paper-raised); border: 1px solid var(--line); border-radius: 8px; padding: 12px 16px; }
  .audit-head { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; font-size: 14px; }
  .audit-when { color: var(--ink-soft); font-size: 13px; font-variant-numeric: tabular-nums; }
  .chip-gate-passed  { background: var(--gate-passed-bg); color: var(--gate-passed); }
  .chip-gate-review  { background: var(--gate-review-bg); color: var(--gate-review); }
  .chip-gate-blocked { background: var(--gate-blocked-bg); color: var(--gate-blocked); }
  .chip-gate-failed  { background: var(--gate-failed-bg); color: var(--gate-failed); }
  .audit-detects { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
  .audit-meta { display: flex; gap: 6px 20px; flex-wrap: wrap; margin-top: 8px; font-size: 12px; color: var(--ink-soft); }
  .audit-meta strong { color: var(--ink); font-weight: 600; }

  @media (prefers-reduced-motion: reduce) {
    .rec-dot, .progress-state::before, .spinner { animation: none !important; }
  }
  @media (max-width: 600px) {
    body { font-size: 16px; }
    main { padding: 16px 16px 64px; }
    .proposal { padding: 18px 16px; }
    .diff { grid-template-columns: 1fr; }
    .actions button { flex: 1 1 40%; }
    .actions.stack button { flex: 1 1 auto; }
    .run-form > div { flex-basis: 100%; }
    .run-form .btn-primary { width: 100%; min-height: 48px; }
    .nav-cards { grid-template-columns: 1fr 1fr; }
    .run-list a { margin-left: 0; }
    .stat-chips { flex-direction: column; }
    .stat-chip { justify-content: space-between; display: flex; }
    .rate-num { min-width: 72px; }
    .delivery-grid li { grid-template-columns: 1fr auto; }
  }
  @media (min-width: 601px) {
    .field-row { grid-template-columns: 1fr 1fr; }
    .nippou-row { display: grid; grid-template-columns: 260px 1fr; gap: 12px; align-items: baseline; }
    .nippou-item { font-weight: 600; }
    .nippou-value { margin-top: 0; }
    .actions.stack .btn-primary { width: auto; align-self: flex-start; min-width: 280px; }
    .actions.stack .progress-state { align-self: stretch; }
  }
</style>
"""


def _render_page(body: str) -> HTMLResponse:
    nav = "<nav class='home-nav'><a href='/'>← MOEGI ホーム</a></nav>"
    body = _pending_delivery_badge() + body
    return HTMLResponse(
        f"<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<meta name='color-scheme' content='light dark'>"
        f"<title>MOEGI 承認UI</title>{_PAGE_STYLE}</head>"
        f"<body><main>{nav}{body}</main></body></html>"
    )


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    conn = _db_conn()
    try:
        # handoff §①: 過去の実行に「承認待ちn件／完了（n件承認）」チップと実行日時を出す
        past_runs = conn.execute(
            """
            SELECT proposals.run_id, MIN(changes.label), MIN(changes.kind),
                   SUM(proposals.status = '未承認'),
                   SUM(proposals.status IN ('承認', '編集済'))
            FROM proposals
            JOIN impacts ON impacts.row_id = proposals.impact_row_id
            JOIN changes ON changes.row_id = impacts.change_row_id
            GROUP BY proposals.run_id
            ORDER BY MAX(proposals.row_id) DESC
            LIMIT 20
            """
        ).fetchall()
        run_started_at = dict(
            conn.execute(
                "SELECT run_id, MIN(created_at) FROM cost_records GROUP BY run_id"
            ).fetchall()
        )
    finally:
        conn.close()

    targets = ingest.load_allowlist(str(pipeline._REPO_ROOT / "config" / "allowlist.toml"))
    label_options = "".join(
        f"<option value='{html.escape(t['label'])}'>{html.escape(t['label'])} — {html.escape(t['title'])}</option>"
        for t in targets
    )

    if past_runs:
        items = []
        for run_id, label, kind, pending_count, approved_count in past_runs:
            when = html.escape((run_started_at.get(run_id) or "")[:16])
            if pending_count:
                chip = f"<span class='chip chip-progress'>承認待ち {pending_count}件</span>"
                link_text = "更新案を見る →"
            else:
                chip = f"<span class='chip chip-done'>完了（{approved_count}件承認）</span>"
                link_text = "結果を見る →"
            items.append(
                f"<li><span class='run-when'>{when}</span>"
                f"<span class='run-target'>対象条文 {html.escape(label)}（{html.escape(kind)}）</span>"
                f"{chip}<a href='/runs/{html.escape(run_id)}'>{link_text}</a></li>"
            )
        run_list = "<ul class='run-list'>" + "".join(items) + "</ul>"
    else:
        run_list = """
        <div class="empty-state">
          <span class="empty-icon">🌱</span>
          <p><strong>まだ分析結果がありません</strong></p>
          <p>上のフォームから対象条文を選んで、最初の分析を実行してください。</p>
        </div>"""

    body = f"""
    <h1 class="brand"><span class="brand-mark">萌</span>MOEGI</h1>
    <p class="page-sub">通達の改定をチェックリストに反映し、承認された変更を現場の記録まで届けます。</p>

    <section>
      <h2>新しい分析を実行</h2>
      <form class="run-form" method="post" action="/runs" onsubmit="this.classList.add('is-submitting')">
        <div>
          <label class="field-label" for="label-select">対象条文</label>
          <select id="label-select" name="label">{label_options}</select>
        </div>
        <button type="submit" class="btn-primary">分析を実行</button>
        <div class="progress-state" role="status">
          <span class="spinner"></span>
          <span>更新案を生成中… <small>1分ほどかかる場合があります。画面を閉じないでください。</small></span>
        </div>
        <p class="run-hint">改定条文と現行チェックリストを突き合わせ、更新案を生成します（約47秒。クラウド呼び出しが失敗した場合のみローカルへ自動フォールバックし、その場合は数分かかります）。</p>
      </form>
    </section>

    <section>
      <h2>過去の実行</h2>
      {run_list}
    </section>

    <section>
      <h2>機能</h2>
      <div class="nav-cards">
        <a class="nav-card" href="/nippou"><span class="nav-icon">🎙</span><span class="nav-name">日報の入力</span><span class="nav-desc">音声・テキストから日報とSOAPを作成</span></a>
        <a class="nav-card" href="/monthly"><span class="nav-icon">📊</span><span class="nav-name">月報</span><span class="nav-desc">1ヶ月分の日報を集計</span></a>
        <a class="nav-card" href="/privacy/audits"><span class="nav-icon">🔎</span><span class="nav-name">監査記録</span><span class="nav-desc">仮名化処理の記録を確認</span></a>
        <a class="nav-card" href="/deliveries"><span class="nav-icon">📮</span><span class="nav-name">配信キュー</span><span class="nav-desc">承認済み変更の配信状況</span></a>
      </div>
    </section>
    """
    return _render_page(body)


@app.post("/runs")
def create_run(label: str = Form(...)) -> RedirectResponse:
    """design.md §10 D4「薄いE2E」。CLIと同じ経路（allowlist→pipeline.run→store）を
    Web起点で通す。models.toml の設定（既定はローカル・単発）をそのまま使う。
    """
    allowlist_path = pipeline._REPO_ROOT / "config" / "allowlist.toml"
    targets = ingest.load_allowlist(str(allowlist_path))
    old_notice = ingest.load_notice(
        str(pipeline._REPO_ROOT / "data" / "notices" / "old_hohatsu0305-12.pdf"),
        id="old",
        title="旧通達",
        issued_on="",
        version="old",
        source_url="",
    )
    new_notice = ingest.load_notice(
        str(pipeline._REPO_ROOT / "data" / "notices" / "new_hohatsu0305-19.pdf"),
        id="new",
        title="新通達",
        issued_on="",
        version="new",
        source_url="",
    )
    old_sections = ingest.apply_allowlist(old_notice, targets)
    new_sections = ingest.apply_allowlist(new_notice, targets)
    sitedoc = pipeline.load_sitedoc()

    conn = _db_conn()
    run_id = store.new_run_id()
    try:
        with llm.LLMAdapter() as adapter:
            result = pipeline.run(
                old_sections=old_sections,
                new_sections=new_sections,
                sitedoc=sitedoc,
                adapter=adapter,
                run_id=run_id,
                store_conn=conn,
                labels={label},
                # eval-loop（2026-08-12深夜・N-8対策）で45/48=93.8%まで実測検証した設定。
                # orchestrate単独では効果が実測ノイズと区別できなかったが、プロンプト改訂
                # （根拠文つき・普遍原則化）後は一貫して寄与しているため本番既定にする。
                orchestrate=True,
            )
        store.save_pipeline_result(conn, run_id, result)
    finally:
        conn.close()

    return RedirectResponse(url=f"/runs/{run_id}", status_code=303)


def _trace_html(row: dict) -> str:
    """design.md §3 G2「根拠が辿れる」。新旧本文を並べ、根拠の引用箇所をハイライトする。"""
    new_text = html.escape(row["new_text"])
    quote = row["evidence_quote"]
    if quote:
        # ledger N-10: quote は _segment_notice_text の分割結果からコード側で取り出した
        # 実在する文そのものなので、常に new_text 中に厳密一致する。
        escaped_quote = html.escape(quote)
        new_text = new_text.replace(escaped_quote, f"<mark>{escaped_quote}</mark>")

    if row["old_text"] is not None:
        old_block = (
            f"<div class='trace-text'><span class='field-label'>旧版（{html.escape(row['old_path'] or '')}）</span>"
            f"{html.escape(row['old_text'])}</div>"
        )
    else:
        old_block = (
            "<div class='trace-text'><span class='field-label'>旧版</span>"
            "（完全新設。旧版に対応条文なし）</div>"
        )

    return f"""
    <details class="trace">
      <summary>▸ 原文トレース（条文: {html.escape(row['new_path'])}）</summary>
      {old_block}
      <div class="trace-text"><span class="field-label">新版（{html.escape(row['new_path'])}）</span>{new_text}</div>
    </details>
    """


#: 提案ステータス（日本語・DB上の値）→ デザインの data-status / チップクラス
#: （docs/design_handoff_moegi_ui/02-approval.html。CSSセレクタは英語の値で書かれている）
_PROPOSAL_STATUS_ATTRS = {
    "未承認": ("pending", "chip-pending"),
    "承認": ("approved", "chip-approved"),
    "却下": ("rejected", "chip-rejected"),
    "編集済": ("edited", "chip-edited"),
}

_VERDICT_CHIP_CLASSES = {"要更新": "chip-need", "判定不能": "chip-unknown"}


def _kind_chip_html(kind: str) -> str:
    """変更種別チップ。色だけに頼らず「変」「＋」の記号を併記する（色覚多様性対応）。"""
    if kind == "追加":
        return "<span class='kind kind-add'>＋ 追加</span>"
    return f"<span class='kind'>変　{html.escape(kind)}</span>"


def _proposal_html(row: dict) -> str:
    item_label = row["sitedoc_item_id"] or "（新規項目の提案）"
    before = html.escape(row["before"]) if row["before"] else "（現在この項目はチェックリストに存在しない）"
    after = html.escape(row["after"])
    row_id = row["row_id"]
    status = row["status"]
    data_status, status_chip = _PROPOSAL_STATUS_ATTRS.get(status, ("pending", "chip-pending"))
    verdict_chip = _VERDICT_CHIP_CLASSES.get(row["verdict"], "chip-pending")

    # 操作ボタン: 現在のステータスに該当する操作は「◯◯済み」の押せないボタンにする。
    # 承認済み・却下カードは編集フォームを出さない（handoff §②: 編集するなら却下して差し戻す）。
    if status == "承認":
        actions = f"""
      <div class="actions">
        <button type="button" class="btn-secondary" disabled>承認済み</button>
        <form method="post" action="/proposals/{row_id}/reject"><button type="submit" class="btn-secondary">却下</button></form>
      </div>
      <div class="edit-form">
        <span class="field-label">確定した文言</span>
        <p class="confirmed-text">{after}</p>
        <p class="confirmed-note">承認済みのため編集できません（編集する場合は却下して差し戻してください）。</p>
      </div>"""
    elif status == "却下":
        actions = f"""
      <div class="actions">
        <form method="post" action="/proposals/{row_id}/approve"><button type="submit" class="btn-secondary">承認</button></form>
        <button type="button" class="btn-secondary" disabled>却下済み</button>
      </div>"""
    else:  # 未承認・編集済
        actions = f"""
      <div class="actions">
        <form method="post" action="/proposals/{row_id}/approve"><button type="submit" class="btn-primary">承認</button></form>
        <form method="post" action="/proposals/{row_id}/reject"><button type="submit" class="btn-secondary">却下</button></form>
      </div>
      <form class="edit-form" method="post" action="/proposals/{row_id}/edit">
        <label class="field-label" for="after-{row_id}">文言を編集</label>
        <textarea id="after-{row_id}" name="after" rows="4">{after}</textarea>
        <button type="submit" class="btn-primary">編集して保存</button>
      </form>"""

    return f"""
    <article class="proposal" data-status="{data_status}">
      <p class="meta">{_kind_chip_html(row['change_kind'])} ・ 対象項目 <strong>{html.escape(item_label)}</strong></p>
      <div class="chips">
        <span class="chip {verdict_chip}">{html.escape(row['verdict'])}</span>
        <span class="chip {status_chip}">{html.escape(status)}</span>
        <span class="confidence">信頼度 {row['confidence']:.2f}</span>
      </div>
      <p class="rationale">{html.escape(row['rationale'])}</p>
      <div class="diff">
        <div class="before"><span class="field-label">BEFORE</span>{before}</div>
        <div class="after"><span class="field-label">AFTER</span>{after}</div>
      </div>
      {_trace_html(row)}
      {actions}
    </article>
    """


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def review_run(run_id: str) -> HTMLResponse:
    conn = _db_conn()
    try:
        rows = store.list_proposals_for_review(conn, run_id)
        fax_history = store.list_fax_deliveries(conn, run_id)
        queue_items = [d for d in store.list_deliveries(conn) if d["run_id"] == run_id]
    finally:
        conn.close()

    if not rows:
        return _render_page(
            f"<h1>更新案の承認</h1><p class='run-sub'>run_id {html.escape(run_id)}</p>"
            "<p>更新案がありません。</p>"
        )

    # handoff §②: FAX生成と配信履歴（FAX・メールキュー）を1つのフッターにまとめる
    history_items = "".join(
        f"<li><span>{html.escape(d['generated_at'])}</span>"
        f"<span>FAX様式PDF ・ 更新案 {len(d['proposal_row_ids'])} 件</span>"
        f"<span class='chip chip-approved'>生成済み</span></li>"
        for d in fax_history
    ) + "".join(
        f"<li><span>{html.escape(d['created_at'])}</span>"
        f"<span>宛先 {html.escape(d['recipient'])} ・ チャネル {html.escape(d['channel'])}</span>"
        f"<span class='chip {'chip-approved' if d['status'] == 'sent' else 'chip-pending'}'>"
        f"{'送信済み' if d['status'] == 'sent' else '送信待ち'}</span></li>"
        for d in queue_items
    )
    history_html = (
        f"<ul class='delivery-list'>{history_items}</ul>"
        if history_items
        else "<p class='meta'>配信履歴はまだありません。</p>"
    )
    footer = f"""
    <footer class="run-footer">
      <h2>配信・出力</h2>
      <div class="actions">
        <form method="get" action="/runs/{html.escape(run_id)}/fax-preview">
          <button type="submit" class="btn-secondary">FAXプレビューPDFを生成</button>
        </form>
      </div>
      {history_html}
    </footer>
    """

    first = rows[0]
    body = (
        "<h1>更新案の承認</h1>"
        f"<p class='run-sub'>run_id {html.escape(run_id)} ・ "
        f"対象条文 {html.escape(first['change_label'])}（{html.escape(first['change_kind'])}）</p>"
    )
    body += "".join(_proposal_html(row) for row in rows)
    body += footer
    return _render_page(body)


@app.get("/runs/{run_id}/fax-preview")
def fax_preview(run_id: str) -> FileResponse:
    """design.md §5.3 即時チャネル。承認済み更新案からFAX様式PDFを生成し画面プレビューする。

    生成のたびに fax_deliveries へ配信履歴（監査ログ）を1件残す（ユーザー決定・2026-08-11、
    ledger R-76）。PDF自体は毎回再生成する既存仕様のままなので、「いつ・どのproposalを
    含んだPDFを作ったか」はこの履歴だけが記録している。
    """
    conn = _db_conn()
    try:
        rows = store.list_proposals_for_review(conn, run_id)
        approved = [r for r in rows if r["status"] == "承認"]
        out_path = _FAX_OUTPUT_DIR / f"{run_id}.pdf"
        fax.generate_fax_pdf("◯◯訪問看護ステーション", approved, out_path)
        store.save_fax_delivery(
            conn,
            run_id=run_id,
            proposal_row_ids=[r["row_id"] for r in approved],
            file_path=str(out_path),
        )
    finally:
        conn.close()

    return FileResponse(out_path, media_type="application/pdf", filename=f"fax_preview_{run_id}.pdf")


def _redirect_to_run(conn, proposal_row_id: int) -> RedirectResponse:
    run_id = conn.execute(
        "SELECT run_id FROM proposals WHERE row_id = ?", (proposal_row_id,)
    ).fetchone()[0]
    return RedirectResponse(url=f"/runs/{run_id}", status_code=303)


def _enqueue_approved_notice(conn, proposal_row_id: int) -> None:
    """承認された更新案を配信キューへ積む（design.md G8・ledger R-89）。

    オフラインでも「積む」ことは必ず成功する——これが store-and-forward の要で、
    回線の有無に関わらず承認操作が完結する。実際に送るのは背景の自動フラッシュ。

    ⚠️ delivery.enqueue() は本文を検査し、識別子が混じっていたら積むこと自体を拒否する。
       拒否された場合は承認自体は成立させ、配信だけ行わない（承認を巻き戻すと
       「承認したのに承認されていない」というもっと分かりにくい状態になる）。
    """
    row = store.get_proposal_for_review(conn, proposal_row_id)
    if row is None:
        return
    run_id_row = conn.execute(
        "SELECT run_id FROM proposals WHERE row_id = ?", (proposal_row_id,)
    ).fetchone()
    subject, body = delivery.format_proposal_notice([row])
    today = date.today()
    try:
        delivery.enqueue(
            conn,
            run_id=run_id_row[0] if run_id_row else "",
            subject=subject,
            body=body,
            proposal_row_ids=[proposal_row_id],
            config=DELIVERY_CONFIG,
            today=(today.year, today.month, today.day),
        )
    except delivery.DeliveryRefused:
        pass  # 例外の中身はここで捨てる（本文・宛先を握ったまま外へ流さない）


@app.post("/proposals/{proposal_row_id}/approve")
def approve_proposal(proposal_row_id: int) -> RedirectResponse:
    conn = _db_conn()
    try:
        store.update_proposal_status(conn, proposal_row_id, "承認")
        _enqueue_approved_notice(conn, proposal_row_id)
        return _redirect_to_run(conn, proposal_row_id)
    finally:
        conn.close()


@app.post("/proposals/{proposal_row_id}/reject")
def reject_proposal(proposal_row_id: int) -> RedirectResponse:
    conn = _db_conn()
    try:
        store.update_proposal_status(conn, proposal_row_id, "却下")
        return _redirect_to_run(conn, proposal_row_id)
    finally:
        conn.close()


@app.post("/proposals/{proposal_row_id}/edit")
def edit_proposal(proposal_row_id: int, after: str = Form(...)) -> RedirectResponse:
    conn = _db_conn()
    try:
        store.update_proposal_status(conn, proposal_row_id, "編集済", after=after)
        return _redirect_to_run(conn, proposal_row_id)
    finally:
        conn.close()


def _nippou_record_html(record: nippou.NippouRecord) -> str:
    """日報の構造化結果（handoff §⑤: モバイル縦2段カード・600px以上で2カラムgrid）。"""
    rows = []
    if record.procedures:
        procedures = "、".join(record.procedures)
        rows.append(
            "<div class='nippou-row'>"
            "<span class='nippou-item'>処置内容（ボタン選択）</span>"
            f"<span class='nippou-value'><span class='chip chip-filled'>✓ 記入済み</span>{html.escape(procedures)}</span></div>"
        )
    for item in record.items:
        if item.filled:
            rows.append(
                "<div class='nippou-row'>"
                f"<span class='nippou-item'>{html.escape(item.checklist_item_id)}</span>"
                f"<span class='nippou-value'><span class='chip chip-filled'>✓ 記入済み</span>{html.escape(item.value)}</span></div>"
            )
        else:
            rows.append(
                "<div class='nippou-row empty'>"
                f"<span class='nippou-item'>{html.escape(item.checklist_item_id)}</span>"
                "<span class='nippou-value'><span class='chip chip-empty'>－ 未記入</span>発話から読み取れませんでした</span></div>"
            )

    evaluation_html = ""
    if record.evaluation:
        ev = record.evaluation
        evaluation_html = f"""
    <section>
      <h2>評価</h2>
      <div class="eval-list">
        <div class="eval-row"><span class="eval-label">看護目標</span>{html.escape(ev.goal_ref or '（参照なし）')}</div>
        <div class="eval-row"><span class="eval-label">達成度</span>{html.escape(ev.achievement)}</div>
        <div class="eval-row"><span class="eval-label">効果</span>{html.escape(ev.effect or '（記載なし）')}</div>
      </div>
    </section>"""

    return f"""
    <section>
      <h2>日報</h2>
      <div class="nippou-table">{"".join(rows)}</div>
      <p class="section-hint">未記入の項目は月報作成前に追記できます。</p>
    </section>
    {evaluation_html}
    """


@app.get("/nippou", response_class=HTMLResponse)
def nippou_form() -> HTMLResponse:
    procedure_checkboxes = "".join(
        f"<li class='procedure-pill'><input type='checkbox' name='procedures' value='{html.escape(p)}'>"
        f"<span>{html.escape(p)}</span></li>"
        for p in _PROCEDURE_OPTIONS
    )
    body = f"""
    <h1>日報の入力</h1>

    <details class="run-warning">
      <summary>🖥 音声とテキストは、この端末とローカルサーバーの中で処理されます</summary>
      <p class="warning-body">録音した音声・入力したテキストは事業所内のサーバーで文字起こし・構造化され、
      外部のクラウドサービスには送信されません（gemma4:26b・ledger N-11）。電波が無い場所でも録音自体はでき、
      あとで構造化できます。</p>
    </details>
    <details class="run-warning">
      <summary>🔎 保存の前に Privacy Gate の検査を通ります</summary>
      <p class="warning-body">構造化の前に、氏名・電話番号などの直接識別子を機械的に検出して仮名化します
      （現在のモード: {html.escape(PRIVACY_CONFIG.mode)}）。仮名化後も病状・服薬などの要配慮個人情報は残るため、
      結果画面で内容を確認してください。記録に残せない情報（保険証番号など）を検出した場合、処理は停止します。
      <a href="/privacy/audits">監査記録を見る</a></p>
    </details>

    <form method="post" action="/nippou/structure" enctype="multipart/form-data" id="nippou-form"
          class="run-form" onsubmit="this.classList.add('is-submitting')">
      <div class="field-row">
        <div>
          <label class="field-label" for="subject-code">利用者コード</label>
          <select id="subject-code" name="subject_code">{_patient_code_options()}</select>
        </div>
        <div>
          <label class="field-label" for="visited-on">訪問日</label>
          <input type="date" id="visited-on" name="visited_on" value="{date.today().isoformat()}">
        </div>
      </div>

      <fieldset>
        <legend class="field-label">処置内容（当てはまるものをすべて選択）</legend>
        <ul class="procedure-buttons">{procedure_checkboxes}</ul>
      </fieldset>

      <div>
        <span class="field-label">音声で記録</span>
        <div class="record-controls" id="record-controls">
          <button type="button" class="record-btn" id="record-btn"><span class="rec-dot"></span><span id="record-btn-label">録音を開始</span></button>
          <span class="record-status" id="record-status">待機中（タップで録音開始）</span>
        </div>
        <p class="field-hint">録音中はボタンが赤枠に変わり、●が点滅します。もう一度タップで停止。</p>
      </div>
      <input type="file" name="audio" id="audio-input" accept="audio/*" style="display:none">

      <div>
        <label class="field-label" for="utterance_text">またはテキストで直接入力</label>
        <textarea id="utterance_text" name="utterance_text"
          placeholder="例: PATIENT_001さん、血圧128の76、体温36.5度。左踵の褥瘡はサイズ縮小。長女さんに服薬の説明を実施。"></textarea>
      </div>

      <div>
        <span class="field-label">出力形式</span>
        <div class="output-options">
          <span class="fixed">📄 日報 — 常に作成されます</span>
          <label><input type="checkbox" name="soap" checked> SOAP（S/O/A/P）も作成する</label>
        </div>
      </div>

      <div class="actions stack">
        <button type="submit" class="btn-primary">構造化する</button>
        <div class="progress-state" role="status">
          <span class="spinner"></span>
          <span>構造化を実行中… <small>30秒ほどかかる場合があります。画面を閉じないでください。</small></span>
        </div>
      </div>
    </form>
    <script>
    (function() {{
      var recordBtn = document.getElementById('record-btn');
      var recordBtnLabel = document.getElementById('record-btn-label');
      var recordControls = document.getElementById('record-controls');
      var statusEl = document.getElementById('record-status');
      var audioInput = document.getElementById('audio-input');
      var mediaRecorder = null;
      var chunks = [];

      if (!navigator.mediaDevices || !window.MediaRecorder) {{
        recordBtn.disabled = true;
        statusEl.textContent = 'このブラウザは録音に対応していません。テキスト入力をご利用ください。';
        return;
      }}

      // iOS Safari 18.3以前はaudio/webmを録音できず、audio/mp4（AAC）にしか対応しない
      // （18.4以降はwebmも対応）。isTypeSupportedで実機ごとに使える形式を選ぶ。
      // 参照: https://webkit.org/blog/11353/mediarecorder-api/
      function pickMimeType() {{
        var candidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/aac'];
        for (var i = 0; i < candidates.length; i++) {{
          if (MediaRecorder.isTypeSupported(candidates[i])) return candidates[i];
        }}
        return '';  // どれも明示サポートが無ければブラウザ既定に任せる
      }}

      // 実際に使われたmimeTypeから拡張子を決める。nippou.pyの_AUDIO_SUFFIXESに
      // .m4a・.webmは既にあるため、mp4系はm4aへ寄せる（webm以外は.m4aで統一）。
      function extensionFor(mimeType) {{
        if (mimeType.indexOf('webm') !== -1) return 'webm';
        if (mimeType.indexOf('ogg') !== -1) return 'ogg';
        return 'm4a';
      }}

      recordBtn.addEventListener('click', function() {{
        if (mediaRecorder && mediaRecorder.state === 'recording') {{
          mediaRecorder.stop();
          return;
        }}
        navigator.mediaDevices.getUserMedia({{audio: true}}).then(function(stream) {{
          chunks = [];
          var mimeType = pickMimeType();
          mediaRecorder = mimeType ? new MediaRecorder(stream, {{mimeType: mimeType}}) : new MediaRecorder(stream);
          mediaRecorder.ondataavailable = function(e) {{ chunks.push(e.data); }};
          mediaRecorder.onstop = function() {{
            var actualType = mediaRecorder.mimeType || mimeType || 'audio/webm';
            var ext = extensionFor(actualType);
            var blob = new Blob(chunks, {{type: actualType}});
            var file = new File([blob], 'nippou.' + ext, {{type: actualType}});
            var dt = new DataTransfer();
            dt.items.add(file);
            audioInput.files = dt.files;
            stream.getTracks().forEach(function(t) {{ t.stop(); }});
            recordControls.classList.remove('recording');
            recordBtnLabel.textContent = '録音を開始';
            statusEl.textContent = '録音完了。内容を確認したら「構造化する」を押してください。';
          }};
          mediaRecorder.start();
          recordControls.classList.add('recording');
          recordBtnLabel.textContent = '録音を停止';
          statusEl.textContent = '録音中（タップで停止）';
        }}).catch(function(err) {{
          statusEl.textContent = 'マイクを使用できませんでした: ' + err.message;
        }});
      }});
    }})();
    </script>
    """
    return _render_page(body)


def _soap_record_html(record: nippou.SoapRecord) -> str:
    if record.undecidable:
        return (
            "<section><h2>SOAP</h2>"
            "<div class='run-warning'>⚠️ <span>ローカルLLMがSOAP構造化を完了できませんでした（判定不能）。"
            "内容を確認して、もう一度お試しください。</span></div></section>"
        )
    rows = "".join(
        f"<div class='soap-row'><span class='soap-key'>{key}</span>"
        + (f"<p>{html.escape(value)}</p>" if value else "<p class='meta'>（該当する記載なし）</p>")
        + "</div>"
        for key, value in (
            ("S", record.subjective),
            ("O", record.objective),
            ("A", record.assessment),
            ("P", record.plan),
        )
    )
    return f"""
    <section>
      <h2>SOAP</h2>
      <div class="soap-list">{rows}</div>
    </section>
    """


#: Privacy Gate の状態 → 表示クラス・アイコン・見出し（handoff §⑤⑥）。
#: blocked/failed だけが赤塗りバナー（ページ内唯一の赤塗り）。failed は ❓ で区別する。
_GATE_VIEWS = {
    privacy.PrivacyGateStatus.PASSED: ("passed", "🔎", "Privacy Gate: 検査通過"),
    privacy.PrivacyGateStatus.REVIEW_REQUIRED: ("review", "⚠️", "Privacy Gate: 確認が必要です"),
    privacy.PrivacyGateStatus.BLOCKED: ("blocked", "⛔", "Privacy Gate: 処理を停止しました"),
    privacy.PrivacyGateStatus.FAILED: ("failed", "❓", "Privacy Gate: 検査を完了できませんでした"),
    privacy.PrivacyGateStatus.PENDING: ("review", "⚠️", "Privacy Gate: 検査中です"),
}

#: 状態ごとの本文（handoff の文言。「匿名化済み」「完全に安全」とは書かない）。
_GATE_MESSAGES = {
    privacy.PrivacyGateStatus.PASSED: (
        "直接識別子を仮名化しました。仮名化後も病状・服薬などの要配慮個人情報は残ります。"
        "内容を確認してから共有してください。"
    ),
    privacy.PrivacyGateStatus.REVIEW_REQUIRED: (
        "個人を特定し得る情報が残っている可能性があります。下記の検出結果を確認し、"
        "修正してから再検査してください。"
    ),
    privacy.PrivacyGateStatus.BLOCKED: (
        "記録に残せない情報を検出したため、構造化を中止しました。日報・SOAPは保存されていません。"
    ),
    privacy.PrivacyGateStatus.FAILED: (
        "検査自体が完了しなかったため、構造化を中止しました。データは保存されていません。"
        "時間をおいて再度お試しください。"
    ),
    privacy.PrivacyGateStatus.PENDING: "検査中です。",
}

#: 停止・確認の理由コード → 現場向けの日本語（値そのものは決して出さない）。
_REASON_MESSAGES = {
    "empty_input": "入力が空です",
    "input_too_long": "入力が長すぎます",
    "mojibake_input": "文字化けした入力を検出しました",
    "dictionary_unavailable": "事業所マスタ辞書を読み込めませんでした",
    "ambiguous_name": "同姓同名など、一意に特定できない氏名があります",
    "excessive_removal": "元の内容から削られた量が多く、必要な情報まで消えた可能性があります",
    "residual_risk_above_threshold": "残存リスクが許容値を超えています",
    "vault_save_failed": "仮名対応表を保存できませんでした",
    "llm_timeout": "ローカルLLMの補完検出がタイムアウトしました",
    "llm_unavailable": "ローカルLLMへ接続できませんでした",
    "llm_invalid_json": "ローカルLLMの応答形式が不正でした",
    "llm_skipped_blocked": "記録できない情報を先に検出したため、ローカルLLMは呼び出していません",
    "residual_registered_name": "仮名化後にも登録済みの氏名が残っています",
    "clinical_numbers_dropped": "バイタルなどの数値が仮名化の前後で消えています",
}

#: 検出種別 → 現場向けの日本語（利用者はこの画面で英語の型名を読まされる理由がない）。
_DETECTION_TYPE_LABELS = {
    "phone": "電話番号",
    "email": "メールアドレス",
    "postal_code": "郵便番号",
    "address": "住所",
    "birthdate": "生年月日",
    "insurance_number": "保険証番号",
    "medical_card_number": "診察券番号",
    "patient_id": "利用者ID",
    "ip_address": "IPアドレス",
    "url_credential": "URL内の認証情報",
    "api_key": "APIキー・秘密情報",
    "jwt": "トークン",
    "person_name": "氏名",
    "facility": "施設・医療機関名",
}


def _detection_type_label(type_: str) -> str:
    """検出種別を日本語にする。`llm:` 接頭辞はローカルLLMが見つけた分。"""
    if type_.startswith("llm:"):
        base = type_.removeprefix("llm:")
        return f"{_DETECTION_TYPE_LABELS.get(base, base)}（文脈から検出）"
    return _DETECTION_TYPE_LABELS.get(type_, type_)


def _reason_text(code: str) -> str:
    if code in _REASON_MESSAGES:
        return _REASON_MESSAGES[code]
    if code.startswith("schema_"):
        return "ローカルLLMの応答がスキーマに違反しました"
    if code.startswith("blocked_"):
        return f"記録できない情報を検出しました（{code.removeprefix('blocked_')}）"
    if code.startswith("residual_"):
        return f"仮名化後にも検出されました（{code.removeprefix('residual_')}）"
    return code


def _gate_summary_html(result: privacy.GateResult) -> str:
    """Privacy Gate サマリ（handoff §⑤⑥: 同一コンポーネントの状態違い）。

    検出した「値」は出さず、種別と件数だけを出す。権限のない利用者に元の値を
    見せないための最低条件で、画面に出さないものは画面から漏れない。
    """
    css, icon, title = _GATE_VIEWS[result.status]
    detects = "".join(
        f"<span class='detect-chip'>{html.escape(_detection_type_label(t))} {n}件</span>"
        for t, n in sorted(result.detected_counts.items())
    )
    detects_html = f"<div class='gate-detect'>{detects}</div>" if detects else ""
    shadow_note = ""
    if result.mode != "enforce" and result.would_block:
        shadow_note = (
            "<div class='run-warning'>⚠️ <span>段階導入モード"
            f"（{html.escape(result.mode)}）のため処理を継続しました。"
            "本番（enforce）ではここで停止します。</span></div>"
        )
    role = " role='alert'" if css in ("blocked", "failed") else ""
    return f"""
    <div class="gate-summary {css}"{role}>
      <div class="gate-head">
        <span class="gate-icon">{icon}</span>
        <span class="gate-title">{html.escape(title)}</span>
        <span class="gate-chip">{html.escape(result.status.value)}</span>
      </div>
      <p class="gate-body">{html.escape(_GATE_MESSAGES[result.status])}</p>
      {detects_html}
      <p class="gate-body"><small>request_id {html.escape(result.request_id[:8])} ・
        ポリシー {html.escape(result.policy_version)} ・ モード {html.escape(result.mode)}</small></p>
    </div>
    <p class="no-send-note">🔒 外部LLMへの送信は行われていません（仮名化・構造化ともローカル実行）。</p>
    {shadow_note}
    """


def _block_reasons_html(result: privacy.GateResult) -> str:
    """停止・確認の理由（handoff §⑥）。コード名も併記して問い合わせ時に特定できるようにする。"""
    if not result.reason_codes:
        return ""
    title = (
        "停止した理由"
        if result.status in (privacy.PrivacyGateStatus.BLOCKED, privacy.PrivacyGateStatus.FAILED)
        else "確認が必要な理由"
    )
    items = "".join(
        f"<li>{html.escape(_reason_text(code))}（<code>{html.escape(code)}</code>）</li>"
        for code in result.reason_codes
    )
    return f"""
    <section>
      <h2>{title}</h2>
      <ul class="block-reasons">
        {items}
        <li>検出した値そのものは表示・保存されず、種別と件数のみを記録しています</li>
      </ul>
    </section>
    """


def _recheck_form_html(sanitized_text: str, context: "_RecordContext") -> str:
    """§12「修正して再検査」。仮名化済みテキストを人が直して再検査する。"""
    hidden = "".join(
        f"<input type='hidden' name='procedures' value='{html.escape(p)}'>"
        for p in context.procedures
    )
    if context.want_soap:
        hidden += "<input type='hidden' name='soap' value='on'>"
    hidden += f"<input type='hidden' name='subject_code' value='{html.escape(context.subject_code)}'>"
    hidden += f"<input type='hidden' name='visited_on' value='{html.escape(context.visited_on)}'>"
    return f"""
    <section>
    <h2>修正して再検査</h2>
    <form class="edit-form" method="post" action="/nippou/recheck"
          onsubmit="this.classList.add('is-submitting')">
      {hidden}
      <p class="field-hint">下のテキストは直接識別子を仮名化した内容です。検出された箇所とその前後の
        言い回しを、その情報に触れない表現に書き直してから再検査してください（元の値は表示されません）。</p>
      <label class="field-label" for="retry-text">仮名化済みテキスト</label>
      <textarea id="retry-text" name="sanitized_text" rows="6">{html.escape(sanitized_text)}</textarea>
      <p class="tag-note">タグの意味: <code>[[INSURANCE_NUMBER]]</code> 保険証番号 ／
        <code>[[PHONE]]</code> 電話番号 ／ <code>[[ADDRESS]]</code> 住所 ／ <code>[[EMAIL]]</code> メールアドレス</p>
      <div class="actions stack">
        <button type="submit" class="btn-primary">修正して再検査</button>
        <div class="progress-state" role="status">
          <span class="spinner"></span>
          <span>再検査を実行中… <small>30秒ほどかかる場合があります。画面を閉じないでください。</small></span>
        </div>
      </div>
    </form>
    </section>
    <p class="back-link"><a href="/nippou">← 日報の入力からもう一度試す</a></p>
    """


def _run_privacy_gate(text: str, *, adapter) -> privacy.GateResult:
    """Privacy Gate を実行し、監査記録を残す（本文は記録しない）。"""
    vault_conn = None
    try:
        vault_conn = privacy_vault.connect()
    except Exception:  # noqa: BLE001 — 対応表DBを開けないこと自体は gate 側が fail-closed で扱う
        vault_conn = None
    try:
        return privacy.run_gate(text, config=PRIVACY_CONFIG, adapter=adapter, vault_conn=vault_conn)
    finally:
        if vault_conn is not None:
            vault_conn.close()


def _save_privacy_audit(result: privacy.GateResult, *, proceeded: bool) -> None:
    conn = _db_conn()
    try:
        store.save_privacy_audit(
            conn,
            request_id=result.request_id,
            gate_status=result.status.value,
            mode=result.mode,
            policy_version=result.policy_version,
            detected_counts=result.detected_counts,
            needs_human_review=result.needs_human_review,
            residual_risk=result.residual_risk,
            reason_codes=result.reason_codes,
            duration_ms=result.duration_ms,
            local_model=result.local_model,
            proceeded_to_structuring=proceeded,
            external_transmission=result.external_transmission,
        )
    finally:
        conn.close()


@dataclasses.dataclass(frozen=True)
class _RecordContext:
    """1回の訪問記録に付随する、LLMを通さない確定値（ledger R-77・R-84）。

    利用者コード・訪問日・処置内容はいずれも人が選んだ値で、LLMの推定に委ねない。
    """

    procedures: list[str]
    want_soap: bool
    subject_code: str
    visited_on: str


def _save_nippou_record(
    result: privacy.GateResult,
    context: _RecordContext,
    record: nippou.NippouRecord,
    soap: nippou.SoapRecord | None,
) -> bool:
    """日報・SOAPの構造化結果を保存する（月報の集計材料・ledger R-84）。

    利用者コードが選ばれていない場合は保存しない——「誰の記録か分からない日報」を
    DBへ入れると月報の集計が静かに狂う。保存できなかったことは画面に出す。
    """
    if not context.subject_code:
        return False
    subject_pseudonym = privacy_vault.stable_pseudonym(context.subject_code)
    conn = _db_conn()
    try:
        store.save_nippou_record(
            conn,
            request_id=result.request_id,
            subject_pseudonym=subject_pseudonym,
            visited_on=context.visited_on,
            gate_status=result.status.value,
            checklist_version=record.checklist_version,
            policy_version=result.policy_version,
            items=[dataclasses.asdict(i) for i in record.items],
            evaluation=dataclasses.asdict(record.evaluation) if record.evaluation else None,
            procedures=list(record.procedures),
            soap=dataclasses.asdict(soap) if soap is not None else None,
        )
    finally:
        conn.close()
    return True


def _structure_and_render(result: privacy.GateResult, context: _RecordContext) -> HTMLResponse:
    """Privacy Gate の結果を受けて、通過していれば既存の構造化処理を呼ぶ。

    **この関数がセキュリティ層と既存処理の唯一の接続点**。
    cleared_for_downstream() が False の場合、agent.structure() /
    structure_soap() を一切呼ばず、日報の保存も行わずに戻る（fail-closed）。
    """
    if not privacy.cleared_for_downstream(result):
        _save_privacy_audit(result, proceeded=False)
        # handoff §⑥: 停止画面は見出しを置かず、赤塗りバナーを最上部に据える
        body = (
            _gate_summary_html(result)
            + _block_reasons_html(result)
            + _recheck_form_html(result.sanitized_text, context)
        )
        return _render_page(body)

    sitedoc = pipeline.load_sitedoc()
    adapter = llm.LLMAdapter()
    try:
        agent = nippou.NippouAgent(adapter)
        agent.set_checklist(sitedoc.items)
        utterance_id = result.request_id[:8]
        record = agent.structure(
            result.sanitized_text, utterance_id=utterance_id, procedures=context.procedures
        )
        soap = (
            agent.structure_soap(
                result.sanitized_text, utterance_id=utterance_id, procedures=context.procedures
            )
            if context.want_soap
            else None
        )
    finally:
        adapter.close()

    _save_privacy_audit(result, proceeded=True)
    saved = _save_nippou_record(result, context, record, soap)

    if saved:
        what = "日報とSOAP" if soap is not None else "日報"
        saved_html = (
            f"<div class='save-note'>✓ {what}を保存しました"
            f"（利用者 {html.escape(context.subject_code)} ・ {html.escape(context.visited_on)}）。"
            f"<a href='/monthly'>月報を見る →</a></div>"
        )
    else:
        saved_html = (
            "<div class='run-warning'>⚠️ <span>利用者コードが選ばれていないため、この記録は保存していません"
            "（月報の集計に入りません）。</span></div>"
        )

    subject = html.escape(context.subject_code) if context.subject_code else "（利用者コード未選択）"
    body = (
        "<h1>日報の結果</h1>"
        f"<p class='page-sub'>{subject} ・ 訪問日 {html.escape(context.visited_on)}</p>"
        + _gate_summary_html(result)
        + saved_html
        + "<section><h2>仮名化済みの発話内容</h2>"
        + f"<div class='speech-text'>{_highlight_pseudonyms(result.sanitized_text)}</div>"
        + "<p class='section-hint'>ハイライト部分が仮名化された箇所です。</p></section>"
        + _nippou_record_html(record)
        + (_soap_record_html(soap) if soap is not None else "")
        + "<p class='back-link'><a href='/nippou'>← もう一度入力する</a></p>"
    )
    return _render_page(body)


@app.post("/nippou/structure", response_class=HTMLResponse)
def nippou_structure(
    utterance_text: str = Form(""),
    procedures: list[str] = Form([]),
    soap: str = Form(""),
    subject_code: str = Form(""),
    visited_on: str = Form(""),
    audio: UploadFile | None = File(None),
) -> HTMLResponse:
    """文字起こし → **Privacy Gate** → 既存の日報・SOAP構造化。

    design/moegi/privacy.md §4 のデータフローどおり、既存処理の直前に検査層を挟む。
    ⚠️ Privacy Gate のモード（observe/shadow/enforce）はリクエストから受け取らない。
       一般利用者がリクエスト単位で安全検査を無効化できないようにするため、
       設定ファイルと環境変数だけが変更経路になっている（privacy/config.py）。
    """
    context = _RecordContext(
        procedures=procedures,
        want_soap=bool(soap),
        subject_code=subject_code.strip(),
        visited_on=_normalize_visited_on(visited_on),
    )
    adapter = llm.LLMAdapter()
    tmp_path: Path | None = None
    try:
        agent = nippou.NippouAgent(adapter)
        if audio is not None and audio.filename:
            suffix = Path(audio.filename).suffix or ".webm"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(audio.file.read())
                tmp_path = Path(tmp.name)
            text = agent.transcribe(tmp_path)
        else:
            text = agent.transcribe(utterance_text)
        gate_result = _run_privacy_gate(text, adapter=adapter)
    finally:
        adapter.close()
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        # 原文はここから先へ持ち出さない（ローカル変数を明示的に手放す）。
        text = ""

    return _structure_and_render(gate_result, context)


@app.post("/nippou/recheck", response_class=HTMLResponse)
def nippou_recheck(
    sanitized_text: str = Form(...),
    procedures: list[str] = Form([]),
    soap: str = Form(""),
    subject_code: str = Form(""),
    visited_on: str = Form(""),
) -> HTMLResponse:
    """§12「修正して再検査」。人が直した仮名化済みテキストを、同じGateにもう一度通す。

    再検査でも検査の厳しさは変えない（人が触ったから緩める、をしない）。
    """
    context = _RecordContext(
        procedures=procedures,
        want_soap=bool(soap),
        subject_code=subject_code.strip(),
        visited_on=_normalize_visited_on(visited_on),
    )
    adapter = llm.LLMAdapter()
    try:
        gate_result = _run_privacy_gate(sanitized_text, adapter=adapter)
    finally:
        adapter.close()
    return _structure_and_render(gate_result, context)


_SAMPLE_UTTERANCE = (
    "訪問終了しました。血圧128の78、体温36.5度、脈拍70、呼吸数16。"
    "褥瘡の処置を実施しました。ご本人は痛みが和らいだとおっしゃっています。"
    "目標にしていた在宅療養の継続については、ご家族の協力もあって順調に経過しています。"
)


def _check_list_html(
    items: list[pipeline.ChecklistItem], *, modified: set[str], added: set[str]
) -> str:
    """handoff §③: 変更点は色（hl-mod/hl-add）＋記号（変/＋）の併用で示す。"""
    rows = []
    for item in items:
        if item.label in added:
            rows.append(
                f"<li class='hl-add'><span class='kind add'>＋</span>{html.escape(item.label)}</li>"
            )
        elif item.label in modified:
            rows.append(
                f"<li class='hl-mod'><span class='kind mod'>変</span>{html.escape(item.label)}</li>"
            )
        else:
            rows.append(f"<li>{html.escape(item.label)}</li>")
    return "<ul class='check-list'>" + "".join(rows) + "</ul>"


def _compare_record_html(record: nippou.NippouRecord, highlight_items: set[str]) -> str:
    """構造化結果の片側。変更後にだけ現れた項目（G6の落ち）は hl-add で目立たせる。"""
    rows = []
    for item in record.items:
        hl = " style='background:var(--diff-add); border-color:var(--diff-add-border);'" if (
            item.checklist_item_id in highlight_items
        ) else ""
        chip = (
            "<span class='chip chip-filled'>✓ 記入</span>"
            if item.filled
            else "<span class='chip chip-empty'>－ 未記入</span>"
        )
        value = html.escape(item.value) if item.value else "—"
        rows.append(
            f"<div class='nippou-row{' empty' if not item.filled else ''}'{hl}>"
            f"<span class='nippou-item'>{html.escape(item.checklist_item_id)}</span>"
            f"<span class='nippou-value'>{chip}{value}</span></div>"
        )
    if record.evaluation is not None:
        ev = record.evaluation
        chip = (
            "<span class='chip chip-filled'>✓ 記入</span>"
            if ev.filled
            else "<span class='chip chip-empty'>－ 未記入</span>"
        )
        rows.append(
            "<div class='nippou-row' style='background:var(--diff-add); border-color:var(--diff-add-border);'>"
            "<span class='nippou-item'>目標に対する評価</span>"
            f"<span class='nippou-value'>{chip}{html.escape(ev.achievement)}"
            f"{('（' + html.escape(ev.effect) + '）') if ev.effect else ''}</span></div>"
        )
    return "<div class='nippou-table'>" + "".join(rows) + "</div>"


def _before_after_form_html(run_id: str, utterance: str = "", goal_ref: str = "") -> str:
    return f"""
    <section>
    <h2>同じ発話を、変更前／変更後のチェックリストで処理する</h2>
    <form class="edit-form" method="post" action="/runs/{html.escape(run_id)}/before-after"
          onsubmit="this.classList.add('is-submitting')">
      <label class="field-label" for="ba-utterance">発話内容</label>
      <textarea id="ba-utterance" name="utterance_text" rows="4">{html.escape(utterance or _SAMPLE_UTTERANCE)}</textarea>
      <label class="field-label" for="ba-goal">看護目標（新規の「評価」項目に紐づけます）</label>
      <textarea id="ba-goal" name="goal_ref" rows="2">{html.escape(goal_ref or "在宅療養の継続")}</textarea>
      <p class="field-hint">発話は Privacy Gate を通してから、両方のチェックリストで構造化します。</p>
      <div class="actions stack">
        <button type="submit" class="btn-primary">Before / After を比較する</button>
        <div class="progress-state" role="status">
          <span class="spinner"></span>
          <span>2回の構造化を実行中… <small>30秒ほどかかる場合があります。画面を閉じないでください。</small></span>
        </div>
      </div>
    </form>
    </section>
    """


def _load_checklist_diff(run_id: str, goal_ref: str) -> checklist.ChecklistDiff:
    conn = _db_conn()
    try:
        rows = store.list_proposals_for_review(conn, run_id)
    finally:
        conn.close()
    return checklist.apply_approved(pipeline.load_sitedoc(), rows, goal_ref=goal_ref)


def _checklist_diff_html(diff: checklist.ChecklistDiff) -> str:
    notes = []
    if not diff.has_changes:
        notes.append(
            "このrunにはまだ承認済みの更新案がありません。先に更新案を承認してください。"
        )
    if diff.unmatched:
        notes.append(
            f"承認済みだが現在のチェックリストに該当項目が見つからなかった更新案が "
            f"{len(diff.unmatched)} 件あります（項目ラベルが既に書き換わっている可能性）。"
        )
    notes_html = "".join(f"<div class='run-warning'>⚠️ <span>{html.escape(n)}</span></div>" for n in notes)
    modified_before = {before for before, _ in diff.changed}
    modified_after = {after for _, after in diff.changed}
    return f"""
    {notes_html}
    <section>
    <h2>チェックリストの変更点</h2>
    <div class="diff compare">
      <div class="before">
        <p class="col-head">改定前（BEFORE）</p>
        {_check_list_html(diff.before.items, modified=modified_before, added=set())}
      </div>
      <div class="after">
        <p class="col-head">改定後（AFTER）</p>
        {_check_list_html(diff.after.items, modified=modified_after, added=set(diff.added))}
      </div>
    </div>
    </section>
    """


def _before_after_header(run_id: str, diff: checklist.ChecklistDiff) -> str:
    return (
        "<h1>Before / After 比較</h1>"
        f"<p class='page-sub'>run_id {html.escape(run_id)} ・ "
        f"<a href='/runs/{html.escape(run_id)}'>このrunの承認画面へ戻る</a></p>"
        "<div class='evidence-note'>🎙 <span>入力音声は同一。差はチェックリストのみ。<br>"
        "<small>同じ発話を改定前後のチェックリストで構造化し、結果の違いだけを比較しています。</small></span></div>"
    )


@app.get("/runs/{run_id}/before-after", response_class=HTMLResponse)
def before_after_form(run_id: str) -> HTMLResponse:
    """G6 Before/After 比較画面の入力フォーム。"""
    diff = _load_checklist_diff(run_id, "在宅療養の継続")
    body = (
        _before_after_header(run_id, diff)
        + _checklist_diff_html(diff)
        + _before_after_form_html(run_id)
    )
    return _render_page(body)


@app.post("/runs/{run_id}/before-after", response_class=HTMLResponse)
def before_after_compare(
    run_id: str, utterance_text: str = Form(""), goal_ref: str = Form("")
) -> HTMLResponse:
    """同一の発話を、変更前／変更後のチェックリストでそれぞれ構造化して並べる（G6）。

    ここでも Privacy Gate は迂回しない。仮名化に通らない発話は構造化を1回も行わない。
    """
    diff = _load_checklist_diff(run_id, goal_ref.strip())

    adapter = llm.LLMAdapter()
    try:
        gate_result = _run_privacy_gate(utterance_text, adapter=adapter)
    finally:
        adapter.close()

    header = _before_after_header(run_id, diff)

    if not privacy.cleared_for_downstream(gate_result):
        body = (
            header
            + _gate_summary_html(gate_result)
            + "<p class='run-warning'>⚠️ <span>セキュリティ検査に通らなかったため、構造化は行っていません。</span></p>"
            + _checklist_diff_html(diff)
        )
        return _render_page(body)

    adapter = llm.LLMAdapter()
    try:
        agent = nippou.NippouAgent(adapter)
        agent.set_checklist(diff.before.items)
        before_record = agent.structure(gate_result.sanitized_text, utterance_id="before")
        agent.set_checklist(diff.after.items)
        after_record = agent.structure(gate_result.sanitized_text, utterance_id="after")
    finally:
        adapter.close()

    _save_privacy_audit(gate_result, proceeded=True)

    # G6の落ち: 変更後にだけ現れた項目（追加・文言変更後）をハイライトする
    appeared = set(diff.added) | {after for _, after in diff.changed}
    body = (
        header
        + _gate_summary_html(gate_result)
        + _checklist_diff_html(diff)
        + "<section><h2>同じ発話の構造化結果</h2>"
        + f"<div class='speech-text'>{_highlight_pseudonyms(gate_result.sanitized_text)}</div>"
        + "<p class='section-hint'>ハイライト部分が仮名化された箇所です。</p>"
        + "<div class='diff compare' style='margin-top:16px;'>"
        + f"<div class='before'><p class='col-head'>改定前のチェックリストで構造化</p>{_compare_record_html(before_record, set())}</div>"
        + f"<div class='after'><p class='col-head'>改定後のチェックリストで構造化</p>{_compare_record_html(after_record, appeared)}</div>"
        + "</div></section>"
        + _before_after_form_html(run_id, utterance_text, goal_ref)
    )
    return _render_page(body)


def _monthly_form_html(subject_code: str = "", month: str = "") -> str:
    """月報の条件入力。**POSTで送る**——利用者コードをURLのクエリに載せないため。"""
    return f"""
    <form class="run-form" method="post" action="/monthly">
      <div>
        <label class="field-label" for="monthly-subject">利用者コード</label>
        <select id="monthly-subject" name="subject_code">{_patient_code_options(subject_code)}</select>
      </div>
      <div>
        <label class="field-label" for="monthly-month">対象月</label>
        <input type="month" id="monthly-month" name="month"
               value="{html.escape(month or date.today().strftime('%Y-%m'))}">
      </div>
      <button type="submit" class="btn-primary">月報を作成</button>
    </form>
    <p class="method-note">集計は日報の記録から決定論的に行っています（LLMは使用していません）。
      実名は保持しておらず、集計単位は利用者コードから導出した安定仮名です。</p>
    """


def _vital_chart_svg(report: monthly.MonthlyReport) -> str:
    """バイタル折れ線（handoff §⑦: インラインSVG直書き・数値表が正、グラフは補助）。

    y軸は 40〜160 固定（血圧・脈拍を1軸に載せるデザインどおり）。範囲外はクランプする。
    点が2つ未満なら折れ線にならないので出さない。
    """
    points = [v for v in report.vitals if v.systolic or v.pulse]
    if len(points) < 2:
        return ""

    left, right, top, bottom = 80, 640, 20, 150

    def x_at(index: int) -> float:
        return left + index * (right - left) / (len(points) - 1)

    def y_at(value: float) -> float:
        clamped = max(40.0, min(160.0, float(value)))
        return bottom - (clamped - 40.0) * (bottom - top) / 120.0

    def polyline(values: list, color: str, dashed: bool = False) -> str:
        coords = " ".join(
            f"{x_at(i):.0f},{y_at(v):.0f}" for i, v in enumerate(values) if v is not None
        )
        if coords.count(" ") < 1:
            return ""
        dash = " stroke-dasharray='4 3'" if dashed else ""
        return f"<polyline fill='none' stroke='{color}' stroke-width='2'{dash} points='{coords}'/>"

    lines = (
        polyline([v.systolic for v in points], "var(--moegi)")
        + polyline([v.diastolic for v in points], "var(--moegi-accent)")
        + polyline([v.pulse for v in points], "var(--ink-soft)", dashed=True)
    )

    # 日付ラベルは最大4つに間引く（月/日だけ）
    label_indexes = sorted({0, len(points) - 1, len(points) // 3, 2 * len(points) // 3})
    date_labels = "".join(
        f"<text x='{x_at(i):.0f}' y='166' text-anchor='middle' font-size='10' fill='var(--ink-soft)'>"
        f"{html.escape(points[i].visited_on[5:].replace('-', '/'))}</text>"
        for i in label_indexes
    )

    note = ""
    if report.unparsed_vital_visits:
        note = (
            f"<p class='chart-note'>読み取れなかった訪問（{report.unparsed_vital_visits}件）は"
            "グラフに含まれません。</p>"
        )

    return f"""
    <div class="vital-chart">
      <p class="chart-title">血圧（収縮期/拡張期）と脈拍 — 補助グラフ（数値は下の表が正）</p>
      <svg viewBox="0 0 720 180" role="img" aria-label="血圧と脈拍の折れ線グラフ">
        <line x1="{left - 40}" y1="{top}" x2="{left - 40}" y2="{bottom}" stroke="var(--line)" stroke-width="1"/>
        <line x1="{left - 40}" y1="{bottom}" x2="700" y2="{bottom}" stroke="var(--line)" stroke-width="1"/>
        <text x="{left - 46}" y="{top + 6}" text-anchor="end" font-size="10" fill="var(--ink-soft)">160</text>
        <text x="{left - 46}" y="{(top + bottom) / 2 + 4:.0f}" text-anchor="end" font-size="10" fill="var(--ink-soft)">100</text>
        <text x="{left - 46}" y="{bottom + 2}" text-anchor="end" font-size="10" fill="var(--ink-soft)">40</text>
        {lines}
        <text x="700" y="{y_at(points[-1].systolic) if points[-1].systolic else 55:.0f}" text-anchor="end" font-size="11" fill="var(--moegi)">収縮期</text>
        <text x="700" y="{y_at(points[-1].diastolic) + 14 if points[-1].diastolic else 107:.0f}" text-anchor="end" font-size="11" fill="var(--moegi-accent)">拡張期</text>
        <text x="700" y="{y_at(points[-1].pulse) + 28 if points[-1].pulse else 130:.0f}" text-anchor="end" font-size="11" fill="var(--ink-soft)">脈拍(破線)</text>
        {date_labels}
      </svg>
      {note}
    </div>
    """


def _monthly_report_html(report: monthly.MonthlyReport, subject_code: str) -> str:
    if report.is_empty:
        return f"""
        <div class="empty-state">
          <span class="empty-icon">🗒</span>
          <p><strong>{report.year}年{report.month}月の日報はまだありません</strong></p>
          <p>利用者 {html.escape(subject_code)} のこの月の日報が保存されると、ここに月報が表示されます。</p>
          <a href="/nippou">日報を入力する →</a>
        </div>"""

    procedures = (
        "".join(
            f"<tr><td>{html.escape(name)}</td><td class='num'>{count}</td></tr>"
            for name, count in sorted(report.procedure_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        or "<tr><td colspan='2' class='meta'>処置の選択がありませんでした。</td></tr>"
    )

    fill_rates = "".join(
        f"<tr><td>{html.escape(r.label)}</td><td>"
        f"<div class='rate-cell'><div class='rate-bar{' zero' if r.total and not r.filled else ''}'>"
        f"<div class='rate-fill' style='width:{r.rate * 100:.0f}%'></div></div>"
        f"<span class='rate-num'>{r.filled}/{r.total} ・ {r.rate * 100:.0f}%</span></div>"
        f"</td></tr>"
        for r in report.item_fill_rates
    )

    def _cell(value) -> str:
        return "—" if value is None else html.escape(str(value))

    vitals = (
        "".join(
            f"<tr><td>{html.escape(v.visited_on[5:].replace('-', '/'))}</td>"
            f"<td class='num'>{_cell(v.systolic)}{'/' if v.systolic else ''}{_cell(v.diastolic) if v.systolic else ''}</td>"
            f"<td class='num'>{_cell(v.temperature)}{'℃' if v.temperature else ''}</td>"
            f"<td class='num'>{_cell(v.pulse)}</td>"
            f"<td class='num'>{_cell(v.spo2)}{'%' if v.spo2 else ''}</td></tr>"
            for v in report.vitals
        )
        or "<tr><td colspan='5' class='meta'>数値として読み取れたバイタルがありませんでした。</td></tr>"
    )

    notes = []
    if report.unparsed_vital_visits:
        notes.append(
            f"バイタルを読み取れなかった訪問が <strong>{report.unparsed_vital_visits}件</strong> あります。"
            "数値表では「—」と表示しています（0件として集計していません）。"
        )
    if report.non_passed_records:
        notes.append(
            f"Privacy Gate が passed 以外だった記録が <strong>{report.non_passed_records}件</strong> 含まれています"
            "（段階導入モードで保存されたもの）。"
        )
    if len(report.policy_versions) > 1:
        notes.append(
            "複数の仮名化ポリシーバージョンの記録が混在しています（"
            + "、".join(html.escape(v) for v in report.policy_versions)
            + "）。"
        )
    notes_html = "".join(f"<div class='run-warning'>⚠️ <span>{n}</span></div>" for n in notes)

    return f"""
    <section>
      <h2>{html.escape(subject_code)} ・ {report.year}年{report.month}月</h2>
      <div class="stat-chips">
        <span class="stat-chip">訪問 <strong>{report.visit_count}</strong> 回</span>
        <span class="stat-chip">訪問日数 <strong>{report.visit_days}</strong> 日</span>
        <span class="stat-chip">評価の記入 <strong>{report.evaluation_filled}/{report.evaluation_total}</strong></span>
      </div>
      {notes_html}
      <p class="section-hint">安定仮名 {html.escape(report.subject_pseudonym)}</p>
    </section>

    <section>
      <h2>処置別の実施回数</h2>
      <table>
        <thead><tr><th>処置</th><th class="num">実施回数</th></tr></thead>
        <tbody>{procedures}</tbody>
      </table>
    </section>

    <section>
      <h2>チェックリスト項目の記入率</h2>
      <table>
        <thead><tr><th>項目</th><th>記入率（記入あり日報 / 全日報）</th></tr></thead>
        <tbody>{fill_rates}</tbody>
      </table>
    </section>

    <section>
      <h2>バイタルの推移</h2>
      {_vital_chart_svg(report)}
      <div class="table-scroll">
        <table>
          <thead><tr><th>日付</th><th class="num">血圧</th><th class="num">体温</th><th class="num">脈拍</th><th class="num">SpO2</th></tr></thead>
          <tbody>{vitals}</tbody>
        </table>
      </div>
    </section>
    """


@app.get("/monthly", response_class=HTMLResponse)
def monthly_form() -> HTMLResponse:
    """月報の作成フォーム（ledger R-84）。"""
    body = f"""
    <h1>月報</h1>
    <p class="page-sub">利用者コードと対象月を選んで作成します。</p>
    {_monthly_form_html()}
    """
    return _render_page(body)


@app.post("/monthly", response_class=HTMLResponse)
def monthly_report(subject_code: str = Form(...), month: str = Form(...)) -> HTMLResponse:
    """月報を表示する。

    GETではなくPOSTで受けるのは、利用者コードをURLのクエリ文字列に載せないため
    （ブラウザ履歴・アクセスログ・Referer に識別子が残るのを避ける）。
    """
    subject_code = subject_code.strip()
    try:
        year, month_number = (int(part) for part in month.split("-", 1))
        date(year, month_number, 1)  # 妥当な年月かをここで確定させる
    except (ValueError, TypeError):
        today = date.today()
        year, month_number = today.year, today.month

    subject_pseudonym = privacy_vault.stable_pseudonym(subject_code)
    conn = _db_conn()
    try:
        records = store.list_nippou_records_for_month(
            conn, subject_pseudonym=subject_pseudonym, year=year, month=month_number
        )
    finally:
        conn.close()

    report = monthly.build(
        records, subject_pseudonym=subject_pseudonym, year=year, month=month_number
    )
    body = (
        "<h1>月報</h1>"
        "<p class='page-sub'>利用者コードと対象月を選んで作成します。</p>"
        + _monthly_form_html(subject_code, f"{year:04d}-{month_number:02d}")
        + _monthly_report_html(report, subject_code)
    )
    return _render_page(body)


@app.get("/deliveries", response_class=HTMLResponse)
def deliveries_page() -> HTMLResponse:
    """配信キュー（design.md G8・ledger R-89）。

    ⚠️ **手動の再送ボタンは置かない**（R-16）。「消えたのではなく待っている」ことが
    見えれば十分で、人が押さないと届かないなら store-and-forward の意味が無い。
    """
    conn = _db_conn()
    try:
        rows = store.list_deliveries(conn)
        pending = store.count_pending_deliveries(conn)
    finally:
        conn.close()

    if pending:
        summary = f"""
        <div class="pending-summary">
          <span class="count">{pending}</span>
          <span class="desc"><strong>送信待ちがあります</strong>回線が復帰すると自動で送信されます。操作は必要ありません。</span>
        </div>"""
    else:
        summary = ""

    channel_note = (
        f"送信先はメール（{html.escape(DELIVERY_CONFIG.recipient)}）です。"
        if DELIVERY_CONFIG.channel == delivery.CHANNEL_EMAIL
        else "SMTPが未設定のため、送信は data/outbox/ への書き出しになります。"
    )

    if not rows:
        listing = """
        <div class="empty-state">
          <span class="empty-icon">📮</span>
          <p><strong>配信はまだありません</strong></p>
          <p>更新案が承認されると、配信がここに表示されます。</p>
        </div>"""
    else:
        items = []
        for r in rows:
            is_pending = r["status"] != "sent"
            chip = (
                "<span class='chip chip-queued'>⏳ 送信待ち</span>"
                if is_pending
                else "<span class='chip chip-sent'>✓ 送信済み</span>"
            )
            meta_parts = []
            if r["attempts"]:
                meta_parts.append(f"試行 {r['attempts']}回")
            if r["sent_at"]:
                meta_parts.append(f"送信 {html.escape(r['sent_at'])}")
            elif is_pending:
                meta_parts.append(f"自動で再試行します（{DELIVERY_FLUSH_INTERVAL_SECONDS:.0f}秒間隔）")
            meta = (
                f"<span class='delivery-meta'>{' ・ '.join(meta_parts)}</span>" if meta_parts else ""
            )
            items.append(
                f"<li{' class=\'pending\'' if is_pending else ''}>"
                f"<span class='delivery-when'>{html.escape(r['created_at'])}</span>"
                f"<span>宛先 {html.escape(r['recipient'])} ・ チャネル {html.escape(r['channel'])}</span>"
                f"{chip}{meta}</li>"
            )
        listing = "<ul class='run-list delivery-grid'>" + "".join(items) + "</ul>"

    body = f"""
    <h1>配信キュー</h1>
    <p class="page-sub">承認済みの変更が現場の記録画面に届くまでの状況です。{channel_note}</p>
    {summary}
    <p class="auto-note">送信は自動でリトライされます（このページに再送ボタンはありません —
      押さないと届かない仕組みにしないためです）。積まれるのは承認済み更新案のみで、
      日報・SOAP・文字起こし原文・仮名対応表は積みません。</p>
    {listing}
    """
    return _render_page(body)


@app.get("/privacy/audits", response_class=HTMLResponse)
def privacy_audits() -> HTMLResponse:
    """仮名化処理の監査記録（design/moegi/privacy.md §10）。

    このテーブルには本文も対応表も入っていないので、そのまま画面に出せる。
    """
    conn = _db_conn()
    try:
        audits = store.list_privacy_audits(conn)
    finally:
        conn.close()

    if not audits:
        listing = """
        <div class="empty-state">
          <span class="empty-icon">🔎</span>
          <p><strong>監査記録はまだありません</strong></p>
          <p>日報の構造化を実行すると、仮名化処理の記録がここに残ります。</p>
        </div>"""
    else:
        gate_chips = {
            "passed": ("chip-gate-passed", "✓"),
            "review_required": ("chip-gate-review", "⚠"),
            "blocked": ("chip-gate-blocked", "⛔"),
            "failed": ("chip-gate-failed", "❓"),
        }
        items = []
        for a in audits:
            chip_css, chip_icon = gate_chips.get(a["gate_status"], ("chip-gate-review", "⚠"))
            detects = "".join(
                f"<span class='detect-chip'>{html.escape(_detection_type_label(t))} {n}件</span>"
                for t, n in sorted(a["detected_counts"].items())
            )
            detects_html = f"<div class='audit-detects'>{detects}</div>" if detects else ""
            proceeded = "進んだ" if a["proceeded_to_structuring"] else "中止"
            external = "あり" if a["external_transmission"] else "なし"
            items.append(
                "<li class='audit'>"
                "<div class='audit-head'>"
                f"<span class='audit-when'>{html.escape(a['occurred_at'])}</span>"
                f"<span class='chip {chip_css}'>{chip_icon} {html.escape(a['gate_status'])}</span>"
                "</div>"
                f"{detects_html}"
                "<div class='audit-meta'>"
                f"<span>処理時間 <strong>{a['duration_ms'] / 1000:.1f}秒</strong></span>"
                f"<span>構造化へ <strong>{proceeded}</strong></span>"
                f"<span>外部送信 <strong>{external}</strong></span>"
                f"<span>request_id <strong>{html.escape(a['request_id'][:8])}</strong></span>"
                f"<span>ポリシー <strong>{html.escape(a['policy_version'])}</strong></span>"
                "</div></li>"
            )
        listing = "<ul class='run-list'>" + "".join(items) + "</ul>"

    body = f"""
    <h1>仮名化処理の監査記録</h1>
    <p class="page-sub">検出した個人情報は種別と件数のみを記録しています。値そのものは保存されません。</p>
    {listing}
    """
    return _render_page(body)

