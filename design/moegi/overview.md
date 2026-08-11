# MOEGI 全体像・現在地（2026-08-12時点）

> **このファイルは design.md（詳細仕様・唯一の真実）を可視化した要約。**
> 詳細な設計判断・実測値・却下案の理由は必ず [design.md](./design.md) と [ledger.md](./ledger.md) を参照すること。
> このファイルはコード変更のたびに更新する保証がない「今のスナップショット」なので、
> 数値・状態が古くなっていたら design.md / ledger.md / state.json を正とする。

## パイプライン全体図（v14・R-79/R-80反映）

```mermaid
flowchart TD
    subgraph input["① 入力"]
        OLD[旧通達PDF]
        NEW[新通達PDF]
        SITE[事業所チェックリスト<br/>項目ラベルのみ・要配慮個人情報なし]
    end

    OLD --> INGEST["取込・正規化<br/>allowlist適用<br/>(ingest.py)"]
    NEW --> INGEST
    INGEST -->|"Change{old,new,kind}"| DIFF

    subgraph pipeline["通達Diff→更新案パイプライン (pipeline.py) — 全工程クラウド既定"]
        DIFF["②差分抽出<br/>backend: orca(クラウド)<br/>通達本文のみ"]
        REQ["③-a 要件抽出<br/>backend: orca(クラウド)<br/>通達本文のみ"]
        MATCH["③-b 突合<br/>backend: orca(クラウド)<br/>再現率83.3%・誤検出35.7%"]
        DRAFT["④更新案生成<br/>backend: orca(クラウド)<br/>項目ラベルのみ"]
    end

    DIFF -->|summary| REQ
    REQ -->|Requirement| MATCH
    SITE -->|ChecklistItem×8 label/kindのみ| MATCH
    MATCH -->|"Impact{verdict,evidence}"| DRAFT
    DRAFT -->|Proposal| STORE[(SQLite<br/>changes/impacts/proposals/fax_deliveries)]

    ORCAFB["ORCA別プロバイダ<br/>orcarouter/auto<br/>主モデル失敗時のみ試行(R-79)"]
    LOCALFB["真の全断時フォールバック<br/>gemma4:26b(ローカル)<br/>両方失敗した場合のみ(R-79)"]
    DIFF -.->|主モデル失敗| ORCAFB
    REQ -.->|主モデル失敗| ORCAFB
    MATCH -.->|主モデル失敗| ORCAFB
    DRAFT -.->|主モデル失敗| ORCAFB
    ORCAFB -.->|それも失敗| LOCALFB

    STORE --> UI["承認UI (FastAPI)<br/>webapp.py・ホームリンク常設(R-78)"]
    UI -->|"原文トレース(G2)<br/>1操作で新旧本文+根拠"| UI
    UI -->|承認/却下/編集| STORE
    UI --> FAX["FAX様式PDF<br/>(fax.py・WeasyPrint)<br/>配信履歴をDBに記録(R-76)"]

    STORE -.->|"D6未着手"| LINE[LINE配布<br/>キュー型・store&forward]

    subgraph nippou["日報エージェント (nippou.py) — 要配慮個人情報を扱う唯一の工程"]
        MIC["🎙モバイル録音UI(R-80)<br/>MediaRecorder→.webm"] -->|文字起こし| GEMMA
        GWS["GWS音声入力(主・実運用)<br/>faster-whisper(オフライン代替)"] -->|文字起こし| GEMMA["gemma4:26b<br/>backend: local(常時固定)<br/>コールド17.8秒/ウォーム3.4秒"]
        BTN["処置ボタン10項目(R-80)<br/>LLMを経由せず格納"] --> GEMMA
    end
    GEMMA -.->|"接続点1箇所(D6未着手)"| SITE

    classDef done fill:#d4edda,stroke:#2e7d32,color:#1a1a1a
    classDef pending fill:#f5f5f5,stroke:#999,color:#666,stroke-dasharray: 4 3
    class OLD,NEW,SITE,INGEST,DIFF,REQ,MATCH,DRAFT,STORE,UI,ORCAFB,LOCALFB,FAX,MIC,GWS,GEMMA,BTN,nippou done
    class LINE pending
```

緑＝実装・実機検証済み。グレー点線＝未着手（LINEキュー型配布・§7台本作り直し＝D6以降）。

## 実装進捗（D1〜D8）

| 日 | 内容 | 状態 |
|---|---|---|
| D0 | 規約照会 | ✅ 完了 |
| D1 | PDF取込・allowlist・日報方針決定・性能実測 | ✅ 完了 |
| D2 | パイプライン4工程・LLMアダプタ層 | ✅ 完了 |
| D3 | ゴールドセット作成・G9計測 | ✅ 完了（G9をG9-a/G9-bに分割） |
| N-10 | evidence参照方式への変更（D3後の追加対応） | ✅ 完了 |
| D4 | 承認UI・原文トレース・薄いE2E | ✅ 完了・ユーザー確認済み（2026-08-11） |
| N-11 | プライバシー境界の再定義（③-b/④をクラウド解禁） | ✅ 完了（批評2巡・実装済み） |
| D5 | 配布（FAXプレビュー）・日報エージェント実装・オフライン薄通し・D1相当ゲート再実測・G9再測定 | ✅ 完了（2026-08-11） |
| D5フォローアップ | G9バッジ削除(R-75)・FAX配信履歴(R-76)・UX修正2件(R-78)・ORCA3段フォールバック(R-79)・モバイル日報UI(R-80) | ✅ **完了（2026-08-12・ユーザーレビュー反映）** |
| D6 | LINEキュー型チャネル・G6周りのUI（処置ボタンは前倒し実装済み）・**§7台本#6を実ワークフローに合わせて作り直す**・実機スマホでの録音UI確認 | ⬜ 未着手 |
| D7 | オフライン本番通し・精度差実測・Before動画・台本確定 | ⬜ 未着手 |
| D8 | 予備日・通しリハ | ⬜ 未着手 |

## 主な実測値（D5〜D5フォローアップ）

- **G9-b（全工程クラウド既定）**: 再現率 **83.3%**、誤検出率 **35.7%**（N-8として継続課題。UI表示はやめ、ピッチで開示する運用に変更・R-75）
- **日報エージェント速度**（gemma4:26b・`reasoning_effort="none"`設定込み）: `structure()` コールド **17.8秒**／ウォーム **3.4秒**
- **ORCAのマルチプロバイダ疎通実測（R-79）**: `orcarouter/auto`→qwen3.7-plus解決・5.2秒、`google/gemini-3.6-flash`→4.0秒、いずれも正常応答。旧R-64「未疎通」を解消
- **3段フォールバックの実機動作（R-79）**: 主モデルに存在しないモデル名を指定→自動で`orcarouter/auto`へ切替、6.6秒で正常応答
- **オフライン完走**: ORCA・別プロバイダとも到達不能な場合のみローカル（gemma4:26b）へ落ち、完走を確認

## 今、詰まっている点（要ユーザー判断・確認）

### 1. N-8: 突合の過剰検出（悪化傾向が数値化・未解決）
③-bがクラウド（claude-sonnet-5）になり再現率は大幅改善したが、誤検出率も26.2%→35.7%に上昇した。D6〜D7で対処を検討する必要がある。

### 2. §7デモ台本#6の作り直しが未着手
「日報を一言喋ってみてください」の即時構造化という台本は、実際のワークフロー（現場録音→事務所バッチ処理）と一致しない。D6で作り直す予定だが内容は未着手。

### 3. N-4: 説明資料の記述矛盾（放置されたまま）
厚労省説明資料 p.23 の開始・終了時刻の記載が「明確化」か「義務化」か、2つの調査で食い違っている。原文目視確認が未着手のまま。

### 4. R-56: ASRモデルサイズ未確定
実音声での再測定はfaster-whisper smallで一度実施（4.6秒・妥当な書き起こし）したが、モデルサイズ比較は未実施。GWS音声入力が主になったため優先度は下がった可能性がある。

### 5. モバイル日報UIの実機確認（R-80・新規）
録音UI・処置ボタンは実音声ファイルのアップロード経路のみ確認済み。実機スマホ・タブレットでのマイク許可フロー・タッチ操作は未確認（ヘッドレスブラウザでは検証不可）。

### 6. LINEキュー型配布・G6接続点（D6未着手）
日報エージェントとチェックリストの接続点（1箇所）、LINE store-and-forward、Before/After比較画面はまだ実装されていない。

## 参照

- 詳細仕様: [design.md](./design.md)
- 決定履歴・却下案の理由: [ledger.md](./ledger.md)
- 機械可読な現在状態: [state.json](./state.json)
- N-11批評原文: [reviews/round-3-opus.md](./reviews/round-3-opus.md) / [reviews/round-3-sonnet.md](./reviews/round-3-sonnet.md)
