#!/usr/bin/env bash
# MOEGI デモ用HTTPS証明書の再発行（ledger R-82 / cert-ip-pinning の対策）。
#
# 問題: 発行済み証明書はMacのLAN IPに固定されている。テザリングや会場Wi-Fiに繋ぎ替えると
#       IPが変わり、証明書が合わなくなって実機スマホのマイク（getUserMedia）が使えなくなる。
#
# 対策: このスクリプトを繋ぎ替えた後に1回走らせる。現在のIPに加えて
#       **Bonjourの .local ホスト名**もSANに含めるので、以降IPが変わっても
#       https://<ホスト名>.local:8420/ でアクセスすれば証明書は有効なまま使える。
#       （iOS・Android ともBonjour解決に対応。テザリング環境でも同一サブネット内なら引ける）
#
# 前提: mkcert がインストール済み（brew install mkcert nss）。
#       発行自体に sudo は不要。`mkcert -install`（CAの信頼登録）だけは別途ユーザーが行う。
#
# 使い方:
#   ./scripts/reissue_cert.sh
#   .venv/bin/uvicorn moegi.webapp:app --app-dir src --host 0.0.0.0 --port 8420 \
#       --ssl-keyfile certs/moegi-local-key.pem --ssl-certfile certs/moegi-local.pem

set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v mkcert >/dev/null 2>&1; then
  echo "mkcert が見つかりません。先に: brew install mkcert nss" >&2
  exit 1
fi

# 既定経路が乗っているインターフェースのIPを採る（en0固定にすると
# テザリング時に別インターフェースになった場合に取り違える）。
IFACE="$(route -n get default 2>/dev/null | awk '/interface:/{print $2}' || true)"
IP=""
if [ -n "${IFACE}" ]; then
  IP="$(ipconfig getifaddr "${IFACE}" 2>/dev/null || true)"
fi

LOCAL_HOST="$(scutil --get LocalHostName 2>/dev/null || echo moegi).local"

mkdir -p certs

echo "インターフェース : ${IFACE:-（不明）}"
echo "現在のIP        : ${IP:-（取得できず・.local のみで発行します）}"
echo "Bonjour名       : ${LOCAL_HOST}"
echo

# shellcheck disable=SC2086 — IPが空のときに空文字を渡さないため、意図的にクォートしない
mkcert \
  -cert-file certs/moegi-local.pem \
  -key-file certs/moegi-local-key.pem \
  localhost 127.0.0.1 ::1 "${LOCAL_HOST}" ${IP}

chmod 600 certs/moegi-local-key.pem

echo
echo "発行しました。実機からは次のURLでアクセスしてください:"
echo "  https://${LOCAL_HOST}:8420/nippou   ← IPが変わっても有効（推奨）"
if [ -n "${IP}" ]; then
  echo "  https://${IP}:8420/nippou          ← 今のネットワーク限定"
fi
echo
echo "⚠️ 実機側に mkcert のルートCA（rootCA.pem）が未登録だと警告が出ます。"
echo "   CAの場所: $(mkcert -CAROOT 2>/dev/null || echo '不明')"
