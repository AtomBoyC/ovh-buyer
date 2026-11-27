#!/bin/sh
set -e

DATA_DIR="${OVH_DATA_DIR:-/app/data}"

# Allow direct shell or python execution for debugging / maintenance
case "${1:-}" in
  sh|/bin/sh|ash|/bin/ash|python|python3)
    exec "$@"
    ;;
esac

if [ "$(id -u)" = "0" ]; then
  TARGET_UID="${PUID:-1000}"
  TARGET_GID="${PGID:-1000}"

  mkdir -p "$DATA_DIR" "$DATA_DIR/cache" 2>/dev/null || true
  chown -R "$TARGET_UID:$TARGET_GID" "$DATA_DIR" 2>/dev/null || true

  PROBE_FILE="$DATA_DIR/.ovh_write_probe.$$"
  if su-exec "$TARGET_UID:$TARGET_GID" sh -c '(: > "$1") && rm -f "$1"' -- "$PROBE_FILE" 2>/dev/null; then
    exec su-exec "$TARGET_UID:$TARGET_GID" python3 /app/run.py "$@"
  else
    rm -f "$PROBE_FILE" 2>/dev/null || true
    echo "[OVH Sniper] Warning: $DATA_DIR is not writable by UID:GID $TARGET_UID:$TARGET_GID; running as root fallback." >&2
    exec python3 /app/run.py "$@"
  fi
fi

mkdir -p "$DATA_DIR" "$DATA_DIR/cache" 2>/dev/null || true
exec python3 /app/run.py "$@"
