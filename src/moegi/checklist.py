"""承認済み更新案をチェックリストへ適用する（G6 Before/After 比較画面の土台）。

design.md G6:
    「**同一の発話データ**を変更前／変更後のチェックリストで処理した2結果を
      Before/After比較画面に並べる。評価は**看護目標を参照して**生成されていること」

このモジュールは純粋な変換だけを持ち、DBもLLMも触らない（テストしやすさのため）。

⚠️ 既知の限界（ledger の checklist-item-identity）:
    `Impact.sitedoc_item_id` は項目ラベル文字列そのもの（可変な自然キー）。
    そのため「承認して文言が変わったチェックリスト」に対して**別のrunの承認済み更新案を
    もう一度適用する**と、ラベルが一致せず該当項目を見失う。ここではその取りこぼしを
    黙って捨てず `unmatched` に残して画面へ出す——見失ったことが分かれば、
    不変IDを入れるかどうかの判断材料になる。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from moegi.pipeline import ChecklistItem, SiteDoc

#: 承認扱いにするステータス（未承認・却下は反映しない）。
APPLIED_STATUSES = frozenset({"承認", "編集済"})


@dataclass
class ChecklistDiff:
    """変更前後のチェックリストと、その差分の内訳。"""

    before: SiteDoc
    after: SiteDoc
    #: 新規追加された項目ラベル
    added: list[str] = field(default_factory=list)
    #: 文言が書き換わった項目（変更前ラベル, 変更後ラベル）
    changed: list[tuple[str, str]] = field(default_factory=list)
    #: 承認済みだが、現在のチェックリストに該当項目が見つからなかったラベル
    unmatched: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.changed)


def apply_approved(
    sitedoc: SiteDoc, proposals: list[dict], *, goal_ref: str | None = None
) -> ChecklistDiff:
    """承認済み（承認・編集済）の更新案をチェックリストへ適用する。

    proposals は store.list_proposals_for_review() の戻り値の形
    （sitedoc_item_id / after / status を持つ dict のリスト）。

    goal_ref: 新規追加された「評価」系の項目に紐づける看護目標。
        design.md G6 の「評価は**看護目標を参照して**生成されていること」を満たすために要る。
        `nippou.NippouAgent.structure()` は goal_ref を持つ項目があるときだけ evaluation を
        生成するので、これが無いと新規項目がラベルとしてしか現れない。
        推測に頼らず**呼び出し側が明示的に渡す**——目標本文は利用者ごとに違い、
        更新案の文面から導けるものではないため。
    """
    items = list(sitedoc.items)
    added: list[str] = []
    changed: list[tuple[str, str]] = []
    unmatched: list[str] = []

    for proposal in proposals:
        if proposal.get("status") not in APPLIED_STATUSES:
            continue
        after_label = str(proposal.get("after") or "").strip()
        if not after_label or after_label == "判定不能":
            continue

        target_label = proposal.get("sitedoc_item_id")
        if target_label is None:
            new_item = ChecklistItem(
                sitedoc_id=sitedoc.id,
                label=after_label,
                kind="自由記述",
                goal_ref=goal_ref if goal_ref and "評価" in after_label else None,
                required=True,
            )
            items.append(new_item)
            added.append(after_label)
            continue

        index = next((i for i, item in enumerate(items) if item.label == target_label), None)
        if index is None:
            # ラベルが既に書き換わっている等で該当項目が無い。黙って捨てない。
            unmatched.append(str(target_label))
            continue
        if items[index].label == after_label:
            continue  # 実質変更なし
        changed.append((items[index].label, after_label))
        items[index] = replace(items[index], label=after_label)

    after = SiteDoc(id=sitedoc.id, kind=sitedoc.kind, title=sitedoc.title, items=items)
    return ChecklistDiff(
        before=sitedoc, after=after, added=added, changed=changed, unmatched=unmatched
    )
