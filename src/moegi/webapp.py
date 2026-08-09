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
    """design.md G9-b: 再現率の実測値を常時表示する（承認UIのどのページにも出す）。"""
    try:
        data = json.loads(_G9_LATEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return '<p class="g9-badge g9-badge--missing">AI検出の再現率: 実測データが見つかりません</p>'
    recall_pct = f"{data['recall'] * 100:.1f}%"
    fpr_pct = f"{data['false_positive_rate'] * 100:.1f}%"
    return (
        '<p class="g9-badge">'
        f"AI検出の再現率（実測・{html.escape(data['mode'])}）: <strong>{recall_pct}</strong>"
        f"　／　誤検出率: <strong>{fpr_pct}</strong>"
        f"　（{html.escape(data['measured_at'])}計測・ゴールドセットに対する実測値。"
        "精度は100%ではありません。だから人間の承認を必ず挟みます）"
        "</p>"
    )


_PAGE_STYLE = """
<style>
  body { font-family: -apple-system, "Hiragino Sans", sans-serif; max-width: 960px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }
  h1 { font-size: 1.4rem; }
  .g9-badge { background: #fff3cd; border: 1px solid #d9a441; border-radius: 6px; padding: 0.6rem 1rem; font-size: 0.9rem; }
  .g9-badge--missing { background: #f8d7da; border-color: #c0392b; }
  .proposal { border: 1px solid #ccc; border-radius: 8px; padding: 1rem; margin: 1rem 0; }
  .proposal[data-status="承認"] { border-color: #2e7d32; background: #f1f8f1; }
  .proposal[data-status="却下"] { border-color: #999; background: #f5f5f5; opacity: 0.7; }
  .proposal[data-status="編集済"] { border-color: #1565c0; background: #eef4fc; }
  .meta { color: #555; font-size: 0.85rem; }
  .badge { display: inline-block; padding: 0.1rem 0.5rem; border-radius: 4px; font-size: 0.8rem; background: #eee; }
  .verdict-要更新 { background: #ffe0b2; }
  .diff { display: flex; gap: 1rem; margin: 0.5rem 0; }
  .diff > div { flex: 1; background: #fafafa; padding: 0.5rem; border-radius: 4px; }
  .diff .before { color: #888; }
  .diff .after { font-weight: 600; }
  mark { background: #fff59d; }
  .trace-text { white-space: pre-wrap; background: #fafafa; padding: 0.5rem; border-radius: 4px; font-size: 0.9rem; }
  .actions form { display: inline; margin-right: 0.5rem; }
  button { cursor: pointer; }
  .run-list a { display: block; margin: 0.3rem 0; }
</style>
"""


def _render_page(body: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
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

    body = f"""
    <h1>MOEGI 承認UI</h1>
    <h2>新しい分析を実行</h2>
    <form method="post" action="/runs">
      <label>対象条文（allowlistラベル）:
        <select name="label">{label_options}</select>
      </label>
      <button type="submit">通達Diff→更新案パイプラインを実行</button>
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
        old_block = f"<div><strong>旧版（{html.escape(row['old_path'] or '')}）</strong><div class='trace-text'>{html.escape(row['old_text'])}</div></div>"
    else:
        old_block = "<div><strong>旧版</strong><div class='trace-text'>（完全新設。旧版に対応条文なし）</div></div>"

    return f"""
    <details>
      <summary>原文トレース（条文: {html.escape(row['new_path'])}）</summary>
      <div class="diff">
        {old_block}
        <div><strong>新版（{html.escape(row['new_path'])}）</strong><div class="trace-text">{new_text}</div></div>
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
      <div class="meta">変更: {html.escape(row['change_label'])}（{html.escape(row['change_kind'])}） / 対象項目: {html.escape(item_label)}</div>
      <p><span class="badge verdict-{html.escape(row['verdict'])}">{html.escape(row['verdict'])}</span>
         信頼度 {row['confidence']:.2f} 　ステータス: <strong>{html.escape(status)}</strong></p>
      <p>{html.escape(row['rationale'])}</p>
      <div class="diff">
        <div class="before">Before: {before}</div>
        <div class="after">After: {after}</div>
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
