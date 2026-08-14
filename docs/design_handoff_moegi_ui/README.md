# Handoff: MOEGI UIリファイン（全9画面）

## Overview
訪問看護事業所向け業務ツール「MOEGI」のUIリファイン一式。
通達Diffの承認フロー（PC・事務向け）と日報の音声入力フロー（スマホ・看護師向け）の全9画面を、
統一デザイントークン（WCAG AA実測済み・ライト/ダーク両対応）でデザインした。

## About the Design Files
このフォルダのHTMLファイルは **HTMLで作成されたデザインリファレンス**（見た目と挙動を示すプロトタイプ）であり、そのまま本番コードとしてコピーするものではない。
実装先は既存の `src/moegi/webapp.py`（Pythonのf-stringでHTMLを組み立て、CSSは `_PAGE_STYLE` 文字列定数）。
**タスクは、このデザインを既存構成の中で再現すること**。具体的には:
1. `moegi-tokens.css` の内容で `_PAGE_STYLE` 冒頭の `:root` / `@media (prefers-color-scheme: dark)` を差し替える
2. 各画面HTMLの `<style>` 内のコンポーネントCSSを `_PAGE_STYLE` に統合する（クラス名は既存実装と揃えてある）
3. HTML構造の差分（下記「Screens」の注記参照）をf-string側に反映する

## 実装制約（ui-brief.md §1 準拠・違反禁止）
- 外部CDN・Webフォント・アイコンフォント禁止（オフライン完走が必須要件）
- 素のHTML+CSS+JSのみ。ビルド工程なし。SPAではない（form POST → 303 → フルリロード）
- 画像アセット不可（絵文字とインラインSVGのみ）
- フォントはOS標準: `-apple-system, "Hiragino Sans", "Yu Gothic", sans-serif`
- 唯一の意図的なJS: `onsubmit="this.classList.add('is-submitting')"`（進行表現の切替。1行のみ。JS不可なら省略可だが進行表現が出なくなる）

## Fidelity
**High-fidelity（hifi）**。色・余白・フォントサイズ・状態表現は最終値。
コントラスト比はすべて実測でWCAG AA（4.5:1）以上を担保しているため、**色の値は変更しないこと**。
変更する場合は同等のコントラスト検証が必要。

## Files
| ファイル | 画面 | 基準デバイス |
|---|---|---|
| `moegi-tokens.css` | 統一デザイントークン最終版 v1.2（全画面の元） | — |
| `01-home.html` | ① ホーム `/` | PC基準・600px以下でモバイル |
| `02-approval.html` | ② 更新案の承認 `/runs/{id}` ★最重要 | PC基準 |
| `03-before-after.html` | ③ Before/After比較 `/runs/{id}/before-after` | PC基準 |
| `04-nippou.html` | ④ 日報の入力 `/nippou` ★スマホ最優先 | **375px基準**・600px以上でPC |
| `05-nippou-result.html` | ⑤ 日報の結果 `/nippou/structure` | 375px基準 |
| `06-blocked.html` | ⑥ 検査で停止した画面（⑤の別状態） | 375px基準 |
| `07-monthly.html` | ⑦ 月報 `/monthly` | PC基準 |
| `08-deliveries.html` | ⑧ 配信キュー `/deliveries` | PC基準 |
| `09-audits.html` | ⑨ 仮名化処理の監査記録 `/privacy/audits` | PC基準 |
| `ui-brief.md` | 元の実装制約・画面インベントリ・文言ルール | — |

各HTMLは単一ファイル完結（`<style>`1つ・外部参照ゼロ）。ブラウザで直接開いてリファレンスにできる。
**各ページ最下部の「⚠ 状態確認用セクション」（破線区切り）はレビュー用。実装しないこと。**

## Design Tokens（moegi-tokens.css が正）
役割→トークン対応（ライト/ダークで同一トークン名を参照する設計。CSSセレクタにモード分岐を書かない）:
- リンク `var(--link)` / hover `var(--link-hover)`
- 主ボタン地 `var(--moegi)` / 文字 `var(--moegi-ink)` / hover `var(--moegi-hover)` / 押下 `var(--moegi-deep)`
- フォーカスリング `outline: 2px solid var(--focus); outline-offset: 2px`（ブラウザ既定の青リング禁止）
- 萌黄 `--moegi-accent` #AACF53 は装飾・タグ地・ハイライト専用。**文字色に使わない**（AA未達）
- ダークモードでは `--moegi` が萌黄系 #A9CE58 に反転し、ボタン文字は濃墨 `--moegi-ink` #1C2114

主要な数値ルール:
- 角丸: 8px（ボタン・入力欄）/ 12px（カード）/ 999px（ピル・チップ）
- タップ対象: 最低44px、録音ボタン・「構造化する」・「修正して再検査」は48px
- ピル間隔: gap 10px（最低8px）
- 本文: モバイル16px / PC15px、行間1.75
- ブレークポイントは600pxの1つだけ

## Screens / Views（実装上の注記）

### ② 更新案の承認
- カードは `data-status="approved|pending|edited|rejected"` で左ボーダー色と減光（rejected は本文 opacity .55）を切替
- **承認済みカードは編集フォームを出さない**。確定文言を `.confirmed-text`（読み取り専用ブロック）で表示し、`.confirmed-note`「承認済みのため編集できません（編集する場合は却下して差し戻してください）」を添える。却下カードも編集フォーム非表示
- 変更種別チップ `.kind` は通達Diffトークン（`--diff-mod`系）＋「変」「＋」記号併用（色覚多様性対応）
- 原文トレースは `<details>` 開閉式、根拠文は `<mark>`（萌黄ハイライト）

### ④ 日報の入力
- 注意書き2つは `<details class="run-warning">` で折りたたみ（初期表示コンパクト化）。summary は44px
- ピルはチェックボックス内包型: `.procedure-pill input:checked + span` で ✓記号＋塗り枠（色以外の判別必須）
- 録音: `.record-controls` に `.recording` 付与で赤枠面＋●点滅（`@keyframes rec-blink`、`prefers-reduced-motion` で停止）
- 進行表現: form に `.is-submitting` 付与でボタンが `.progress-state`（スピナー＋スイープ＋「30秒ほどかかる場合があります」）に置換

### ⑤⑥ 日報の結果／停止画面
- Privacy Gate サマリは同一コンポーネントの状態違い: `.gate-summary.passed|review|blocked|failed`
- passed は枠のみ緑（塗りなし・「安全です」と誇張しない）。blocked/failed だけが赤塗り `--gate-blocked-fill` #A8391F（**ページ内唯一の赤塗り**という強度設計。他で赤塗りを使わないこと）
- failed は ❓アイコンで blocked（⛔）と区別
- 検出チップは「氏名 2件」のような**種別と件数のみ**。値そのものは絶対に表示しない
- 日報テーブルはモバイルで縦2段カード（項目名上・状態＋値下）、600px以上で `grid-template-columns:260px 1fr`

### ⑦ 月報
- 記入率バー: `.rate-bar` 内の `.rate-fill` を width% で。0% は `.rate-bar.zero`（斜線地）で「空」を明示。数値（n/9・%）併記必須
- バイタル折れ線はインラインSVG直書き（stroke は `var(--moegi)` 等のトークン参照）。**数値表が正、グラフは補助**

### ⑧ 配信キュー
- **手動の再送ボタンは絶対に置かない**（設計上の決定）
- 送信待ちは大きな数字＋「操作は必要ありません」

## 文言ルール（ui-brief §6・厳守）
- 禁止語: 「匿名化済み」「個人情報ではありません」「完全に安全」「完全匿名」
- 正: 「直接識別子を仮名化しました。仮名化後も病状・服薬などの要配慮個人情報は残ります」
- 画面に出すのは仮名（PATIENT_001 等）とタグ（[[PHONE]] 等）のみ。検出値そのものは出さない

## Assets
画像アセットなし。絵文字（⛔ ⚠️ 🔎 ❓ ✓ 等）とインラインSVG（月報の折れ線）のみ。
