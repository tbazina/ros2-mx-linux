#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${BASH_SOURCE[0]} != "$0" ]]; then
    printf '%s\n' 'Execute this script; do not source it.' >&2
    return 1
fi
if [[ -v WS ]]; then
    printf '%s\n' 'WS is retired: use --root for candidates or baseline --workspace for read-only capture.' >&2
    exit 2
fi
if [[ -v SKIP_KEYS ]]; then
    printf '%s\n' 'SKIP_KEYS is retired: use --profile with an explicit skip_keys mapping ({} means no skips).' >&2
    exit 2
fi
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PIPELINE_HOME="${HOME:-$(/usr/bin/python3 -c 'import os, pwd; print(pwd.getpwuid(os.getuid()).pw_dir)')}"
exec env -i HOME="$PIPELINE_HOME" USER="${USER:-}" TERM="${TERM:-dumb}" \
    PATH=/usr/bin:/bin LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 GIT_OPTIONAL_LOCKS=0 \
    /usr/bin/python3 -B "$SCRIPT_DIR/lib/pipeline.py" "$@"
