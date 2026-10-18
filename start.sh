#!/usr/bin/env sh
# Starts the TG Cleaner local service.
#   ./start.sh            normal use (runs one-time setup first if needed)
#   ./start.sh --demo     fake data, never contacts Telegram
#   ./start.sh setup      re-enter API ID / hash
#   ./start.sh logout     log out of Telegram and delete the local session
set -eu
cd "$(dirname "$0")/service"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Python 3.10+ is required (python3 not found)." >&2
  exit 1
fi
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "Python 3.10 or newer is required." >&2
  exit 1
}

if [ ! -x .venv/bin/python ]; then
  echo "Creating virtual environment…"
  "$PY" -m venv .venv
fi
# Reinstall dependencies only when requirements.txt changes.
if ! cmp -s requirements.txt .venv/.installed-requirements 2>/dev/null; then
  echo "Installing dependencies…"
  .venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt
  cp requirements.txt .venv/.installed-requirements
fi

case "${1:-}" in
  setup|logout|reset)
    exec .venv/bin/python -m tgcleaner "$@" ;;
  --demo)
    shift
    exec .venv/bin/python -m tgcleaner serve --demo "$@" ;;
esac

if ! .venv/bin/python -c 'from tgcleaner.config import load_config; import sys; sys.exit(0 if load_config() else 1)'; then
  .venv/bin/python -m tgcleaner setup
fi
exec .venv/bin/python -m tgcleaner serve "$@"
