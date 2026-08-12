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

import html
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from moegi import fax, ingest, llm, nippou, pipeline, store

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FAX_OUTPUT_DIR = _REPO_ROOT / "data" / "fax_previews"

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

app = FastAPI(title="MOEGI 承認UI")


def _db_conn():
    return store.connect()


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
    <p><a href="/nippou">日報の文字起こし→構造化を試す</a>（design.md §6.5・要配慮個人情報のためローカル固定）</p>
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

    <form method="post" action="/nippou/structure" enctype="multipart/form-data" id="nippou-form">
      <label>処置内容（複数選択可・ledger R-77: LLMではなくボタン選択で確定）</label>
      <div class="procedure-buttons">{procedure_checkboxes}</div>

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


@app.post("/nippou/structure", response_class=HTMLResponse)
def nippou_structure(
    utterance_text: str = Form(""),
    procedures: list[str] = Form([]),
    audio: UploadFile | None = File(None),
) -> HTMLResponse:
    sitedoc = pipeline.load_sitedoc()
    adapter = llm.LLMAdapter()
    tmp_path: Path | None = None
    try:
        agent = nippou.NippouAgent(adapter)
        agent.set_checklist(sitedoc.items)
        if audio is not None and audio.filename:
            suffix = Path(audio.filename).suffix or ".webm"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(audio.file.read())
                tmp_path = Path(tmp.name)
            text = agent.transcribe(tmp_path)
        else:
            text = agent.transcribe(utterance_text)
        record = agent.structure(text, utterance_id=store.new_run_id()[:8], procedures=procedures)
    finally:
        adapter.close()
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    body = f"""
    <h1>日報エージェント（design.md §6.5）</h1>
    <p><a href="/nippou">← もう一度試す</a></p>
    <h2>発話内容</h2>
    <p class="trace-text">{html.escape(text)}</p>
    {_nippou_record_html(record)}
    """
    return _render_page(body)
