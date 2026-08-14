#!/usr/bin/env bash
# MOEGI 承認UIを起動し、実機からアクセスできるURLを表示する。
#
# なぜスクリプトにしたか: 会場やテザリングでは Mac のIPが毎回変わる。起動のたびに
# `ipconfig getifaddr` を叩いて実機に打ち込むURLを組み立てるのが面倒だし、
# 「どのURLなら証明書が有効か」を毎回思い出すのも間違いのもとなので、
# 起動と同時に**そのとき有効なURLだけ**を印字する。
#
# 使い方:
#   ./scripts/serve.sh          # HTTP（Android向け・既定）
#   ./scripts/serve.sh https    # HTTPS（iPhoneの録音UI向け。要 certs/）
#
# マイク（getUserMedia）について:
#   ブラウザはマイクを **secure context** でしか許可しない。
#     - iPhone : HTTPS が要る → `https` モード＋端末にルートCAを入れる
#     - Android: `.local` の名前解決が不安定なうえCAの導入も重いので、
#                録音を実機で動かすなら Chrome の Port forwarding 経由で
#                `http://localhost:8420` を使うのが確実（ケーブル1本で済む）
#   録音UI以外（承認・Before/After・月報・配信キュー・Privacy Gateの停止）は
#   マイクを使わないので、**プレーンHTTPでそのまま動く**。

set -euo pipefail

cd "$(dirname "$0")/.."

MODE="${1:-http}"
PORT="${MOEGI_PORT:-8420}"

IFACE="$(route -n get default 2>/dev/null | awk '/interface:/{print $2}' || true)"
IP=""
if [ -n "${IFACE}" ]; then
  IP="$(ipconfig getifaddr "${IFACE}" 2>/dev/null || true)"
fi
LOCAL_HOST="$(scutil --get LocalHostName 2>/dev/null || echo moegi).local"

if lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "⚠️  ポート ${PORT} は既に使われています。古いサーバーが残っていませんか:"
  lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN | tail -n +2
  echo "   停止する場合: lsof -ti:${PORT} | xargs kill"
  exit 1
fi

ARGS=(moegi.webapp:app --app-dir src --host 0.0.0.0 --port "${PORT}")
SCHEME="http"

if [ "${MODE}" = "https" ]; then
  if [ ! -f certs/moegi-local.pem ] || [ ! -f certs/moegi-local-key.pem ]; then
    echo "certs/ が見つかりません。先に ./scripts/reissue_cert.sh を実行してください。" >&2
    exit 1
  fi
  ARGS+=(--ssl-keyfile certs/moegi-local-key.pem --ssl-certfile certs/moegi-local.pem)
  SCHEME="https"
fi

echo "────────────────────────────────────────────────────────"
echo " MOEGI 承認UI（${SCHEME}） ポート ${PORT}"
echo "────────────────────────────────────────────────────────"
echo " このMac      : ${SCHEME}://localhost:${PORT}/"
if [ -n "${IP}" ]; then
  echo " 同一ネットワーク: ${SCHEME}://${IP}:${PORT}/"
fi
if [ "${SCHEME}" = "https" ]; then
  echo " iPhone推奨   : https://${LOCAL_HOST}:${PORT}/nippou"
  if [ -n "${IP}" ] && ! openssl x509 -in certs/moegi-local.pem -noout -ext subjectAltName 2>/dev/null | grep -q "${IP}"; then
    echo ""
    echo " ⚠️  現在のIP（${IP}）が証明書に入っていません。"
    echo "     IPで繋ぐなら ./scripts/reissue_cert.sh を1回実行してください。"
  fi
else
  echo " Android      : 上の「同一ネットワーク」URLをそのまま開く（証明書もCAも不要）"
  echo "                ※ 録音UIだけはマイクの都合でHTTPでは動きません。"
  echo "                   録音を実機で試すなら Chrome の Port forwarding で"
  echo "                   http://localhost:${PORT}/nippou を使ってください。"
fi
echo "────────────────────────────────────────────────────────"

exec .venv/bin/uvicorn "${ARGS[@]}"
