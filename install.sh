#!/bin/bash
# safekeep — installazione idempotente dell'agent (launchd su macOS, systemd
# su Linux — SPEC.md §14.3). Su Linux SENZA systemd (WSL1, WSL2 con systemd
# spento, container) non installa la unit e esce 0 con le istruzioni di avvio
# manuale (SPEC.md §17.3). NON avvia il job (bootstrap / enable --now va
# fatto DOPO aver configurato ~/.safekeep con dest reali).
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
# Interprete renderizzato nel plist (SPEC.md §8.4): lo shim di sistema
# /usr/bin/python3 non viene mai cancellato da un `brew upgrade` — un path
# Cellar sparisce sotto il processo vivo e TCC non lo identifica più (EPERM).
PY="${SAFEKEEP_PYTHON:-/usr/bin/python3}"
OS="$(uname -s)"

# ≥ 3.9 e realmente eseguibile: senza CLT (Command Line Tools) lo shim di
# sistema non risponde → cade su python3 nel PATH (comportamento pre-0.5.1)
py_ok() {
    [ -n "${1:-}" ] && [ -x "$1" ] &&
        "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
            >/dev/null 2>&1
}

# --trend (SPEC.md §9.2): render + bootstrap SOLO dell'agent orario del trend.
# Non tocca, non ricarica la unit dell'agente principale.
TREND=0
for arg in "$@"; do
    case "$arg" in
        --trend) TREND=1 ;;
        -h|--help)
            echo "uso: $0 [--trend]   (--trend = installa solo l'agent trend orario, SPEC §9.2)"
            exit 0 ;;
        *) echo "uso: $0 [--trend]" >&2; exit 2 ;;
    esac
done

# --- preflight: fswatch + python3 ---------------------------------------------
if ! command -v fswatch >/dev/null 2>&1; then
    case "$OS" in
        Darwin) HINT="brew install fswatch (https://brew.sh se manca Homebrew)" ;;
        Linux)
            if command -v pacman >/dev/null 2>&1; then
                HINT="pacman -S fswatch"
            elif command -v apt-get >/dev/null 2>&1; then
                HINT="sudo apt-get install fswatch"
            elif command -v dnf >/dev/null 2>&1; then
                HINT="sudo dnf install fswatch"
            else
                HINT="installa fswatch col package manager della tua distro"
            fi ;;
        *) echo "✗ OS non supportato: $OS (servono macOS o Linux)" >&2; exit 1 ;;
    esac
    echo "✗ fswatch mancante — installalo: $HINT" >&2
    exit 1
fi
if ! py_ok "$PY"; then
    PY="$(command -v python3 || true)"
fi
if ! py_ok "$PY"; then
    echo "✗ python3 ≥ 3.9 non trovato (override: SAFEKEEP_PYTHON=/percorso/python3)" >&2
    exit 1
fi

# --- directory di stato + config globale -------------------------------------
mkdir -p "$HOME/.local/state/safekeep"
if [ ! -f "$HOME/.safekeep" ]; then
    cp "$REPO/examples/safekeep.example" "$HOME/.safekeep"
    echo "→ creata $HOME/.safekeep (da examples/safekeep.example)"
fi

# --- agent trend (SPEC §9.2): render + bootstrap, daemon principale intatto ---
if [ "$TREND" = 1 ]; then
    if [ "$OS" != Darwin ]; then
        echo "✗ --trend: agent launchd solo su macOS (su Linux usa cron o un systemd-timer)" >&2
        exit 1
    fi
    TPL="$REPO/launchd/com.safekeep.trend.plist"
    AGENT="$HOME/Library/LaunchAgents/com.safekeep.trend.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    sed -e "s|__REPO__|$REPO|g" \
        -e "s|__PYTHON__|$PY|g" \
        -e "s|__SCRIPT__|$REPO/bin/safekeep-trend.sh|g" \
        -e "s|__HOME__|$HOME|g" \
        "$TPL" > "$AGENT"
    if command -v plutil >/dev/null 2>&1; then
        plutil -lint "$AGENT" >/dev/null
    fi
    echo "→ plist trend installato: $AGENT"
    # idempotente: se il job è già caricato, scarica SOLO quello (bootout del
    # singolo label) e lo ricarica — com.safekeep.agent non viene toccato
    if launchctl print "gui/$(id -u)/com.safekeep.trend" >/dev/null 2>&1; then
        launchctl bootout "gui/$(id -u)/com.safekeep.trend" 2>/dev/null || true
    fi
    launchctl bootstrap "gui/$(id -u)" "$AGENT"
    echo "→ trend agent caricato (orario, StartInterval 3600)"
    echo "   prima riga tra 1h, oppure forzala:"
    echo "   launchctl kickstart gui/\$(id -u)/com.safekeep.trend"
    echo "   CSV: ${SAFEKEEP_TREND_CSV:-$HOME/.local/state/safekeep/permission-trend.csv}"
    echo "   log: $HOME/.local/state/safekeep/trend.log"
    exit 0
fi

# --- render dell'agent (placeholder → path reali) ----------------------------
case "$OS" in
    Darwin)
        TPL="$REPO/launchd/com.safekeep.agent.plist"
        AGENT="$HOME/Library/LaunchAgents/com.safekeep.agent.plist"
        mkdir -p "$HOME/Library/LaunchAgents"
        sed -e "s|__REPO__|$REPO|g" \
            -e "s|__PYTHON__|$PY|g" \
            -e "s|__HOME__|$HOME|g" \
            "$TPL" > "$AGENT"
        if command -v plutil >/dev/null 2>&1; then
            plutil -lint "$AGENT" >/dev/null
        fi
        echo "→ plist installato: $AGENT"
        ;;
    Linux)
        # Niente systemd vivo (WSL1, WSL2 con systemd spento, container):
        # nessuna unit, istruzioni di avvio manuale ed EXIT 0 — degradazione
        # graziosa, non un errore (SPEC.md §17.3)
        if [ ! -d /run/systemd/system ]; then
            echo "⚠ systemd non disponibile: unit systemd NON installata"
            echo "   → avvio manuale del daemon: $REPO/bin/safekeep.py run"
            echo "   → config già pronta: $HOME/.safekeep"
            echo "   → con systemd (WSL2) aggiungi a /etc/wsl.conf:"
            echo "       [boot]"
            echo "       systemd=true"
            echo "     poi riavvia WSL (wsl --shutdown) e rilancia install.sh"
            exit 0
        fi
        TPL="$REPO/systemd/safekeep.service"
        AGENT="$HOME/.config/systemd/user/safekeep.service"
        mkdir -p "$HOME/.config/systemd/user"
        sed -e "s|__REPO__|$REPO|g" \
            -e "s|__PYTHON__|$PY|g" \
            "$TPL" > "$AGENT"
        echo "→ unit systemd installata: $AGENT"
        if command -v systemctl >/dev/null 2>&1 && systemctl --user daemon-reload; then
            echo "→ systemd: daemon-reload eseguito"
        else
            echo "⚠ systemd user session non disponibile — esegui a mano:" \
                 "systemctl --user daemon-reload" >&2
        fi
        ;;
    *) echo "✗ OS non supportato: $OS (servono macOS o Linux)" >&2; exit 1 ;;
esac

cat <<EOF

installato. Prossimi passi:
  1. Configura source/dest reali in ~/.safekeep (ora è una copia dell'esempio)
  2. Diagnostica:   $REPO/bin/safekeep.py doctor
EOF

case "$OS" in
    Darwin)
        cat <<EOF
  3. Carica l'agent: launchctl bootstrap gui/\$(id -u) $AGENT
     (scarica con: launchctl bootout gui/\$(id -u)/com.safekeep.agent)
EOF
        ;;
    Linux)
        cat <<EOF
  3. Avvia l'agent: systemctl --user enable --now safekeep
     (stato: systemctl --user status safekeep — log: journalctl --user -u safekeep -f)
  4. Boot senza login (opzionale): loginctl enable-linger \$USER
     (senza linger la unit parte solo dopo il primo login, SPEC.md §14.3)
EOF
        ;;
esac
