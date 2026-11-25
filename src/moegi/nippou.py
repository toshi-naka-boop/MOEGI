"""日報エージェント（design.md §6.5）。

契約（旧コードの内部実装に依存しない）:
    NippouAgent
      set_checklist(items: list[ChecklistItem]) -> None
          承認済みチェックリストを参照として受け取る ← 本体との接続点はこの1本だけ
      transcribe(input: str | Path) -> str
          ローカルASR（オフライン必須・§5）。テキスト入力でも同じ経路に入る（G7）
      structure(text: str) -> NippouRecord
          set_checklist で受け取った項目に照合した構造化記録を返す
      structure_soap(text: str) -> SoapRecord
          同じ発話をSOAP形式（S/O/A/P）へ構造化して返す

⚠️ structure() / structure_soap() に渡してよいのは、Privacy Gate を通過した
   **仮名化済みテキスト**だけ（design/moegi/privacy.md §4）。原文をそのまま渡す経路は
   webapp.py 側に作っていない。このモジュール自体は仮名化の有無を検査しない——
   検査は privacy.gate に集約し、既存の構造化ロジックには手を入れない方針のため。

design.md §6.6・N-11: structure() は要配慮個人情報（患者の看護記録）を扱う唯一の工程で、
常にローカル（gemma4:26b・stage="nippou"）。transcribe() の音声入力もオフライン必須（R-13）。
実運用ではGWSドキュメントの音声入力が主で、faster-whisperはオフライン時の代替（ledger N-11）。

批評 Y-2（design.md §6.5「評価の記録」の実体要件）: evaluation は goal_ref/achievement/effect を
別フィールドで持つ。goal_ref が空のまま achievement を返した場合は「未達成」とみなす
（目標を参照せずに書いた「評価」は条文の要求を満たさないため、これはLLMの判断に委ねず
コード側で強制する）。

ledger R-77・R-80: 処置内容はLLMの自由記述抽出に任せない（処置名の表記揺れ・誤認識リスク
のため）。呼び出し側（webapp.py）がボタン選択式UIから確定した処置名のリストをそのまま
structure() に渡し、NippouRecord.procedures にLLMを経由せず格納する。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from moegi.llm import LLMAdapter
from moegi.jsonutil import extract_json_payload as _extract_json_payload
from moegi.pipeline import ChecklistItem

_REPO_ROOT = Path(__file__).resolve().parents[2]

# faster-whisperは重い依存（ctranslate2・モデルダウンロード）なので、実際に
# transcribe()が音声入力を受けたときだけ遅延importする（テキストのみで動かす
# 場合や、パイプライン側のimportコストを抑えるため）。
# .webm はブラウザのMediaRecorder API既定の出力形式（webapp.py の録音UI・ledger R-80）。
# faster-whisperはPyAV経由でデコードするためコンテナ形式は問わない。
_AUDIO_SUFFIXES = frozenset(
    {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".webm", ".aac", ".mp4", ".opus", ".3gp"}
)


#: 画面（webapp）が、受け付ける音声ファイルの拡張子を案内・検査するために使う。
AUDIO_SUFFIXES = _AUDIO_SUFFIXES


class UnsupportedAudioError(ValueError):
    """音声ファイルとして扱えない拡張子・存在しないファイル。"""


@dataclass
class NippouItem:
    """日報の1項目（design.md §6.4 NippouRecord.items の要素）。

    ledger R-88: `checklist_item_id` は**表示用のラベル**（可変）。月報の集計など
    同一性が要る場面では `checklist_item_key`（不変ID）を使うこと。
    """

    checklist_item_id: str
    value: str
    filled: bool
    checklist_item_key: str = ""


@dataclass
class Evaluation:
    """design.md §6.5「評価の記録」の実体要件（批評Y-2）。"""

    goal_ref: str | None
    achievement: str
    effect: str
    filled: bool


@dataclass
class NippouRecord:
    """design.md §6.4 NippouRecord。"""

    checklist_version: str
    utterance_id: str
    items: list[NippouItem] = field(default_factory=list)
    evaluation: Evaluation | None = None
    procedures: list[str] = field(default_factory=list)
    #: ローカルLLMのtimeout・接続失敗で構造化できなかった。全項目が未記入に見えるが、
    #: 「記入が無かった」のではなく「判定できなかった」ので、保存してはいけない。
    undecidable: bool = False


@dataclass
class SoapRecord:
    """SOAP形式の看護記録。

    訪問看護記録IIで一般的な4区分。日報（チェックリスト項目への充足判定）とは
    出力形式が違うだけで、入力も実行主体も同じ——**どちらも Privacy Gate を
    通過した仮名化済みテキストだけを受け取る**（design/moegi/privacy.md §4）。

    undecidable は、ローカルLLMのtimeout・応答形式不正で構造化できなかった場合に立つ。
    design.md §6.6 の完走優先方針に合わせ、例外を上げず「判定不能」として返す。
    """

    utterance_id: str
    subjective: str = ""
    objective: str = ""
    assessment: str = ""
    plan: str = ""
    procedures: list[str] = field(default_factory=list)
    undecidable: bool = False


_STRUCTURE_PROMPT = """あなたは訪問看護の記録担当者です。以下の発話内容を、事業所チェックリストの
各項目に照合して構造化してください。

【発話内容】
{text}

【チェックリスト項目】
{items_block}
{evaluation_block}
【出力形式】
次の形のJSONオブジェクトのみを出力すること。前置き・説明文・コードブロック記号は付けないこと。
items には項目番号 {id_list} を1つ残らず含めること。
各要素は item_id・filled・value の3つのキーを持つこと。filled は true（発話内容に言及があった）
か false（言及なし）の真偽値。value は発話内容から該当箇所を抜き出した短い文（無ければ空文字）。
{evaluation_output_instruction}

出力例:
{{"items": [{{"item_id": "1", "filled": true, "value": "バイタル異常なし"}}, {{"item_id": "2", "filled": false, "value": ""}}]{evaluation_output_example}}}
"""

_EVALUATION_BLOCK_TEMPLATE = """
【評価対象の看護目標】
{goal_ref}
"""

_EVALUATION_INSTRUCTION = (
    'evaluationには achievement（目標達成の程度）・effect（その効果）を発話内容から抽出して'
    "入れること。発話内容に評価に relate する言及が無ければ achievement・effect は空文字にすること。"
)
_EVALUATION_EXAMPLE = ', "evaluation": {"achievement": "順調に経過", "effect": "疼痛が軽減"}'


_SOAP_PROMPT = """あなたは訪問看護の記録担当者です。以下の発話内容を、看護記録のSOAP形式へ
構造化してください。

【発話内容】
{text}

【各区分の意味】
- subjective（S・主観的情報）: 利用者・家族が話した内容、訴え、自覚症状
- objective（O・客観的情報）: 観察・測定した事実。バイタルサイン、皮膚・創部の状態、ADLの様子
- assessment（A・アセスメント）: SとOから導いた看護上の判断・評価
- plan（P・計画）: 次回訪問までの対応、継続observation、指導内容

【必ず守ること】
- 発話内容に無いことを書かないこと。該当する情報が無い区分は空文字にすること
- PATIENT_001・NURSE_001・長女 のような仮名、[[PHONE]]・[[ADDRESS]] のようなタグは
  **そのまま書き写す**こと。実名や具体的な値に戻そうとしないこと
- 病名・症状・服薬・処置・バイタルの数値は落とさずに記載すること

【出力形式】
次のJSONオブジェクトのみを出力すること。前置き・説明文・コードブロック記号は付けないこと。
subjective・objective・assessment・plan の4つのキーを必ず持ち、値はいずれも文字列。

出力例:
{{"subjective": "腰の痛みが昨日より楽になったとの訴えあり。", "objective": "血圧128/78、体温36.5度、脈拍70。創部の発赤なし。", "assessment": "疼痛はコントロールできており、創部感染兆候なし。", "plan": "次回訪問時も疼痛スケールを確認し、体位交換の指導を継続する。"}}
"""


def _format_checklist_block(items: list[ChecklistItem]) -> str:
    return "\n".join(f"{i}. {item.label}（種別: {item.kind}）" for i, item in enumerate(items, start=1))


class NippouAgent:
    """design.md §6.5 の契約を満たす日報エージェント本体（最小再実装・ledger R-52/R-53）。"""

    def __init__(self, adapter: LLMAdapter, *, whisper_model_size: str = "small") -> None:
        self._adapter = adapter
        self._checklist: list[ChecklistItem] = []
        self._whisper_model_size = whisper_model_size
        self._whisper_model = None  # 遅延ロード（コールド時間・メモリを使う場面を絞る）

    def set_checklist(self, items: list[ChecklistItem]) -> None:
        """承認済みチェックリストを参照として受け取る。本体との接続点はこの1本だけ。"""
        self._checklist = items

    def transcribe(self, input_: str | Path) -> str:
        """design.md §6.5・G7: テキスト入力でも音声入力でも同じ経路に入る。

        実運用ではGWSドキュメントの音声入力（クラウド）が主で、このメソッドが
        実際に文字起こしを行うのはオフライン時の代替経路（faster-whisper）としてのみ
        （ledger N-11）。音声ファイルパスが渡された場合だけASRを起動し、それ以外
        （すでに文字起こし済みのテキスト）はそのまま返す。
        """
        if isinstance(input_, Path):
            # Path で渡されたものは必ずファイル。拡張子が音声でなければ、ファイル名の文字列を
            # 「発話」として下流へ流さずに止める（静かに中身の無い日報になるのを防ぐ）。
            if input_.suffix.lower() not in _AUDIO_SUFFIXES or not input_.exists():
                raise UnsupportedAudioError("音声ファイルとして読み込めません")
            return self._transcribe_audio(input_)
        path = Path(str(input_))
        if path.suffix.lower() in _AUDIO_SUFFIXES and path.exists():
            return self._transcribe_audio(path)
        return str(input_)

    def _transcribe_audio(self, path: Path) -> str:
        if self._whisper_model is None:
            from faster_whisper import WhisperModel  # 遅延import（重い依存）

            self._whisper_model = WhisperModel(self._whisper_model_size, device="cpu", compute_type="int8")
        segments, _ = self._whisper_model.transcribe(str(path), language="ja")
        return "".join(segment.text for segment in segments).strip()

    def structure(
        self, text: str, *, utterance_id: str = "utt", procedures: list[str] | None = None
    ) -> NippouRecord:
        """set_checklist で受け取った項目に照合した構造化記録を返す。

        design.md §6.5・批評C-5: 自由文生成ではなく「項目ごとの充足判定」を返す形式に
        固定する（非決定性対策）。評価付き項目（goal_refを持つ項目）があれば、
        evaluationフィールドを別途生成する（批評Y-2）。

        procedures はLLMに通さず、そのままNippouRecordへ格納する（ledger R-77・R-80:
        処置名は呼び出し側のボタン選択式UIで確定させ、LLMの表記揺れ・誤認識を避ける）。
        """
        procedures = procedures or []
        if not self._checklist:
            raise ValueError("set_checklist() を先に呼ぶこと")

        goal_item = next((i for i in self._checklist if i.goal_ref), None)
        id_list = "、".join(str(i) for i in range(1, len(self._checklist) + 1))
        evaluation_block = (
            _EVALUATION_BLOCK_TEMPLATE.format(goal_ref=goal_item.goal_ref) if goal_item else ""
        )
        prompt = _STRUCTURE_PROMPT.format(
            text=text,
            items_block=_format_checklist_block(self._checklist),
            evaluation_block=evaluation_block,
            id_list=id_list,
            evaluation_output_instruction=_EVALUATION_INSTRUCTION if goal_item else "",
            evaluation_output_example=_EVALUATION_EXAMPLE if goal_item else "",
        )

        result = self._adapter.complete("nippou", prompt)
        checklist_version = f"sitedoc-{len(self._checklist)}items"

        if result.get("timeout"):
            # design.md §6.6（v10）の完走優先方針をnippouにも適用: 例外を上げず、
            # 全項目「未記入」として記録を返す（黙って落とさない）。
            items = [
                NippouItem(
                    checklist_item_id=item.label, value="", filled=False, checklist_item_key=item.key
                )
                for item in self._checklist
            ]
            evaluation = self._build_evaluation(goal_item, None) if goal_item else None
            return NippouRecord(
                checklist_version=checklist_version,
                utterance_id=utterance_id,
                items=items,
                evaluation=evaluation,
                procedures=procedures,
                undecidable=True,
            )

        payload = _extract_json_payload(result["text"])
        if not payload:
            # JSON として読めない応答は、全項目「未記入」の日報として保存しない（SOAP と同じ扱い）。
            items = [
                NippouItem(
                    checklist_item_id=item.label, value="", filled=False, checklist_item_key=item.key
                )
                for item in self._checklist
            ]
            evaluation = self._build_evaluation(goal_item, None) if goal_item else None
            return NippouRecord(
                checklist_version=checklist_version,
                utterance_id=utterance_id,
                items=items,
                evaluation=evaluation,
                procedures=procedures,
                undecidable=True,
            )
        entries_by_id = {
            str(e["item_id"]).strip(): e
            for e in payload.get("items") or []
            if isinstance(e, dict) and e.get("item_id") is not None
        }

        items = []
        for index, item in enumerate(self._checklist, start=1):
            entry = entries_by_id.get(str(index))
            if entry is None:
                items.append(
                    NippouItem(
                        checklist_item_id=item.label,
                        value="",
                        filled=False,
                        checklist_item_key=item.key,
                    )
                )
            else:
                items.append(
                    NippouItem(
                        checklist_item_id=item.label,
                        value=str(entry.get("value") or ""),
                        filled=bool(entry.get("filled")),
                        checklist_item_key=item.key,
                    )
                )

        evaluation = self._build_evaluation(goal_item, payload.get("evaluation")) if goal_item else None
        return NippouRecord(
            checklist_version=checklist_version,
            utterance_id=utterance_id,
            items=items,
            evaluation=evaluation,
            procedures=procedures,
        )

    def structure_soap(
        self, text: str, *, utterance_id: str = "utt", procedures: list[str] | None = None
    ) -> SoapRecord:
        """発話内容をSOAP形式（S/O/A/P）へ構造化する。

        日報の structure() と同じ規律で作ってある:
          - stage は "nippou"（要配慮個人情報を扱うためローカル固定・llm.py が強制）
          - 自由文の一括生成ではなくキー固定のJSONを返させる（非決定性対策・批評C-5）
          - 処置内容はLLMを経由せず、呼び出し側のボタン選択をそのまま格納（ledger R-77）
          - timeout・形式不正では例外を上げず undecidable=True で返す（design.md §6.6）

        入力は Privacy Gate を通過した仮名化済みテキストであることを前提にしている。
        仮名（PATIENT_001 等）とタグ（[[PHONE]] 等）はそのまま残すようプロンプトで
        指示する——ここで元に戻そうとされると仮名化の意味が消える。
        """
        procedures = procedures or []
        result = self._adapter.complete("nippou", _SOAP_PROMPT.format(text=text))

        if result.get("timeout"):
            return SoapRecord(utterance_id=utterance_id, procedures=procedures, undecidable=True)

        payload = _extract_json_payload(result["text"])
        if not payload:
            return SoapRecord(utterance_id=utterance_id, procedures=procedures, undecidable=True)

        def _field(key: str) -> str:
            return str(payload.get(key) or "").strip()

        return SoapRecord(
            utterance_id=utterance_id,
            subjective=_field("subjective"),
            objective=_field("objective"),
            assessment=_field("assessment"),
            plan=_field("plan"),
            procedures=procedures,
        )

    def _build_evaluation(self, goal_item: ChecklistItem | None, raw: dict | None) -> Evaluation:
        """批評Y-2: goal_refが空のままachievementを生成した場合は「未達成」とみなす。

        これはLLMの判断に委ねず、コード側で強制する（目標を参照せずに書いた「評価」は
        条文の要求を満たさないため）。
        """
        goal_ref = goal_item.goal_ref if goal_item else None
        achievement = str((raw or {}).get("achievement") or "").strip()
        effect = str((raw or {}).get("effect") or "").strip()
        if not goal_ref:
            achievement = "未達成"
        filled = bool(achievement) and bool(effect) and bool(goal_ref)
        return Evaluation(goal_ref=goal_ref, achievement=achievement or "未達成", effect=effect, filled=filled)
