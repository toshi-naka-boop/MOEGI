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
        f"<p class='run-warning'>📮 <strong>送信待ち {pending} 件</strong>。"
        "回線が復帰すると自動で送信されます（手動の再送操作は必要ありません）。"
        " <a href='/deliveries'>配信キューを見る</a></p>"
    )


_PAGE_STYLE = """
<style>
  :root {
    --ink: #1c1f1a;
    --ink-soft: #565f4f;
    --paper: #fbfaf5;
    --paper-raised: #ffffff;
    --line: #dde0d2;
    --moegi: #7a9c2e;
    --moegi-soft: #eef3e1;
    --moegi-deep: #55701f;
    --warn: #a8641b;
    --warn-soft: #fbf1e2;
    --danger: #a8391f;
    --status-approved: #3f7d3a;
    --status-approved-bg: #eef6ea;
    --status-rejected: #7a7f70;
    --status-rejected-bg: #f1f1ec;
    --status-edited: #3a5f8a;
    --status-edited-bg: #eaf1f8;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --ink: #eceee6; --ink-soft: #b4bba8; --paper: #191c15; --paper-raised: #22261c;
      --line: #363c2c; --moegi: #9bc24e; --moegi-soft: #26301a; --moegi-deep: #b9de77;
      --warn: #dba15a; --warn-soft: #2c2416; --danger: #d97f63;
      --status-approved: #7fc17a; --status-approved-bg: #1c2a1a;
      --status-rejected: #9aa08d; --status-rejected-bg: #24261e;
      --status-edited: #7fabde; --status-edited-bg: #182535;
    }
  }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, "Hiragino Sans", "Yu Gothic", sans-serif;
    max-width: 880px; margin: 0 auto; padding: 1.5rem 1.25rem 4rem;
    background: var(--paper); color: var(--ink); line-height: 1.7;
  }
  h1 { font-size: 1.5rem; letter-spacing: 0.01em; margin: 1.75rem 0 0.75rem; text-wrap: balance; }
  h2 { font-size: 1.05rem; color: var(--ink-soft); font-weight: 600; margin: 2rem 0 0.75rem; }
  a { color: var(--moegi-deep); }

  .home-nav { margin: 0.75rem 0 0; font-size: 0.85rem; }
  .home-nav a { text-decoration: none; font-weight: 600; }
  .home-nav a:hover { text-decoration: underline; }

  .run-warning {
    background: var(--warn-soft); border-left: 3px solid var(--warn); border-radius: 0 6px 6px 0;
    padding: 0.6rem 0.9rem; font-size: 0.85rem; color: var(--ink);
  }
  .run-form { display: flex; align-items: center; gap: 0.6rem; margin-top: 0.75rem; flex-wrap: wrap; }
  select { font: inherit; padding: 0.35rem 0.5rem; border: 1px solid var(--line); border-radius: 6px; background: var(--paper-raised); color: var(--ink); }

  .procedure-buttons { display: flex; flex-wrap: wrap; gap: 0.5rem; margin: 0.5rem 0 1.25rem; }
  .procedure-pill {
    display: inline-flex; align-items: center; gap: 0.4rem; padding: 0.55rem 0.9rem;
    border: 1px solid var(--line); border-radius: 99px; background: var(--paper-raised);
    font-size: 0.88rem; cursor: pointer; user-select: none; min-height: 44px;
  }
  .procedure-pill:has(input:checked) { background: var(--moegi-soft); border-color: var(--moegi); color: var(--moegi-deep); font-weight: 600; }
  .procedure-pill input { accent-color: var(--moegi-deep); width: 1.1rem; height: 1.1rem; }
  .record-controls { display: flex; align-items: center; gap: 0.75rem; margin: 0.5rem 0 1.25rem; }
  #record-btn { min-height: 48px; padding: 0.6rem 1.3rem; font-size: 1rem; }
  #nippou-form > label { display: block; font-weight: 600; margin: 1rem 0 0.3rem; }

  button {
    cursor: pointer; font: inherit; font-weight: 600; border: none; border-radius: 6px;
    padding: 0.45rem 1rem; background: var(--moegi); color: #10240a;
  }
  button:hover { background: var(--moegi-deep); color: #fff; }
  button:disabled { opacity: 0.55; cursor: wait; }
  .actions button { background: var(--paper-raised); color: var(--ink); border: 1px solid var(--line); font-weight: 500; }
  .actions button:hover { background: var(--moegi-soft); color: var(--ink); }

  .run-list { list-style: none; padding: 0; display: flex; flex-direction: column; gap: 0.3rem; }
  .run-list a { font-variant-numeric: tabular-nums; font-size: 0.9rem; }

  .proposal {
    border: 1px solid var(--line); border-left: 4px solid var(--line); border-radius: 8px;
    padding: 1rem 1.1rem; margin: 1rem 0; background: var(--paper-raised);
  }
  .proposal[data-status="承認"] { border-left-color: var(--status-approved); }
  .proposal[data-status="却下"] { border-left-color: var(--status-rejected); opacity: 0.65; }
  .proposal[data-status="編集済"] { border-left-color: var(--status-edited); }

  .meta { color: var(--ink-soft); font-size: 0.82rem; margin: 0 0 0.4rem; }
  .chips { display: flex; align-items: center; gap: 0.5rem; margin: 0 0 0.5rem; flex-wrap: wrap; }
  .chip { display: inline-flex; align-items: center; padding: 0.15rem 0.55rem; border-radius: 99px; font-size: 0.78rem; font-weight: 600; background: var(--moegi-soft); color: var(--moegi-deep); }
  .status-chip { background: var(--status-rejected-bg); color: var(--status-rejected); }
  .proposal[data-status="承認"] .status-chip { background: var(--status-approved-bg); color: var(--status-approved); }
  .proposal[data-status="編集済"] .status-chip { background: var(--status-edited-bg); color: var(--status-edited); }
  .confidence { color: var(--ink-soft); font-size: 0.82rem; font-variant-numeric: tabular-nums; }

  .rationale { font-size: 0.92rem; color: var(--ink-soft); margin: 0.5rem 0; }
  .diff { display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; margin: 0.6rem 0; }
  .diff > div { background: var(--paper); border: 1px solid var(--line); padding: 0.6rem 0.7rem; border-radius: 6px; font-size: 0.88rem; }
  .diff .field-label { display: block; font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.04em; color: var(--ink-soft); margin-bottom: 0.25rem; }
  .diff .before { color: var(--ink-soft); }
  .diff .after { font-weight: 600; }
  mark { background: #f4e08a; color: #1c1f1a; border-radius: 2px; padding: 0 0.1em; }

  details { margin: 0.6rem 0; }
  summary { cursor: pointer; font-size: 0.85rem; color: var(--moegi-deep); font-weight: 600; }
  .trace-text { white-space: pre-wrap; background: var(--paper); border: 1px solid var(--line); padding: 0.6rem 0.7rem; border-radius: 6px; font-size: 0.85rem; margin-top: 0.3rem; }

  .actions { display: flex; gap: 0.5rem; margin-top: 0.7rem; }
  .edit-form { display: flex; flex-direction: column; gap: 0.3rem; margin-top: 0.9rem; }
  .edit-form .field-label { margin: 0; }
  .edit-form button { align-self: flex-start; }
  textarea {
    width: 100%; font: inherit; font-size: 0.9rem; line-height: 1.5; padding: 0.6rem 0.7rem;
    border: 1px solid var(--line); border-radius: 6px; background: var(--paper); color: var(--ink); resize: vertical;
  }

  @media (max-width: 600px) {
    .diff { grid-template-columns: 1fr; }
  }
</style>
"""


def _render_page(body: str) -> HTMLResponse:
    nav = "<nav class='home-nav'><a href='/'>← MOEGI ホーム</a></nav>"
    body = _pending_delivery_badge() + body
    return HTMLResponse(
        f"<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>MOEGI 承認UI</title>{_PAGE_STYLE}</head>"
        f"<body>{nav}{body}</body></html>"
    )


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    conn = _db_conn()
    try:
        past_runs = conn.execute(
            """
            SELECT proposals.run_id, MIN(changes.label), MIN(changes.kind)
            FROM proposals
            JOIN impacts ON impacts.row_id = proposals.impact_row_id
            JOIN changes ON changes.row_id = impacts.change_row_id
            GROUP BY proposals.run_id
            ORDER BY MAX(proposals.row_id) DESC
            LIMIT 20
            """
        ).fetchall()
    finally:
        conn.close()

    targets = ingest.load_allowlist(str(pipeline._REPO_ROOT / "config" / "allowlist.toml"))
    label_options = "".join(
        f"<option value='{html.escape(t['label'])}'>{html.escape(t['label'])} — {html.escape(t['title'])}</option>"
        for t in targets
    )

    run_list = (
        "<ul class='run-list'>"
        + "".join(
            f"<li><a href='/runs/{html.escape(run_id)}'>対象条文 {html.escape(label)}（{html.escape(kind)}）</a>"
            f" <span class='meta'>{html.escape(run_id[:8])}</span></li>"
            for run_id, label, kind in past_runs
        )
        + "</ul>"
        if past_runs
        else "<p>まだ分析結果がありません。下のフォームから実行してください。</p>"
    )

    # 実機で確認済み: 単発モードのローカルLLM実行は項目により1〜4分かかる（design.md §6.2）。
    # 進行表示が無いと固まって見え「落ちた」と誤認されるため、送信後にボタンを無効化して
    # 実行中である旨を明示する（素のJS・CDN依存なし＝design.md §14）。
    body = f"""
    <h1>MOEGI 承認UI</h1>
    <h2>新しい分析を実行</h2>
    <p class="run-warning">⚠️ クラウドLLM（ORCA）を使うため<strong>1分弱</strong>かかります（実測47秒・N-11以降）。
    クラウド呼び出しが失敗した場合のみローカルへ自動フォールバックし、その場合は数分かかります。
    実行中はタブを閉じずにお待ちください。</p>
    <form class="run-form" method="post" action="/runs" onsubmit="
      this.querySelector('button').disabled = true;
      this.querySelector('button').textContent = '実行中…';
    ">
      <label for="label-select">対象条文</label>
      <select id="label-select" name="label">{label_options}</select>
      <button type="submit">パイプラインを実行</button>
    </form>
    <h2>過去の実行</h2>
    {run_list}
    <h2>日報エージェント</h2>
    <p><a href="/nippou">日報・SOAPの文字起こし→仮名化→構造化を試す</a>（design.md §6.5・要配慮個人情報のためローカル固定）</p>
    <p><a href="/monthly">月報（1ヶ月分の日報を集計）</a>（ledger R-84・LLM不使用の決定論的な集計）</p>
    <p><a href="/privacy/audits">仮名化処理の監査記録</a>（design/moegi/privacy.md §10）</p>
    <p><a href="/deliveries">配信キュー</a>（design.md G8・キュー型チャネル・回線復帰で自動送信）</p>
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
        old_block = f"<div><span class='field-label'>旧版（{html.escape(row['old_path'] or '')}）</span><div class='trace-text'>{html.escape(row['old_text'])}</div></div>"
    else:
        old_block = "<div><span class='field-label'>旧版</span><div class='trace-text'>（完全新設。旧版に対応条文なし）</div></div>"

    return f"""
    <details>
      <summary>原文トレース（条文: {html.escape(row['new_path'])}）</summary>
      <div class="diff">
        {old_block}
        <div><span class="field-label">新版（{html.escape(row['new_path'])}）</span><div class="trace-text">{new_text}</div></div>
      </div>
    </details>
    """


def _proposal_html(row: dict) -> str:
    item_label = row["sitedoc_item_id"] or "（新規項目の提案）"
    before = html.escape(row["before"]) if row["before"] else "（現在この項目はチェックリストに存在しない）"
    after = html.escape(row["after"])
    row_id = row["row_id"]
    status = row["status"]

    return f"""
    <div class="proposal" data-status="{html.escape(status)}">
      <div class="meta">変更 {html.escape(row['change_label'])}（{html.escape(row['change_kind'])}） ・ 対象項目 {html.escape(item_label)}</div>
      <div class="chips">
        <span class="chip">{html.escape(row['verdict'])}</span>
        <span class="chip status-chip">{html.escape(status)}</span>
        <span class="confidence">信頼度 {row['confidence']:.2f}</span>
      </div>
      <p class="rationale">{html.escape(row['rationale'])}</p>
      <div class="diff">
        <div class="before"><span class="field-label">Before</span>{before}</div>
        <div class="after"><span class="field-label">After</span>{after}</div>
      </div>
      {_trace_html(row)}
      <div class="actions">
        <form method="post" action="/proposals/{row_id}/approve"><button type="submit">承認</button></form>
        <form method="post" action="/proposals/{row_id}/reject"><button type="submit">却下</button></form>
      </div>
      <form class="edit-form" method="post" action="/proposals/{row_id}/edit">
        <label class="field-label" for="after-{row_id}">文言を編集</label>
        <textarea id="after-{row_id}" name="after" rows="4">{after}</textarea>
        <button type="submit">編集して保存</button>
      </form>
    </div>
    """


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def review_run(run_id: str) -> HTMLResponse:
    conn = _db_conn()
    try:
        rows = store.list_proposals_for_review(conn, run_id)
        deliveries = store.list_fax_deliveries(conn, run_id)
    finally:
        conn.close()

    if not rows:
        return _render_page(f"<h1>run_id={html.escape(run_id)}</h1><p>更新案がありません。</p>")

    approved_count = sum(1 for r in rows if r["status"] == "承認")
    history_html = (
        "<p class='meta'>配信履歴はまだありません。</p>"
        if not deliveries
        else "<ul class='run-list'>"
        + "".join(
            f"<li>{html.escape(d['generated_at'])} — 更新案 {len(d['proposal_row_ids'])} 件</li>"
            for d in deliveries
        )
        + "</ul>"
    )
    fax_section = f"""
    <h2>配布（即時チャネル）</h2>
    <p class="meta">承認済み {approved_count} 件を反映したFAX様式PDFのプレビューを生成します（design.md §5.3）。</p>
    <form method="get" action="/runs/{html.escape(run_id)}/fax-preview">
      <button type="submit">FAXプレビューPDFを生成</button>
    </form>
    <h2>配信履歴</h2>
    {history_html}
    """

    body = f"<h1>更新案の承認（run_id={html.escape(run_id)}）</h1>"
    body += "".join(_proposal_html(row) for row in rows)
    body += fax_section
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
    rows = "".join(
        f"<tr><th>{html.escape(item.checklist_item_id)}</th>"
        f"<td><span class='chip {'status-chip' if not item.filled else ''}'>"
        f"{'記入済み' if item.filled else '未記入'}</span> {html.escape(item.value)}</td></tr>"
        for item in record.items
    )
    evaluation_html = ""
    if record.evaluation:
        ev = record.evaluation
        evaluation_html = f"""
        <h3>評価（design.md §6.5・評価の記録）</h3>
        <table>
          <tr><th>看護目標（goal_ref）</th><td>{html.escape(ev.goal_ref or '（参照なし）')}</td></tr>
          <tr><th>達成の程度（achievement）</th><td>{html.escape(ev.achievement)}</td></tr>
          <tr><th>効果（effect）</th><td>{html.escape(ev.effect or '（記載なし）')}</td></tr>
          <tr><th>ステータス</th><td><span class="chip status-chip">{'記入済み' if ev.filled else '未記入'}</span></td></tr>
        </table>
        """
    procedures_html = (
        "<p class='meta'>処置内容の選択はありません。</p>"
        if not record.procedures
        else "<p>" + "".join(f"<span class='chip'>{html.escape(p)}</span> " for p in record.procedures) + "</p>"
    )
    return f"""
    <h2>構造化結果（utterance_id={html.escape(record.utterance_id)}）</h2>
    <h3>処置内容（ボタン選択・ledger R-77）</h3>
    {procedures_html}
    <table style="width:100%; border-collapse: collapse;">{rows}</table>
    {evaluation_html}
    """


@app.get("/nippou", response_class=HTMLResponse)
def nippou_form() -> HTMLResponse:
    procedure_checkboxes = "".join(
        f"<label class='procedure-pill'><input type='checkbox' name='procedures' value='{html.escape(p)}'>"
        f"{html.escape(p)}</label>"
        for p in _PROCEDURE_OPTIONS
    )
    body = f"""
    <h1>日報エージェント（design.md §6.5）</h1>
    <p class="run-warning">⚠️ 要配慮個人情報を扱うため常にローカル（gemma4:26b）で処理します（ledger N-11）。
    訪問時の移動中、または現場での録音許可が取れている場合はマイクボタンで録音してください
    （ledger R-80）。電波が無い場所でも録音自体はでき、あとで構造化できます。</p>
    <p class="run-warning">🔒 文字起こしのあと、日報・SOAPを作る前に<strong>仮名化・安全検査（Privacy Gate）</strong>を
    通します（現在のモード: <strong>{html.escape(PRIVACY_CONFIG.mode)}</strong>）。
    氏名・電話番号・住所・生年月日などの直接識別子を仮名へ置換し、検査に通らなかった場合は
    構造化へ進まず確認画面を出します。この処理では外部LLMへの送信は行われません。
    <a href="/privacy/audits">監査記録を見る</a></p>

    <form method="post" action="/nippou/structure" enctype="multipart/form-data" id="nippou-form">
      <label for="subject-code">利用者コード（ledger R-84: 月報の集計単位。LLMに同定させず選択式で確定する）</label>
      <div class="run-form">
        <select id="subject-code" name="subject_code">{_patient_code_options()}</select>
        <label for="visited-on">訪問日</label>
        <input type="date" id="visited-on" name="visited_on" value="{date.today().isoformat()}">
      </div>

      <label>処置内容（複数選択可・ledger R-77: LLMではなくボタン選択で確定）</label>
      <div class="procedure-buttons">{procedure_checkboxes}</div>

      <label>出力形式</label>
      <div class="procedure-buttons">
        <span class="procedure-pill">日報（チェックリスト照合）— 常に作成</span>
        <label class="procedure-pill"><input type="checkbox" name="soap" checked>SOAP形式でも記録する</label>
      </div>

      <label>録音</label>
      <div class="record-controls">
        <button type="button" id="record-btn">🎙 録音開始</button>
        <span id="record-status" class="meta"></span>
      </div>
      <input type="file" name="audio" id="audio-input" accept="audio/*" style="display:none">

      <label for="utterance_text">またはテキストで直接入力</label>
      <textarea id="utterance_text" name="utterance_text" rows="4" style="width:100%"
        placeholder="訪問終了しました。血圧128の78、体温36.5度、脈拍70、呼吸数16でした。特記事項はありません。"></textarea>

      <button type="submit">構造化する</button>
    </form>
    <script>
    (function() {{
      var recordBtn = document.getElementById('record-btn');
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
            recordBtn.textContent = '🎙 録音開始';
            statusEl.textContent = '録音完了。内容を確認したら「構造化する」を押してください。';
          }};
          mediaRecorder.start();
          recordBtn.textContent = '⏹ 録音停止';
          statusEl.textContent = '録音中…';
        }}).catch(function(err) {{
          statusEl.textContent = 'マイクを使用できませんでした: ' + err.message;
        }});
      }});

      document.getElementById('nippou-form').addEventListener('submit', function() {{
        this.querySelector('button[type=submit]').disabled = true;
      }});
    }})();
    </script>
    """
    return _render_page(body)


def _soap_record_html(record: nippou.SoapRecord) -> str:
    if record.undecidable:
        return (
            "<h2>SOAP形式</h2>"
            "<p class='run-warning'>ローカルLLMがSOAP構造化を完了できませんでした（判定不能）。"
            "内容を確認して、もう一度お試しください。</p>"
        )
    rows = "".join(
        f"<tr><th style='text-align:left; vertical-align:top; white-space:nowrap; padding:0.3rem 0.8rem 0.3rem 0;'>{label}</th>"
        f"<td style='padding:0.3rem 0;'>{html.escape(value) if value else '<span class=\"meta\">（該当する記載なし）</span>'}</td></tr>"
        for label, value in (
            ("S（主観的情報）", record.subjective),
            ("O（客観的情報）", record.objective),
            ("A（アセスメント）", record.assessment),
            ("P（計画）", record.plan),
        )
    )
    return f"""
    <h2>SOAP形式（utterance_id={html.escape(record.utterance_id)}）</h2>
    <table style="width:100%; border-collapse: collapse;">{rows}</table>
    """


#: Privacy Gate の状態 → 画面表示（design/moegi/privacy.md §12）。
#: 「匿名化済み」「完全に安全」とは書かない——仮名化後も要配慮個人情報は残る。
_GATE_MESSAGES = {
    privacy.PrivacyGateStatus.PASSED: "直接識別子を仮名化しました。仮名化後も病状・服薬などの要配慮個人情報は残ります。",
    privacy.PrivacyGateStatus.REVIEW_REQUIRED: "個人を特定し得る情報が残っている可能性があります。候補を確認してから処理を続けてください。",
    privacy.PrivacyGateStatus.BLOCKED: "セキュリティ検査で停止しました。記録に残してはいけない情報（保険証番号・認証情報など）が含まれています。",
    privacy.PrivacyGateStatus.FAILED: "セキュリティ検査を完了できませんでした。処理を中止しました。",
    privacy.PrivacyGateStatus.PENDING: "セキュリティ検査中です。",
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
    "residual_registered_name": "仮名化後にも登録済みの氏名が残っています",
}


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
    """検出サマリ・Privacy Gate状態・外部送信の有無を表示する（§12）。

    検出した「値」は出さず、種別と件数だけを出す。権限のない利用者に元の値を
    見せないための最低条件で、画面に出さないものは画面から漏れない。
    """
    counts = (
        "".join(
            f"<span class='chip'>{html.escape(t)} {n}件</span> "
            for t, n in sorted(result.detected_counts.items())
        )
        or "<span class='meta'>検出はありませんでした。</span>"
    )
    reasons = (
        "<ul>" + "".join(f"<li>{html.escape(_reason_text(c))}</li>" for c in result.reason_codes) + "</ul>"
        if result.reason_codes
        else ""
    )
    shadow_note = ""
    if result.mode != "enforce" and result.would_block:
        shadow_note = (
            "<p class='run-warning'>⚠️ 段階導入モード"
            f"（{html.escape(result.mode)}）のため処理を継続しました。"
            "本番（enforce）ではここで停止します。</p>"
        )
    return f"""
    <div class="proposal">
      <div class="meta">Privacy Gate ・ request_id {html.escape(result.request_id[:8])} ・
        ポリシー {html.escape(result.policy_version)} ・ モード {html.escape(result.mode)}</div>
      <p><strong>{html.escape(_GATE_MESSAGES[result.status])}</strong></p>
      <div class="chips">{counts}</div>
      {reasons}
      {shadow_note}
      <p class="meta">この処理では外部LLMへの送信は行われていません（仮名化・構造化ともローカル実行）。</p>
    </div>
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
    <h2>修正して再検査</h2>
    <p class="meta">残っている個人情報を手で消してから再検査してください。ここに表示されているのは
      仮名化済みのテキストです（元の値は表示されません）。</p>
    <form class="edit-form" method="post" action="/nippou/recheck">
      {hidden}
      <textarea name="sanitized_text" rows="6">{html.escape(sanitized_text)}</textarea>
      <button type="submit">修正して再検査する</button>
    </form>
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
    header = "<h1>日報エージェント（design.md §6.5）</h1><p><a href='/nippou'>← もう一度試す</a></p>"

    if not privacy.cleared_for_downstream(result):
        _save_privacy_audit(result, proceeded=False)
        body = header + _gate_summary_html(result) + _recheck_form_html(result.sanitized_text, context)
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
        saved_html = (
            f"<p class='meta'>この記録を利用者 {html.escape(context.subject_code)} の"
            f" {html.escape(context.visited_on)} の日報として保存しました。"
            f"<a href='/monthly'>月報を見る</a></p>"
        )
    else:
        saved_html = (
            "<p class='run-warning'>⚠️ 利用者コードが選ばれていないため、この記録は保存していません"
            "（月報の集計に入りません）。</p>"
        )

    body = (
        header
        + _gate_summary_html(result)
        + saved_html
        + "<h2>発話内容（仮名化済み）</h2>"
        + f"<p class='trace-text'>{html.escape(result.sanitized_text)}</p>"
        + _nippou_record_html(record)
        + (_soap_record_html(soap) if soap is not None else "")
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


def _checklist_items_html(items: list[pipeline.ChecklistItem], highlight: set[str]) -> str:
    return "<ol>" + "".join(
        (
            f"<li><mark>{html.escape(i.label)}</mark>"
            if i.label in highlight
            else f"<li>{html.escape(i.label)}"
        )
        + (f" <span class='meta'>（看護目標: {html.escape(i.goal_ref)}）</span>" if i.goal_ref else "")
        + "</li>"
        for i in items
    ) + "</ol>"


def _before_after_form_html(run_id: str, utterance: str = "", goal_ref: str = "") -> str:
    return f"""
    <h2>同じ発話を、変更前／変更後のチェックリストで処理する</h2>
    <p class="meta">同一の発話データを2回構造化し、チェックリストの変更が記録の中身に
      どう現れるかを並べます（design.md G6）。発話は Privacy Gate を通してから構造化します。</p>
    <form class="edit-form" method="post" action="/runs/{html.escape(run_id)}/before-after"
          onsubmit="this.querySelector('button').disabled = true;
                    this.querySelector('button').textContent = '処理中…';">
      <label class="field-label" for="ba-utterance">発話内容</label>
      <textarea id="ba-utterance" name="utterance_text" rows="4">{html.escape(utterance or _SAMPLE_UTTERANCE)}</textarea>
      <label class="field-label" for="ba-goal">看護目標（新規の「評価」項目に紐づけます）</label>
      <textarea id="ba-goal" name="goal_ref" rows="2">{html.escape(goal_ref or "在宅療養の継続")}</textarea>
      <button type="submit">Before / After を比較する</button>
    </form>
    """


def _load_checklist_diff(run_id: str, goal_ref: str) -> checklist.ChecklistDiff:
    conn = _db_conn()
    try:
        rows = store.list_proposals_for_review(conn, run_id)
    finally:
        conn.close()
    return checklist.apply_approved(pipeline.load_sitedoc(), rows, goal_ref=goal_ref)


def _checklist_diff_html(diff: checklist.ChecklistDiff) -> str:
    added = set(diff.added)
    changed_after = {after for _, after in diff.changed}
    notes = []
    if not diff.has_changes:
        notes.append(
            "このrunにはまだ承認済みの更新案がありません。先に更新案を承認してください。"
        )
    if diff.unmatched:
        notes.append(
            f"承認済みだが現在のチェックリストに該当項目が見つからなかった更新案が "
            f"{len(diff.unmatched)} 件あります（項目ラベルが既に書き換わっている可能性。"
            "項目の不変ID設計が未着手のため、ここは取りこぼしとして表示しています）。"
        )
    notes_html = (
        "<div class='run-warning'><ul>" + "".join(f"<li>{html.escape(n)}</li>" for n in notes) + "</ul></div>"
        if notes
        else ""
    )
    return f"""
    {notes_html}
    <div class="diff">
      <div><span class="field-label">変更前のチェックリスト</span>
        {_checklist_items_html(diff.before.items, set())}</div>
      <div><span class="field-label">変更後のチェックリスト</span>
        {_checklist_items_html(diff.after.items, added | changed_after)}</div>
    </div>
    """


@app.get("/runs/{run_id}/before-after", response_class=HTMLResponse)
def before_after_form(run_id: str) -> HTMLResponse:
    """G6 Before/After 比較画面の入力フォーム。"""
    diff = _load_checklist_diff(run_id, "在宅療養の継続")
    body = (
        f"<h1>Before / After 比較（run_id={html.escape(run_id[:8])}）</h1>"
        f"<p class='home-nav'><a href='/runs/{html.escape(run_id)}'>← このrunの承認画面へ戻る</a></p>"
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

    header = (
        f"<h1>Before / After 比較（run_id={html.escape(run_id[:8])}）</h1>"
        f"<p class='home-nav'><a href='/runs/{html.escape(run_id)}'>← このrunの承認画面へ戻る</a></p>"
    )

    if not privacy.cleared_for_downstream(gate_result):
        body = (
            header
            + _gate_summary_html(gate_result)
            + "<p class='run-warning'>セキュリティ検査に通らなかったため、構造化は行っていません。</p>"
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

    body = (
        header
        + _gate_summary_html(gate_result)
        + "<h2>発話内容（仮名化済み・両方に同じものを入力）</h2>"
        + f"<p class='trace-text'>{html.escape(gate_result.sanitized_text)}</p>"
        + _checklist_diff_html(diff)
        + "<h2>構造化結果の比較</h2>"
        + "<div class='diff'>"
        + f"<div><span class='field-label'>変更前</span>{_nippou_record_html(before_record)}</div>"
        + f"<div><span class='field-label'>変更後</span>{_nippou_record_html(after_record)}</div>"
        + "</div>"
        + _before_after_form_html(run_id, utterance_text, goal_ref)
    )
    return _render_page(body)


def _monthly_form_html(subject_code: str = "", month: str = "") -> str:
    """月報の条件入力。**POSTで送る**——利用者コードをURLのクエリに載せないため。"""
    return f"""
    <h2>月報を作成</h2>
    <form class="run-form" method="post" action="/monthly">
      <label for="monthly-subject">利用者コード</label>
      <select id="monthly-subject" name="subject_code">{_patient_code_options(subject_code)}</select>
      <label for="monthly-month">対象月</label>
      <input type="month" id="monthly-month" name="month"
             value="{html.escape(month or date.today().strftime('%Y-%m'))}">
      <button type="submit">月報を作成</button>
    </form>
    """


def _monthly_report_html(report: monthly.MonthlyReport, subject_code: str) -> str:
    if report.is_empty:
        return (
            f"<p class='run-warning'>利用者 {html.escape(subject_code)} の "
            f"{report.year}年{report.month}月の日報がありません。"
            "先に <a href='/nippou'>日報エージェント</a> で記録を作成してください。</p>"
        )

    procedures = (
        "".join(
            f"<tr><th style='text-align:left; padding:0.25rem 1rem 0.25rem 0;'>{html.escape(name)}</th>"
            f"<td style='font-variant-numeric: tabular-nums;'>{count} 回</td></tr>"
            for name, count in sorted(report.procedure_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        or "<tr><td class='meta'>処置の選択がありませんでした。</td></tr>"
    )

    fill_rates = "".join(
        f"<tr><th style='text-align:left; padding:0.25rem 1rem 0.25rem 0; font-weight:500;'>"
        f"{html.escape(r.label)}</th>"
        f"<td style='font-variant-numeric: tabular-nums;'>{r.filled} / {r.total}"
        f"（{r.rate * 100:.0f}%）</td></tr>"
        for r in report.item_fill_rates
    )

    def _cell(value) -> str:
        return "—" if value is None else html.escape(str(value))

    vitals = (
        "".join(
            f"<tr><td>{html.escape(v.visited_on)}</td>"
            f"<td>{_cell(v.systolic)}{'/' if v.systolic else ''}{_cell(v.diastolic) if v.systolic else ''}</td>"
            f"<td>{_cell(v.temperature)}</td><td>{_cell(v.pulse)}</td><td>{_cell(v.spo2)}</td></tr>"
            for v in report.vitals
        )
        or "<tr><td colspan='5' class='meta'>数値として読み取れたバイタルがありませんでした。</td></tr>"
    )

    notes = []
    if report.unparsed_vital_visits:
        notes.append(
            f"バイタルを数値として読み取れなかった訪問が {report.unparsed_vital_visits} 件あります"
            "（0件として集計せず、この件数として残しています）"
        )
    if report.non_passed_records:
        notes.append(
            f"Privacy Gate が passed 以外だった記録が {report.non_passed_records} 件含まれています"
            "（段階導入モードで保存されたもの）"
        )
    if len(report.policy_versions) > 1:
        notes.append(
            "複数の仮名化ポリシーバージョンの記録が混在しています（"
            + "、".join(html.escape(v) for v in report.policy_versions)
            + "）"
        )
    notes_html = (
        "<div class='run-warning'><ul>" + "".join(f"<li>{n}</li>" for n in notes) + "</ul></div>"
        if notes
        else ""
    )

    return f"""
    <div class="proposal">
      <div class="meta">利用者 {html.escape(subject_code)} ・ 安定仮名
        {html.escape(report.subject_pseudonym)} ・ 集計はすべて決定論的（LLM不使用）</div>
      <h2 style="margin-top:0.5rem;">{report.year}年{report.month}月の月報</h2>
      <div class="chips">
        <span class="chip">訪問 {report.visit_count} 回</span>
        <span class="chip">訪問日数 {report.visit_days} 日</span>
        <span class="chip">評価の記入 {report.evaluation_filled} / {report.evaluation_total}</span>
      </div>
      {notes_html}
      <h3>処置別の実施回数</h3>
      <table style="width:100%; border-collapse: collapse;">{procedures}</table>
      <h3>チェックリスト項目の記入率</h3>
      <table style="width:100%; border-collapse: collapse;">{fill_rates}</table>
      <h3>バイタルの推移</h3>
      <table style="width:100%; border-collapse: collapse; font-variant-numeric: tabular-nums;">
        <tr><th style="text-align:left;">訪問日</th><th style="text-align:left;">血圧</th>
            <th style="text-align:left;">体温</th><th style="text-align:left;">脈拍</th>
            <th style="text-align:left;">SpO2</th></tr>
        {vitals}
      </table>
    </div>
    """


@app.get("/monthly", response_class=HTMLResponse)
def monthly_form() -> HTMLResponse:
    """月報の作成フォーム（ledger R-84）。"""
    body = f"""
    <h1>月報</h1>
    <p class="meta">保存済みの日報を1ヶ月分まとめます。集計はすべて決定論的で、
      LLMは使いません——同じ日報からは常に同じ月報が出ます。
      実名は保持しておらず、集計単位は利用者コードから導出した安定仮名です。</p>
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
        + _monthly_report_html(report, subject_code)
        + _monthly_form_html(subject_code, f"{year:04d}-{month_number:02d}")
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

    if not rows:
        listing = "<p class='meta'>配信はまだありません。更新案を承認すると、ここに積まれます。</p>"
    else:
        listing = "".join(
            f"<div class='proposal' data-status='{'承認' if r['status'] == 'sent' else '未承認'}'>"
            f"<div class='meta'>{html.escape(r['created_at'])} ・ 宛先 {html.escape(r['recipient'])}"
            f" ・ チャネル {html.escape(r['channel'])}</div>"
            f"<div class='chips'><span class='chip'>"
            f"{'送信済み' if r['status'] == 'sent' else '送信待ち'}</span>"
            + (f"<span class='meta'>試行 {r['attempts']} 回</span>" if r["attempts"] else "")
            + (
                f"<span class='meta'>{html.escape(r['sent_at'])} に送信</span>"
                if r["sent_at"]
                else ""
            )
            + "</div>"
            f"<p class='rationale'>{html.escape(r['subject'])}</p>"
            "</div>"
            for r in rows
        )

    channel_note = (
        f"送信先は<strong>メール</strong>（{html.escape(DELIVERY_CONFIG.recipient)}）です。"
        if DELIVERY_CONFIG.channel == delivery.CHANNEL_EMAIL
        else "SMTPが未設定のため、送信は <code>data/outbox/</code> への書き出しになります"
        "（環境変数 MOEGI_SMTP_HOST を設定すると実際にメールを送ります）。"
    )

    body = f"""
    <h1>配信キュー</h1>
    <p class="meta">承認された更新案は、まずここに積まれます（store-and-forward）。
      オフライン中は「送信待ち」のまま溜まり、回線が復帰すると
      {DELIVERY_FLUSH_INTERVAL_SECONDS:.0f}秒以内に<strong>自動で</strong>送信されます。
      手動の再送ボタンは意図的に置いていません。{channel_note}</p>
    <p class="meta">積んでよいのは承認済み更新案（チェックリスト項目のラベルと更新後の文言）だけです。
      日報・SOAP・文字起こし原文・仮名対応表は積みません——本文に識別子が混じっていた場合、
      キューに入れること自体を拒否します。</p>
    <h2>送信待ち {pending} 件</h2>
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
        rows = "<p class='meta'>まだ監査記録がありません。</p>"
    else:
        rows = "".join(
            f"<div class='proposal'>"
            f"<div class='meta'>{html.escape(a['occurred_at'])} ・ request_id {html.escape(a['request_id'][:8])}"
            f" ・ ポリシー {html.escape(a['policy_version'])} ・ モード {html.escape(a['mode'])}</div>"
            f"<div class='chips'><span class='chip'>{html.escape(a['gate_status'])}</span>"
            + "".join(
                f"<span class='chip'>{html.escape(t)} {n}件</span>"
                for t, n in sorted(a["detected_counts"].items())
            )
            + f"<span class='confidence'>{a['duration_ms']:.0f}ms</span></div>"
            f"<p class='meta'>構造化へ進んだ: {'はい' if a['proceeded_to_structuring'] else 'いいえ'}"
            f" ／ 外部送信: {'あり' if a['external_transmission'] else 'なし'}"
            f" ／ 人の確認が必要: {'はい' if a['needs_human_review'] else 'いいえ'}</p>"
            + (
                "<p class='meta'>"
                + "、".join(html.escape(_reason_text(c)) for c in a["reason_codes"])
                + "</p>"
                if a["reason_codes"]
                else ""
            )
            + "</div>"
            for a in audits
        )

    body = f"""
    <h1>仮名化処理の監査記録</h1>
    <p class="meta">記録しているのは種別と件数だけです。文字起こし原文・仮名化済み全文・
      日報／SOAP全文・仮名対応表はこのテーブルに保存していません
      （design/moegi/privacy.md §10）。</p>
    {rows}
    """
    return _render_page(body)
