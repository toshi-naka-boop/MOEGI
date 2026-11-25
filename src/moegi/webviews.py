"""画面の部品（HTMLの組み立て）。日報・SOAP・月報の表示だけを担当する純粋な関数群。

webapp.py が大きくなりすぎないよう分けたもの。状態（DB・LLM）には触れない。
"""

from __future__ import annotations

import html

from moegi import monthly, nippou


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
      <p class="section-hint">「未記入」は、発話から読み取れなかった項目です。保存した日報はこの画面では直せません。
        補いたいときは、足りない内容を加えた文章でもう一度入力してください（同じ内容の記録は二重に保存しません）。
        誤って保存した記録は、月報の画面から削除できます。</p>
    </section>
    {evaluation_html}
    """


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
