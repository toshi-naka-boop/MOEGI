"""承認UIの共通スタイル（CSS）。webapp.py から切り出したもの（内容は変更していない）。"""

from __future__ import annotations

PAGE_STYLE = """
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
