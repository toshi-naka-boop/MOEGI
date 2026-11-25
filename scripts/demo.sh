#!/usr/bin/env bash
# MOEGI デモモード。Ollama・ORCA・通達PDFが無いPCでも、画面の流れを一周して試せる。
#
#   ./scripts/demo.sh
#
# - AIは動きません。更新案は合成データ2件、日報・SOAPの判定は単純な規則です（画面にも表示されます）。
#   Privacy Gate の規則・辞書による検査と再検査はコードによる本物の検査です。
# - 記録は data/demo/ に作られ、本番用の data/ とは混ざりません。やり直すときは data/demo/ を消してください。
# - 起動後の手順は README の「まず試す（デモモード）」を見てください。
set -euo pipefail

cd "$(dirname "$0")/.."
export MOEGI_DEMO=1
export MOEGI_DATA_DIR="${MOEGI_DATA_DIR:-$PWD/data/demo}"

echo "デモモードで起動します（記録の置き場所: ${MOEGI_DATA_DIR}）"
exec ./scripts/serve.sh "$@"
