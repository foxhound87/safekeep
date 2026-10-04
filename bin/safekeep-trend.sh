#!/bin/bash
# safekeep-trend — wrapper trend errori di permesso (SPEC.md §9.2).
#
# Esegue `doctor --json`, estrae timestamp / exit / count_24h / last e appende
# una riga al CSV. Header scritto una sola volta (idempotente), una riga per
# run: con frequenza oraria (launchd StartInterval 3600) sono 24 righe/giorno.
#
# Override (gli stessi meccanismi che usano i test):
#   HOME               → config (~/.safekeep) e path CSV di default
#   SAFEKEEP_TREND_CSV → path CSV (default: $HOME/.local/state/safekeep/permission-trend.csv)
#   SAFEKEEP_PYTHON    → interprete (default: python3 nel PATH)
#   --config PATH      → passato a `doctor`
#
# L'exit code di `doctor` è un DATO e finisce nel CSV: la riga viene appendata
# anche quando doctor esce 1 (un check fatale è ciò che il trend deve vedere).
# Fallisce (exit 1, messaggio su stderr) SOLO se l'output non è JSON parsabile —
# mai una riga inventata. Estrazione con python3 + json, mai grep su JSON.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="${SAFEKEEP_PYTHON:-$(command -v python3)}"
CSV="${SAFEKEEP_TREND_CSV:-$HOME/.local/state/safekeep/permission-trend.csv}"
CONFIG=()

while [ $# -gt 0 ]; do
    case "$1" in
        --config) CONFIG=(--config "${2:?--config richiede un path}"); shift 2 ;;
        -h|--help)
            echo "uso: $0 [--config PATH]   (SPEC.md §9.2)"
            exit 0 ;;
        *) echo "uso: $0 [--config PATH]" >&2; exit 2 ;;
    esac
done

# doctor esce anche con 1: il suo exit code è un campo della riga, non un errore
JSON="$("$PY" "$REPO/bin/safekeep.py" doctor --json ${CONFIG[@]+"${CONFIG[@]}"})" || true

if ! ROW="$(printf '%s' "$JSON" | "$PY" -c '
import json, sys
d = json.load(sys.stdin)
pe = d.get("permission_errors") or {}
print("{},{},{},{}".format(
    d.get("timestamp", ""),
    d.get("exit", ""),
    pe.get("count_24h", 0),
    pe.get("last") or ""))
')"; then
    echo "safekeep-trend: output di doctor non è JSON parsabile — nessuna riga scritta" >&2
    exit 1
fi

mkdir -p "$(dirname "$CSV")"
if [ ! -s "$CSV" ]; then                      # header idempotente (SPEC §9.2)
    printf 'timestamp,exit,count_24h,last\n' > "$CSV"
fi
printf '%s\n' "$ROW" >> "$CSV"
