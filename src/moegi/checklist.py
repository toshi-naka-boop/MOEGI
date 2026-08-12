"""承認済み更新案をチェックリストへ適用する（G6 Before/After 比較画面の土台）。

design.md G6:
    「**同一の発話データ**を変更前／変更後のチェックリストで処理した2結果を
      Before/After比較画面に並べる。評価は**看護目標を参照して**生成されていること」

このモジュールは純粋な変換だけを持ち、DBもLLMも触らない（テストしやすさのため）。

ledger R-88（不変ID導入）:
    突き合わせは `Impact.sitedoc_item_key`（= `ChecklistItem.key`・不変）で行う。
    ラベルは表示用の可変文言なので、承認で書き換わった後は同一性の判定に使えない。
    不変ID導入前に記録された更新案は key を持たない（NULL）ので、その場合だけ
    ラベル一致へフォールバックする。どちらでも当たらなければ `unmatched` に残して
    画面へ出す——取りこぼしを黙って捨てない。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace

from moegi.pipeline import ChecklistItem, SiteDoc


def _new_item_id(label: str, existing: list[ChecklistItem]) -> str:
    """承認で追加された新規項目に不変IDを割り当てる（ledger R-88）。

    ラベルのハッシュから決定論的に作る——同じ更新案を2回適用しても同じIDになるので、
    Before/After比較をやり直しても日報・月報との紐付けがぶれない。衝突した場合だけ
    連番を足す（実質起きないが、起きたときに黙って上書きしない）。
    """
    base = f"item-{hashlib.sha1(label.encode('utf-8')).hexdigest()[:10]}"
    taken = {item.key for item in existing}
    if base not in taken:
        return base
    for suffix in range(2, 100):
        candidate = f"{base}-{suffix}"
        if candidate not in taken:
            return candidate
    return base

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

        target_key = proposal.get("sitedoc_item_key")
        target_label = proposal.get("sitedoc_item_id")
        if target_key is None and target_label is None:
            new_item = ChecklistItem(
                sitedoc_id=sitedoc.id,
                label=after_label,
                kind="自由記述",
                goal_ref=goal_ref if goal_ref and "評価" in after_label else None,
                required=True,
                # 新規項目にも不変IDを与える（ledger R-88）。以降この項目の文言が
                # 変わっても、日報・月報・過去判定との紐付けが切れない。
                item_id=_new_item_id(after_label, items),
            )
            items.append(new_item)
            added.append(after_label)
            continue

        # 不変ID優先。旧データ（key が NULL）のときだけラベル一致へフォールバックする。
        index = None
        if target_key is not None:
            index = next((i for i, item in enumerate(items) if item.key == target_key), None)
        if index is None and target_label is not None:
            index = next((i for i, item in enumerate(items) if item.label == target_label), None)
        if index is None:
            unmatched.append(str(target_key or target_label))
            continue
        if items[index].label == after_label:
            continue  # 実質変更なし
        changed.append((items[index].label, after_label))
        items[index] = replace(items[index], label=after_label)

    after = SiteDoc(id=sitedoc.id, kind=sitedoc.kind, title=sitedoc.title, items=items)
    return ChecklistDiff(
        before=sitedoc, after=after, added=added, changed=changed, unmatched=unmatched
    )
