# safekeep

**Backup selettivo always-on per macOS**: osserva le cartelle sorgente con
[fswatch](https://github.com/emcrisostomo/fswatch) e copia solo i file scelti
nelle destinazioni — semantica **allow-list** e **mai cancella** nulla nel backup
(file rimosso o rinominato alla sorgente resta nel backup).

## Caratteristiche

- **Allow-list**: un file viene copiato solo se matcha almeno un `include:`;
  nessun match → non copiato. Le directory non matchate vengono attraversate,
  quelle con verdict *esclusa* vengono potate con tutto il sottoalbero.
- **Copia atomica, mai un file parziale**: tmp nella stessa cartella della
  destinazione + `fsync` + `os.replace` → un lettore vede il file vecchio o
  quello nuovo, mai una copia a metà.
- **Reconcile** (riconciliazione) all'avvio, ogni 24h, al remount e con
  `sync-once`: copia ciò che differisce per size/mtime, idempotente — eventi
  persi, crash e riavvi non contano, al prossimo reconcile tutto converge.
- **Volumi non montati**: destinazione assente → stato `pending` con backoff
  esponenziale (1s → 60s cap), poi reconcile al ritorno del mount.
- **launchd al boot**: agent con `RunAtLoad` + `KeepAlive` tiene vivo il
  processo e lo riavvia se muore.
- **`.sync` solo regole, destinazioni solo in `~/.safekeep`**: un `.sync` non
  può aggiungere né rimuovere destinazioni (una riga `dest:` è riga invalida),
  quindi un repo clonato da terzi non può far scrivere il backup altrove.

## Installazione

```bash
git clone git@gitlab.com:foxhound87/safekeep.git
cd safekeep

# unica dipendenza esterna
brew install fswatch

# copia l'esempio in ~/.safekeep, render del plist, controlli preflight
bash install.sh
```

Poi:

1. Compila `~/.safekeep` con le tue `source:` e `dest:` reali (sono placeholder
   nell'esempio — vedi [`examples/safekeep.example`](examples/safekeep.example));
2. metti un file `.sync` nella root di ogni progetto da seguire (vedi
   [`examples/sync.example`](examples/sync.example)); una directory **senza**
   `.sync` non è seguita;
3. carica l'agent:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.safekeep.agent.plist
# scarica con: launchctl bootout gui/$(id -u)/com.safekeep.agent
```

Dopo aver concesso i permessi TCC/FDA (Full Disk Access) all'interprete Python
e a `fswatch`, lancia `python3 bin/safekeep.py doctor` per la diagnostica.

## Esempio rapido

`~/.safekeep` (unico posto dove vivono le destinazioni):

```bash
# radici osservate: SOLO le directory con un .sync vengono seguite
source: ~/Code
source: ~/Projects

# destinazioni di TUTTI i progetti
dest: ~/Backup/safekeep

# relative (default): <dest>/<path relativo alla radice source>
#   ~/Code/myapp/docs/a.md → ~/Backup/safekeep/myapp/docs/a.md
layout: relative

log_level: info
```

`~/Code/myapp/.sync` (solo regole, nessuna destinazione):

```bash
name: myapp

# semantica allow-list: senza questi include non verrebbe copiato NESSUN file
include: /.env
include: /.env.*
include: .vault/
include: *.md
```

Verifica cosa verrebbe copiato, senza copiare:

```bash
python3 bin/safekeep.py sync-once --dry-run
```

## Comandi CLI

```
bin/safekeep.py <comando> [--config PATH] [-v]
```

| Comando | Cosa fa |
|---|---|
| `run` | daemon: reconcile iniziale, watch fswatch, dispatch degli eventi, timer 24h |
| `sync-once [--dry-run] [--project PATH]` | un singolo passaggio: walk sorgente e copia di ciò che differisce (con `--dry-run` stampa solo cosa copierebbe) |
| `status` | sola lettura: config, source, progetti scoperti con N regole, stato delle destinazioni |
| `doctor` | diagnostica: config, fswatch, TCC, plist launchd — exit ≠ 0 se un check fatale fallisce |

## Test

```bash
python3 -m unittest discover -s tests
```

Suite stdlib (`unittest`), nessuna dipendenza: 94 test su matcher, config,
copia atomica, volumi, daemon e CLI. Non serve `fswatch` per i test.

## Documentazione

Tutto il dettaglio (formato dei config, semantica dei pattern, flusso evento →
sync, copia atomica, launchd, edge case) è in [`SPEC.md`](SPEC.md);
esempi commentati in [`examples/`](examples/).
