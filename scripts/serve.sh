#!/usr/bin/env bash
# MOEGI 承認UIを起動し、実機からアクセスできるURLを表示する。
#
# なぜスクリプトにしたか: 会場やテザリングでは Mac のIPが毎回変わる。起動のたびに
# `ipconfig getifaddr` を叩いて実機に打ち込むURLを組み立てるのが面倒だし、
# 「どのURLなら証明書が有効か」を毎回思い出すのも間違いのもとなので、
# 起動と同時に**そのとき有効なURLだけ**を印字する。
#
# 使い方:
#   ./scripts/serve.sh          # HTTP・このPCだけ（127.0.0.1）で待ち受ける（既定）
#   ./scripts/serve.sh https    # HTTPS（iPhoneの録音UI向け。要 certs/）
#   MOEGI_LAN=1 MOEGI_ACCESS_TOKEN=... ./scripts/serve.sh
#                               # 同じネットワークの端末にも公開する。パスワード
#                               # （MOEGI_ACCESS_TOKEN）が未設定なら起動しない。
#                               # ※ HTTPのためパスワードは暗号化されずに流れる。信頼できる閉じたLAN内だけで使うこと
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

HOST="127.0.0.1"
if [ "${MOEGI_LAN:-}" = "1" ]; then
  if [ -z "${MOEGI_ACCESS_TOKEN:-}" ]; then
    echo "エラー: LAN公開（MOEGI_LAN=1）には、パスワードとして MOEGI_ACCESS_TOKEN の設定が必要です。" >&2
    echo "        認証なしで同じネットワークの誰にでも承認・閲覧・入力を許すことになるため、起動しません。" >&2
    exit 1
  fi
  HOST="0.0.0.0"
fi

# 同一ネットワーク用のIP表示（macOSのコマンド。取れない環境では表示を省くだけで、起動には影響しない）
IFACE=""
IP=""
LOCAL_HOST="moegi.local"
if [ "${HOST}" = "0.0.0.0" ]; then
  IFACE="$(route -n get default 2>/dev/null | awk '/interface:/{print $2}' || true)"
  if [ -n "${IFACE}" ]; then
    IP="$(ipconfig getifaddr "${IFACE}" 2>/dev/null || true)"
  fi
  if [ -z "${IP}" ]; then
    IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  fi
  LOCAL_HOST="$(scutil --get LocalHostName 2>/dev/null || echo moegi).local"
fi

if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "⚠️  ポート ${PORT} は既に使われています。古いサーバーが残っていませんか:"
  lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN | tail -n +2
  echo "   停止する場合: lsof -ti:${PORT} | xargs kill"
  exit 1
fi

ARGS=(moegi.webapp:app --app-dir src --host "${HOST}" --port "${PORT}")
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
echo " このPC        : ${SCHEME}://localhost:${PORT}/"
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
  if [ "${HOST}" = "0.0.0.0" ]; then
    echo " Android      : 上の「同一ネットワーク」URLをそのまま開く（証明書もCAも不要）"
  fi
  echo "                ※ 録音UIだけはマイクの都合でHTTPでは動きません。"
  echo "                   録音を実機で試すなら Chrome の Port forwarding で"
  echo "                   http://localhost:${PORT}/nippou を使ってください。"
fi
if [ "${HOST}" = "127.0.0.1" ]; then
  echo " ※ このPCだけで待ち受けています。他の端末から開く場合は MOEGI_LAN=1 と MOEGI_ACCESS_TOKEN を指定してください。"
fi
echo "────────────────────────────────────────────────────────"

exec .venv/bin/uvicorn "${ARGS[@]}"
