#!/usr/bin/env bash
# E2E SMB/CIFS live (SPEC §20) — the gate for network volumes, the way
# e2e_linux.sh is the gate for the fswatch path: every unittest drives a
# fake fs, this one drives a REAL CIFS mount (smbd + mount.cifs).
#
# Phases (SPEC §20.5/§20.2):
#   1. DEST on CIFS: sync-once copies onto the mount, second run is
#      idempotent (0 copie), no `*.safekeep.tmp.*` residue, `status`
#      labels the dest `[nas]` (fstype detect, §20.6);
#   2. SOURCE on CIFS with `poll_interval: 5`: a change written on the
#      mount reaches the LOCAL dest within ~20s — the true gate for the
#      poll monitor; a change on the local source root reaches the same
#      dest via fswatch in the SAME daemon (hybrid poll+fswatch loop);
#   3. `.sync` exclusions hold under poll: `!draft.md` and a non-`*.md`
#      file written on the mount stay out of dest;
#   4. umount -> edit via the share dir -> remount: `poll_pending` with
#      backoff, no crash, catch-up copies everything missed (§20.2);
#      (skipped in --mounted mode: remounting is a manual step there)
#   5. clean SIGTERM (exit 0), no orphan fswatch, no tmp residue.
#
# Modes:
#   --server   (default, CI): starts a private guest smbd on 127.0.0.1
#              with a sandbox smb.conf and `sudo mount.cifs //127.0.0.1/…`
#              (vers=3.0, fallback vers=2.1);
#   --mounted  (manual rig, e.g. macOS): use the EXISTING writable SMB
#              mount in $SAFEKEEP_E2E_SMB_MOUNT (e.g. /Volumes/skshare);
#              phase 4 is skipped there.
#
# Gating (SPEC §14.6): without SAFEKEEP_E2E_SMB=1 the script prints a SKIP
# message and exits 0 — the real gate runs in GitHub Actions (3.12 leg
# only), where mount.cifs is possible; the GitLab docker executor has no
# SYS_ADMIN and cannot mount, so GitLab deliberately does not run this.
#
# Sandbox: everything (share dir, mountpoint, source, dest, config, HOME)
# lives in a `mktemp -d` removed on exit; the daemon runs with HOME pointed
# at it, so logs and doctor probes never touch the real ~/.safekeep.
set -euo pipefail

if [[ "${SAFEKEEP_E2E_SMB:-}" != "1" ]]; then
  echo "==> SKIP: SAFEKEEP_E2E_SMB=1 non impostato — e2e SMB saltato (gate CI-only, SPEC §14.6)"
  exit 0
fi

MODE="${1:---server}"
REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SANDBOX="$(mktemp -d)"
SHARE="$SANDBOX/share"          # directory servita da smbd
MNT="$SANDBOX/mnt"              # mountpoint CIFS
SRCLOCAL="$SANDBOX/localsrc"    # radice LOCALE per fswatch (loop ibrido)
SRCNET="$MNT/srcproj"           # radice su CIFS per il polling
DESTLOCAL="$SANDBOX/destlocal"  # dest locale
DESTCIFS="$MNT/dest"            # dest su CIFS (fase 1)
CFG="$SANDBOX/safekeep.conf"
SMBCFG="$SANDBOX/smb.conf"
export HOME="$SANDBOX/home"

DAEMON_PID=""
FSWATCH_PID=""
SMBD_PID=""
STATUS=0

fail() { echo "FAIL: $*" >&2; STATUS=1; }
note() { echo "==> $*"; }

cleanup() {
  if [[ -n "$DAEMON_PID" ]] && kill -0 "$DAEMON_PID" 2>/dev/null; then
    kill -TERM "$DAEMON_PID" 2>/dev/null || true
    wait "$DAEMON_PID" 2>/dev/null || true
  fi
  if [[ "$MODE" == "--server" ]]; then
    sudo umount "$MNT" 2>/dev/null || true
    [[ -n "$SMBD_PID" ]] && kill "$SMBD_PID" 2>/dev/null || true
  fi
  rm -rf "$SANDBOX"
}
trap cleanup EXIT

wait_for() {
  local deadline=$((SECONDS + $1))
  shift
  while ((SECONDS < deadline)); do
    if "$@"; then return 0; fi
    sleep 1
  done
  "$@"
}

fswatch_ours() { pgrep -P "$DAEMON_PID" -x fswatch >/dev/null 2>&1; }
fswatch_ours_dead() { ! kill -0 "$FSWATCH_PID" 2>/dev/null; }
daemon_alive() { kill -0 "$DAEMON_PID" 2>/dev/null; }

mkdir -p "$SHARE" "$MNT" "$SRCLOCAL" "$HOME"

# ---------------------------------------------------------------- mount ----
if [[ "$MODE" == "--server" ]]; then
  note "smbd guest su 127.0.0.1 + mount.cifs (sandbox: $SANDBOX)"
  cat >"$SMBCFG" <<EOF
[global]
   map to guest = Bad User
   server role = standalone server
   bind interfaces only = yes
   interfaces = 127.0.0.1
   load printers = no
   printcap name = /dev/null
   disable spoolss = yes
   state directory = $SANDBOX/samba-state
   cache directory = $SANDBOX/samba-state
   pid directory = $SANDBOX/samba-state
   private dir = $SANDBOX/samba-state
   lock directory = $SANDBOX/samba-state
   ncalrpc dir = $SANDBOX/samba-state/ncalrpc
   log file = $SANDBOX/samba-state/log.%m
   max log size = 0
   usershare max shares = 0
[sk]
   path = $SHARE
   guest ok = yes
   read only = no
EOF
  mkdir -p "$SANDBOX/samba-state/ncalrpc" "$SHARE" "$MNT"
  # la share va a 777: smbd serve la root come uid guest (nobody) e safekeep
  # deve poter creare/scrivere file e directory nella radice della share
  chmod 777 "$SANDBOX" "$SHARE" || true
  # setsid: smbd in una sessione propria — altrimenti condivide il pgid della
  # shell e il suo shutdown manda segnali all'intero gruppo (e2e muore con 143)
  setsid smbd --foreground --no-process-group --configfile="$SMBCFG" &
  SMBD_PID=$!
  if ! wait_for 10 bash -c 'exec 3<>/dev/tcp/127.0.0.1/445'; then
    fail "smbd non in ascolto su 127.0.0.1:445"
    exit "$STATUS"
  fi
  if ! sudo mount.cifs "//127.0.0.1/sk" "$MNT" -o guest,vers=3.0,file_mode=0777,dir_mode=0777 2>/dev/null; then
    note "vers=3.0 rifiutato, riprovo con vers=2.1"
    if ! sudo mount.cifs "//127.0.0.1/sk" "$MNT" -o guest,vers=2.1,file_mode=0777,dir_mode=0777; then
      fail "mount.cifs fallito con entrambe le versioni"
      exit "$STATUS"
    fi
  fi
elif [[ "$MODE" == "--mounted" ]]; then
  if [[ -z "${SAFEKEEP_E2E_SMB_MOUNT:-}" ]]; then
    echo "FAIL: --mounted richiede SAFEKEEP_E2E_SMB_MOUNT=/Volumes/TUO_SHARE" >&2
    exit 2
  fi
  MNT="$(cd "$SAFEKEEP_E2E_SMB_MOUNT" && pwd)"
  SHARE="$MNT"                # le modifiche "lato server" passano dal mount stesso
  note "uso il mount esistente $MNT (--mounted, fase smont/remount saltata)"
else
  echo "FAIL: modalità sconosciuta: $MODE (uso: --server | --mounted)" >&2
  exit 2
fi

# --------------------------------------------------- fase 1: dest su CIFS ----
printf 'include: *.md\n' >"$SRCLOCAL/.sync"
printf 'local v1\n' >"$SRCLOCAL/local.md"
mkdir -p "$SHARE/destcifs" && chmod 777 "$SHARE/destcifs"
DESTCIFS="$MNT/destcifs"    # dir creata lato server (share 777): montata è 777
CFG_DEST="$SANDBOX/dest-cifs.conf"
printf 'source: %s\ndest: %s\n' "$SRCLOCAL" "$DESTCIFS" >"$CFG_DEST"

note "fase 1: dest su CIFS — sync-once + idempotenza + status [nas]"
out="$(python3 "$REPO/bin/safekeep.py" sync-once --config "$CFG_DEST")" \
  || fail "sync-once verso dest CIFS uscito != 0: $out"
if ! wait_for 5 test -f "$DESTCIFS/local.md"; then
  fail "local.md non è mai arrivato sulla dest CIFS"
fi
if ! grep -q "dest *$DESTCIFS \[ok\] \[nas\]" \
    <(python3 "$REPO/bin/safekeep.py" status --config "$CFG_DEST"); then
  fail "status non etichetta la dest CIFS come [nas]"
fi
out="$(python3 "$REPO/bin/safekeep.py" sync-once --config "$CFG_DEST")" \
  || fail "secondo sync-once uscito != 0"
grep -q '0 copie' <<<"$out" || fail "secondo sync-once non idempotente: $out"
residue="$(find "$DESTCIFS" -name '*.safekeep.tmp.*' 2>/dev/null || true)"
[[ -z "$residue" ]] || fail "residui tmp sulla dest CIFS: $residue"

# ------------------------------------- fase 2-5: sorgente su CIFS + loop ----
# ogni dir CIFS è creata lato $SHARE (filesystem reale, prima o indipendente dal
# mount) con chmod 777: creata dal client montato sarebbe `nobody:755` e
# safekeep non potrebbe scriverci dentro
mkdir -p "$SHARE/srcproj" && chmod 777 "$SHARE/srcproj"
printf 'include: *.md\n!draft.md\n' >"$SHARE/srcproj/.sync"
printf 'nas v1\n' >"$SHARE/srcproj/note.md"
# i file CIFS serviti dal guest finiscono `nobody:644` lato server: safekeep
# deve poterli RISCRIVERE dal mount (i test di fase li riscrivono), quindi 777
chmod 777 "$SHARE/srcproj/.sync" "$SHARE/srcproj/note.md"
printf 'local v1\n' >"$SRCLOCAL/local.md"
mkdir -p "$SHARE/destlocal" && chmod 777 "$SHARE/destlocal"
DESTLOCAL="$MNT/destlocal"   # come DESTCIFS: creata lato server, montata 777
printf 'source: %s\nsource: %s\ndest: %s\npoll_interval: 5\n' \
  "$SRCNET" "$SRCLOCAL" "$DESTLOCAL" >"$CFG"

note "fase 2: daemon ibrido — poll sul CIFS + fswatch sulla root locale"
python3 "$REPO/bin/safekeep.py" run --config "$CFG" &
DAEMON_PID=$!
if ! wait_for 10 fswatch_ours; then
  fail "fswatch non spawnato nonostante la root locale (daemon alive: $(daemon_alive && echo yes || echo no))"
  exit "$STATUS"
fi
FSWATCH_PID="$(pgrep -P "$DAEMON_PID" -x fswatch | head -n1)" || true

# baseline: il reconcile iniziale copia tutto entro pochi secondi
# (layout relative: il path dest è relativo alla radice `source`, §4.3)
if ! wait_for 15 test -f "$DESTLOCAL/note.md"; then
  fail "reconcile iniziale: note.md (root su CIFS) non è arrivato su dest locale"
fi

# il gate VERO: modifica scritta sul MOUNT (nessun evento possibile) →
# il poll la deve portare a dest entro ~20s (intervallo 5s + margine)
printf 'nas v2-piu-lungo\n' >"$SRCNET/note.md"
if ! wait_for 20 grep -q 'nas v2-piu-lungo' "$DESTLOCAL/note.md" 2>/dev/null; then
  fail "modifica sul mount CIFS non è arrivata a dest in 20s (polling rotto?)"
fi

# e la stessa root deve seguire anche un file NUOVO sul mount
printf 'nuovo\n' >"$SRCNET/nuovo.md"
if ! wait_for 20 test -f "$DESTLOCAL/nuovo.md"; then
  fail "file nuovo sul mount CIFS non è arrivato a dest in 20s"
fi

# e la root locale deve ancora passare da fswatch (loop ibrido)
printf 'local v2\n' >"$SRCLOCAL/local.md"
if ! wait_for 15 grep -q 'local v2' "$DESTLOCAL/local.md" 2>/dev/null; then
  fail "la root locale non passa più da fswatch"
fi

note "fase 3: esclusioni .sync sotto polling (draft.md, skip.txt)"
printf 'x\n' >"$SRCNET/draft.md" 2>/dev/null || printf 'x\n' >"$SHARE/srcproj/draft.md"
printf 'x\n' >"$SRCNET/skip.txt" 2>/dev/null || printf 'x\n' >"$SHARE/srcproj/skip.txt"
chmod 777 "$SHARE/srcproj/draft.md" "$SHARE/srcproj/skip.txt" 2>/dev/null || true
sleep 12                               # ≥ 2 turni di poll + margine
[[ ! -e "$DESTLOCAL/draft.md" ]] \
  || fail "draft.md copiato benché .sync lo escluda"
[[ ! -e "$DESTLOCAL/skip.txt" ]] \
  || fail "skip.txt copiato benché *.md non lo matchi"

if [[ "$MODE" == "--server" ]]; then
  note "fase 4: umount → modifiche via share → remount → catch-up (pending, no crash)"
  if ! wait_for 10 sudo umount "$MNT"; then
    fail "umount del mountpoint CIFS fallito (target is busy persistente)"
  fi
  sleep 1                               # qualche walk fallisce → poll_pending + backoff
  printf 'nas v3-durante-smont\n' >"$SHARE/srcproj/note.md"
  printf 'perso\n' >"$SHARE/srcproj/durante.md"
  if ! sudo mount.cifs "//127.0.0.1/sk" "$MNT" -o guest,vers=3.0,file_mode=0777,dir_mode=0777 2>/dev/null \
     && ! sudo mount.cifs "//127.0.0.1/sk" "$MNT" -o guest,vers=2.1,file_mode=0777,dir_mode=0777; then
    fail "remount fallito"
  fi
  if ! wait_for 45 grep -q 'nas v3-durante-smont' "$DESTLOCAL/note.md" 2>/dev/null; then
    fail "catch-up dopo remount non avvenuto in 45s"
  fi
  if ! wait_for 5 test -f "$DESTLOCAL/durante.md"; then
    fail "file creato while smontato non raggiunto al remount (catch-up)"
  fi
  daemon_alive || fail "il daemon è morto durante lo smont/remount (poll_pending deve confinare l'errore)"
else
  note "fase 4: saltata in modalità --mounted (remount manuale)"
fi

note "fase 5: SIGTERM pulito, nessun fswatch orfano, nessun residuo tmp"
kill -TERM "$DAEMON_PID"
rc=0
wait "$DAEMON_PID" || rc=$?
DAEMON_PID=""
if [[ $rc -ne 0 ]]; then
  fail "daemon uscito con $rc (atteso 0)"
fi
if [[ -n "$FSWATCH_PID" ]] && ! wait_for 5 fswatch_ours_dead; then
  fail "fswatch (pid $FSWATCH_PID) sopravvissuto al daemon"
fi
residue="$(find "$DESTLOCAL" "$SHARE" -name '*.safekeep.tmp.*' 2>/dev/null || true)"
[[ -z "$residue" ]] || fail "residui tmp: $residue"

if [[ $STATUS -eq 0 ]]; then
  note "E2E SMB OK"
fi
exit "$STATUS"
