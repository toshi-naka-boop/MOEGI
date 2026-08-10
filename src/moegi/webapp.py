"""承認UI（design.md §10 D4）。

    uvicorn moegi.webapp:app --reload

design.md §14 の技術スタック決定どおり FastAPI・サーバーレンダリング＋素のJS
（CDN依存ゼロ）。テンプレートエンジンは使わず、素のPython文字列で組み立てる。

G2「根拠が辿れる」: 各更新案の行に <details> で原文トレース（新旧本文・
根拠の引用箇所）を埋め込み、開閉の1操作で到達できるようにする。
G9-b「再現率が測られ、開示されている」: config/g9_latest.json の実測値を
ページ上部に常時表示する（数値目標は置かず、開示すること自体を合格条件とする）。
"""

from __future__ import annotations

import html
import json
from pathlib import Path

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from moegi import ingest, llm, pipeline, store

_REPO_ROOT = Path(__file__).resolve().parents[2]
_G9_LATEST_PATH = _REPO_ROOT / "config" / "g9_latest.json"

app = FastAPI(title="MOEGI 承認UI")


def _db_conn():
    return store.connect()


def _g9_badge_html() -> str:
    """design.md G9-b: 再現率の実測値を常時表示する（承認UIのどのページにも出す）。

    数値目標を置かず「開示すること」自体が合格条件（design.md §3 G9-b）。
    ただし常時表示するものが長文だとかえって読まれなくなるため、数値2つを
    チップで見せ、注記は補足として1行に圧縮する（ユーザー指摘: くどい・殺風景）。
    """
    try:
        data = json.loads(_G9_LATEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return '<div class="g9-bar g9-bar--missing">AI検出精度: 実測データが見つかりません</div>'
    recall_pct = f"{data['recall'] * 100:.1f}%"
    fpr_pct = f"{data['false_positive_rate'] * 100:.1f}%"
    return f"""
    <div class="g9-bar">
      <span class="g9-label">AI検出精度（実測・{html.escape(data['mode'])}）</span>
      <span class="g9-stat">再現率 <strong>{recall_pct}</strong></span>
      <span class="g9-stat">誤検出率 <strong>{fpr_pct}</strong></span>
      <span class="g9-note">{html.escape(data['measured_at'])}計測・だから承認は必ず人が行います</span>
    </div>
    """


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

  .g9-bar {
    display: flex; flex-wrap: wrap; align-items: baseline; gap: 0.35rem 1rem;
    background: var(--moegi-soft); border-bottom: 3px solid var(--moegi);
    padding: 0.6rem 1rem; font-size: 0.85rem; margin: 0 -1.25rem 1rem;
  }
  .g9-bar--missing { background: var(--warn-soft); border-color: var(--warn); color: var(--warn); }
  .g9-label { font-weight: 600; color: var(--moegi-deep); }
  .g9-stat strong { font-variant-numeric: tabular-nums; font-size: 1rem; }
  .g9-note { color: var(--ink-soft); margin-left: auto; }

  .run-warning {
    background: var(--warn-soft); border-left: 3px solid var(--warn); border-radius: 0 6px 6px 0;
    padding: 0.6rem 0.9rem; font-size: 0.85rem; color: var(--ink);
  }
  .run-form { display: flex; align-items: center; gap: 0.6rem; margin-top: 0.75rem; flex-wrap: wrap; }
  select { font: inherit; padding: 0.35rem 0.5rem; border: 1px solid var(--line); border-radius: 6px; background: var(--paper-raised); color: var(--ink); }

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

  .actions { display: flex; gap: 0.5rem; margin-top: 0.7rem; flex-wrap: wrap; align-items: flex-start; }
  .actions form { display: flex; gap: 0.4rem; align-items: flex-start; }
  textarea { font: inherit; font-size: 0.85rem; padding: 0.4rem; border: 1px solid var(--line); border-radius: 6px; background: var(--paper); color: var(--ink); resize: vertical; }

  @media (max-width: 600px) {
    .diff { grid-template-columns: 1fr; }
    .g9-bar { margin: 0 -1.25rem 1rem; }
  }
</style>
"""


def _render_page(body: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>MOEGI 承認UI</title>{_PAGE_STYLE}</head>"
        f"<body>{_g9_badge_html()}{body}</body></html>"
    )


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    conn = _db_conn()
    try:
        run_ids = [
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT run_id FROM proposals ORDER BY row_id DESC LIMIT 20"
            ).fetchall()
        ]
    finally:
        conn.close()

    targets = ingest.load_allowlist(str(pipeline._REPO_ROOT / "config" / "allowlist.toml"))
    label_options = "".join(
        f"<option value='{html.escape(t['label'])}'>{html.escape(t['label'])} — {html.escape(t['title'])}</option>"
        for t in targets
    )

    run_list = (
        "<ul class='run-list'>"
        + "".join(f"<li><a href='/runs/{html.escape(rid)}'>{html.escape(rid)}</a></li>" for rid in run_ids)
        + "</ul>"
        if run_ids
        else "<p>まだ分析結果がありません。下のフォームから実行してください。</p>"
    )

    # 実機で確認済み: 単発モードのローカルLLM実行は項目により1〜4分かかる（design.md §6.2）。
    # 進行表示が無いと固まって見え「落ちた」と誤認されるため、送信後にボタンを無効化して
    # 実行中である旨を明示する（素のJS・CDN依存なし＝design.md §14）。
    body = f"""
    <h1>MOEGI 承認UI</h1>
    <h2>新しい分析を実行</h2>
    <p class="run-warning">⚠️ ローカルLLMで実行するため<strong>1〜4分程度</strong>かかります。実行中はタブを閉じずにお待ちください。</p>
    <form class="run-form" method="post" action="/runs" onsubmit="
      this.querySelector('button').disabled = true;
      this.querySelector('button').textContent = '実行中…（1〜4分）';
    ">
      <label for="label-select">対象条文</label>
      <select id="label-select" name="label">{label_options}</select>
      <button type="submit">パイプラインを実行</button>
    </form>
    <h2>過去の実行</h2>
    {run_list}
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
        <form method="post" action="/proposals/{row_id}/edit">
          <textarea name="after" rows="2" cols="40">{after}</textarea>
          <button type="submit">編集して保存</button>
        </form>
      </div>
    </div>
    """


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def review_run(run_id: str) -> HTMLResponse:
    conn = _db_conn()
    try:
        rows = store.list_proposals_for_review(conn, run_id)
    finally:
        conn.close()

    if not rows:
        return _render_page(f"<h1>run_id={html.escape(run_id)}</h1><p>更新案がありません。</p>")

    body = f"<h1>更新案の承認（run_id={html.escape(run_id)}）</h1>"
    body += "".join(_proposal_html(row) for row in rows)
    return _render_page(body)


def _redirect_to_run(conn, proposal_row_id: int) -> RedirectResponse:
    run_id = conn.execute(
        "SELECT run_id FROM proposals WHERE row_id = ?", (proposal_row_id,)
    ).fetchone()[0]
    return RedirectResponse(url=f"/runs/{run_id}", status_code=303)


@app.post("/proposals/{proposal_row_id}/approve")
def approve_proposal(proposal_row_id: int) -> RedirectResponse:
    conn = _db_conn()
    try:
        store.update_proposal_status(conn, proposal_row_id, "承認")
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
