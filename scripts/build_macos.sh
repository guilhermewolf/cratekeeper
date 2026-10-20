#!/usr/bin/env bash
# Build cratekeeper.app for the architecture of this machine, sign it,
# smoke-test it and wrap it in a DMG (+ zip). Output lands in dist/.
#
#   scripts/build_macos.sh
#
# Environment (all optional):
#   PYTHON                 interpreter with cratekeeper[app,build] installed
#                          (default: .venv/bin/python if present, else python3)
#   MACOS_SIGN_IDENTITY    "Developer ID Application: ..." — otherwise ad-hoc signed
#   APPLE_ID / APPLE_TEAM_ID / APPLE_APP_PASSWORD   notarize + staple when all set
#   SKIP_GUI_SMOKE=1       don't open the window during the smoke test
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
fi

VERSION="$("$PY" -c 'import cratekeeper; print(cratekeeper.__version__)')"
ARCH="$(uname -m)"
APP="dist/cratekeeper.app"
BIN="$APP/Contents/MacOS/cratekeeper"
BASENAME="cratekeeper-${VERSION}-macos-${ARCH}"

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

step "cratekeeper $VERSION for $ARCH using $("$PY" --version)"
"$PY" -c 'import PyInstaller, webview' 2>/dev/null || {
  echo "error: install build deps first:  $PY -m pip install -e '.[app,build]'" >&2; exit 1; }

# ---- icon ------------------------------------------------------------------
step "icon (.icns)"
rm -rf build/icon.iconset build/icon.icns
mkdir -p build/icon.iconset
for s in 16 32 128 256 512; do
  sips -z "$s" "$s" packaging/icon.png --out "build/icon.iconset/icon_${s}x${s}.png" >/dev/null
  d=$((s * 2))
  sips -z "$d" "$d" packaging/icon.png --out "build/icon.iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns build/icon.iconset -o build/icon.icns

# ---- bundle ----------------------------------------------------------------
step "pyinstaller"
rm -rf "$APP" dist/cratekeeper
"$PY" -m PyInstaller --noconfirm --clean --log-level WARN packaging/cratekeeper.spec
test -x "$BIN"

# ---- sign ------------------------------------------------------------------
if [ -n "${MACOS_SIGN_IDENTITY:-}" ]; then
  step "codesign with Developer ID"
  codesign --force --deep --options runtime --timestamp \
    --entitlements packaging/entitlements.plist \
    --sign "$MACOS_SIGN_IDENTITY" "$APP"
else
  step "codesign (ad-hoc; set MACOS_SIGN_IDENTITY for a distributable signature)"
  codesign --force --deep --sign - "$APP"
fi
codesign --verify --deep --strict "$APP"

# ---- smoke tests -----------------------------------------------------------
step "smoke: CLI"
"$BIN" --version | grep -q "cratekeeper $VERSION"

step "smoke: headless server"
SMOKE_DIR="$(mktemp -d)"
mkdir -p "$SMOKE_DIR/lib" "$SMOKE_DIR/data"
cp tests/fixtures/silence.mp3 "$SMOKE_DIR/lib/one.mp3"
PORT="$("$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
printf 'library_path = "%s"\ndata_dir = "%s"\nport = %s\n' "$SMOKE_DIR/lib" "$SMOKE_DIR/data" "$PORT" \
  > "$SMOKE_DIR/config.toml"
CRATEKEEPER_CONFIG="$SMOKE_DIR/config.toml" "$BIN" serve >"$SMOKE_DIR/serve.log" 2>&1 &
SERVE_PID=$!
cleanup() { kill "$SERVE_PID" 2>/dev/null || true; }
trap cleanup EXIT
ok=0
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:$PORT/api/health" 2>/dev/null | grep -q '"cratekeeper"'; then ok=1; break; fi
  sleep 0.5
done
if [ "$ok" != 1 ]; then echo "server smoke failed:"; cat "$SMOKE_DIR/serve.log"; exit 1; fi
# the scan kicked off at startup must index the fixture and not crash on tags/hashing
for _ in $(seq 1 40); do
  if curl -fsS "http://127.0.0.1:$PORT/api/stats" | grep -q '"total":1'; then break; fi
  sleep 0.5
done
curl -fsS "http://127.0.0.1:$PORT/api/stats" | grep -q '"total":1' || { echo "scan did not index fixture"; cat "$SMOKE_DIR/serve.log"; exit 1; }
curl -fsS "http://127.0.0.1:$PORT/" | grep -q "cratekeeper"
curl -fsS "http://127.0.0.1:$PORT/static/app.js" >/dev/null
kill "$SERVE_PID"; wait "$SERVE_PID" 2>/dev/null || true
trap - EXIT
grep -E "Traceback| ERROR " "$SMOKE_DIR/serve.log" && { echo "errors in serve log"; exit 1; } || true

if [ "${SKIP_GUI_SMOKE:-0}" != 1 ]; then
  step "smoke: desktop window (opens briefly)"
  CRATEKEEPER_CONFIG="$SMOKE_DIR/config.toml" CRATEKEEPER_SMOKE=1 "$BIN"
fi
rm -rf "$SMOKE_DIR"

# ---- notarize the app (before it goes into the DMG, so the ticket is stapled inside) ----
NOTARIZE=0
if [ -n "${APPLE_ID:-}" ] && [ -n "${APPLE_TEAM_ID:-}" ] && [ -n "${APPLE_APP_PASSWORD:-}" ]; then
  NOTARIZE=1
  step "notarize app"
  rm -f "dist/$BASENAME.zip"
  ditto -c -k --keepParent "$APP" "dist/$BASENAME.zip"
  xcrun notarytool submit "dist/$BASENAME.zip" --apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID" \
    --password "$APPLE_APP_PASSWORD" --wait
  xcrun stapler staple "$APP"
fi

# ---- package ---------------------------------------------------------------
step "dmg + zip"
rm -rf dist/dmg "dist/$BASENAME.dmg" "dist/$BASENAME.zip"
mkdir -p dist/dmg
cp -R "$APP" dist/dmg/
ln -s /Applications dist/dmg/Applications
hdiutil create -volname "cratekeeper" -srcfolder dist/dmg -ov -format UDZO -quiet "dist/$BASENAME.dmg"
ditto -c -k --keepParent "$APP" "dist/$BASENAME.zip"
rm -rf dist/dmg

if [ "$NOTARIZE" = 1 ]; then
  step "notarize dmg"
  xcrun notarytool submit "dist/$BASENAME.dmg" --apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID" \
    --password "$APPLE_APP_PASSWORD" --wait
  xcrun stapler staple "dist/$BASENAME.dmg"
fi

step "done"
ls -lh "dist/$BASENAME.dmg" "dist/$BASENAME.zip"
