#!/usr/bin/env bash
# 分析（通達Diff）に使う通達PDFを data/notices/ に取得する。
#
# 取得元は厚生労働省の公開資料（scripts/verify_allowlist.py に書かれているURLと同じ）。
# 使い方:  ./scripts/fetch_notices.sh
#
# ⚠️ このスクリプトは、取得したファイルがPDFの形をしていることまでを確かめます。
#    内容が意図した通達かどうかは、取得後に目視で確認してください（ハッシュは固定していません）。
set -euo pipefail

cd "$(dirname "$0")/.."
DEST="data/notices"
mkdir -p "${DEST}"

fetch() {
  local name="$1" url="$2"
  if [ -s "${DEST}/${name}" ]; then
    echo "既にあります: ${DEST}/${name}（取得し直す場合は先に削除してください）"
    return 0
  fi
  echo "取得中: ${url}"
  curl --fail --silent --show-error --location --max-time 120 -o "${DEST}/${name}.part" "${url}"
  if [ "$(head -c 4 "${DEST}/${name}.part")" != "%PDF" ]; then
    rm -f "${DEST}/${name}.part"
    echo "エラー: ${name} がPDFではありませんでした。URLが変更された可能性があります。" >&2
    exit 1
  fi
  mv "${DEST}/${name}.part" "${DEST}/${name}"
  echo "保存: ${DEST}/${name}  SHA256=$( (shasum -a 256 "${DEST}/${name}" 2>/dev/null || sha256sum "${DEST}/${name}") | awk '{print $1}')"
}

# 旧: 令和6年度改定時の通知（保発0305第12号） / 新: 令和8年3月5日 保発0305第19号
fetch "old_hohatsu0305-12.pdf" "https://www.mhlw.go.jp/content/12404000/001241054.pdf"
fetch "new_hohatsu0305-19.pdf" "https://www.mhlw.go.jp/content/12400000/001686845.pdf"
echo "完了。サーバーを起動して「分析を実行」を押せます。"
