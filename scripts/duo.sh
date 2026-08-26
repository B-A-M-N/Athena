#!/usr/bin/env bash
# athena duo — side-by-side: calm surface (left) | OI live stream (right).
# Both are read-only projections of the same canonical event log.
set -euo pipefail

SESSION="${ATHENA_DUO_SESSION:-athena}"
DB="${ATHENA_DB:-$PWD/athena.db}"
PY="${PYTHON:-python3}"

if tmux has-session -t "$SESSION" 2>/dev/null; then
    exec tmux attach -t "$SESSION"
fi

tmux new-session -d -s "$SESSION" -n athena \
    "$PY -m athena.cli.duo_left" \; \
    split-window -h -t "$SESSION:0" \
    "ATHENA_DB='$DB' $PY -m athena.cli.oi_stream" \; \
    resize-pane -t "$SESSION:0.1" -x 45% \; \
    select-pane -t "$SESSION:0.0"

exec tmux attach -t "$SESSION"
