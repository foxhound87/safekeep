#!/bin/bash
# safekeep — installazione idempotente dell'agent (launchd su macOS, systemd
# su Linux — SPEC.md §14.3). Su Linux SENZA systemd (WSL1, WSL2 con systemd
# spento, container) non installa la unit e esce 0 con le istruzioni di avvio
# manuale (SPEC.md §17.3). NON avvia il job (bootstrap / enable --now va
# fatto DOPO aver configurato ~/.safekeep con dest reali).
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
PY="${SAFEKEEP_PYTHON:-/opt/homebrew/bin/python3}"
OS="$(uname -s)"

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
if [ ! -x "$PY" ]; then
    PY="$(command -v python3 || true)"
fi
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
    echo "✗ python3 non trovato (servono Python ≥ 3.9)" >&2
    exit 1
fi

# --- directory di stato + config globale -------------------------------------
mkdir -p "$HOME/.local/state/safekeep"
if [ ! -f "$HOME/.safekeep" ]; then
    cp "$REPO/examples/safekeep.example" "$HOME/.safekeep"
    echo "→ creata $HOME/.safekeep (da examples/safekeep.example)"
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
