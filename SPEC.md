# SPEC.md — safekeep

**Versione:** 0.2.0 (semver — Semantic Versioning, https://semver.org)
**Stato:** pre-implementazione
**Piattaforma target:** macOS (FSEvents, launchd)

---

## 1. Panoramica e obiettivo

`safekeep` è un motore di backup **event-driven** per macOS: osserva le cartelle sorgente con
**fswatch** (utility installabile via Homebrew) e copia i file modificati su una o più destinazioni,
senza mai cancellare nulla nel backup.

Obiettivi, in ordine di priorità:

1. **Nessuna perdita di dati nel backup.** La semantica è *solo copia/aggiorna, mai cancellare*:
   un file rimosso o rinominato alla sorgente **resta nel backup** (storicizzazione di fatto).
2. **Semplicità operativa.** Unico script Python 3 (solo stdlib), zero dipendenze esterne oltre a
   `fswatch` e al sistema di avvio `launchd` (Launch Daemon/Agent di macOS).
3. **Configurazione minimale e prevedibile.** Un file globale `~/.safekeep` (sorgenti,
   destinazioni, layout, defaults) più, opzionalmente, un file `.sync` per progetto con
   sintassi stile gitignore e **solo regole** di inclusione/esclusione.
4. **Robustezza.** Copia atomica, reconcile (riconciliazione) periodico, gestione dei volumi non
   montati, resistenza agli event storm (migliaia di modifiche in pochi secondi).

Non-obiettivi (esplicitamente fuori scope di 0.1.0):

- compressione, deduplicazione, cifratura a livello di backup;
- sync bidirezionale o mirror con cancellazioni;
- retention/pruning delle copie storiche;
- daemon multi-utente o servizio installato come LaunchDaemon di sistema.

---

## 2. Architettura

```
                        ┌──────────────────────────────────────────────┐
                        │              launchd (plist)                 │
                        │   RunAtLoad + KeepAlive — avvio al boot      │
                        │   opzionale StartOnMount (vedi §8.3)         │
                        └───────────────────┬──────────────────────────┘
                                            │ avvia / riavvia
                                            ▼
   ~/.safekeep                             ┌──────────────────────────────────┐
   (globale: source, dest, ──────────────► │  bin/safekeep.py                 │
    layout, defaults,                      │  CLI: run | sync-once            │
    log_level)                             │        status | doctor           │
                                           └───────┬──────────┬───────────────┘
   <progetto>/.sync                                │          │
   (name, defaults,  ──────────────────────────────┘          │ subprocess
    include/exclude last-match-wins)                          ▼
                                           ┌──────────────────────────────────┐
                                           │ fswatch -0 -m fsevents_monitor   │
                                           │ -r -l 1.0 -e <regex> -- <roots>  │
                                           └───────────────┬──────────────────┘
                                                           │ eventi path\0
                                                           ▼
                                           ┌──────────────────────────────────┐
                                           │  Dispatcher per progetto          │
                                           │  - matcher gitignore-like         │
                                           │  - stability check                │
                                           │  - copia atomica su N destinazioni│
                                           │  - >5000 path → reconcile         │
                                           └───────────────┬──────────────────┘
                                                           │ mount / ogni 24h / boot
                                                           ▼
                                           ┌──────────────────────────────────┐
                                           │  Reconcile (walk sorgente,        │
                                           │  copia ciò che differisce per     │
                                           │  mtime/size)                      │
                                           └──────────────────────────────────┘
```

### Componenti

| Componente | Dove vive | Responsabilità |
|---|---|---|
| Config globale | `~/.safekeep` | **unico** posto delle destinazioni, layout, defaults, log |
| Config di progetto | `<progetto>/.sync` | nome progetto, defaults override, regole include/exclude |
| Dispatcher | `bin/safekeep.py run` | avvia fswatch, riceve eventi, pianifica le copie |
| Sync one-shot | `bin/safekeep.py sync-once` | walk completo o di un progetto: copia ciò che differisce |
| Reconcile | dentro `run`/`sync-once` | walk di riconciliazione (boot, mount, ogni 24h) |
| Matcher | modulo nello stesso script | pattern gitignore-like, last-match-wins, dir pruning |
| Copia atomica | modulo nello stesso script | stability check, tmp + fsync + `os.replace` |
| launchd plist | `~/Library/LaunchAgents/com.safekeep.agent.plist` | tenere vivo il processo, avvio al boot |
| CLI diagnostica | `status` / `doctor` | salute, TCC (Transparency, Consent and Control), validazione config |

### Vincoli architetturali

1. **Un solo processo Python** (più il figlio `fswatch`): nessun journal persistente.
   La copia è idempotente (stesso contenuto → nessuna scrittura), quindi lo stato si ricostruisce
   con la riconciliazione, non con un log di operazioni.
2. **Nessuna cancellazione**: né di file nel backup, né (in 0.1.0) di file temporanei oltre ai
   residui di crash della stessa run.
3. **Idempotenza come fondamento**: eventi persi, crash, riavvi non importano — al prossimo
   reconcile tutto converge.

---

## 3. Formato del file globale `~/.safekeep`

Percorso: `~/.safekeep` — **file singolo nella `$HOME`** (niente
`~/.config/safekeep/config`): è l'unico posto dove vivono le destinazioni.
Sintassi: `chiave: valore` (una per riga), commenti `#`, blank lines ignorati.

| Chiave | Tipo | Default | Descrizione |
|---|---|---|---|
| `source` | path (ripetibile) | **opzionale** | radici da osservare; ogni radice è una "sorgente"; assente → **modalità auto-discovery** da `$HOME` (vedi §6); dir senza `.sync` = **non seguita** (vedi §4.1) |
| `dest` | path (ripetibile) | **obbligatoria** | destinazioni di **tutti** i progetti: le uniche esistenti (i `.sync` non possono aggiungerne o rimuoverne, vedi §4.2 e §11) |
| `layout` | `relative` \| `full` | `relative` | layout dei path di destinazione (vedi nota sotto) |
| `defaults` | lista regole | builtin | regole di default a **bassa priorità** (sotto le regole globali e sotto `.sync`) |
| `log_level` | `debug`\|`info`\|`warn`\|`error` | `info` | livello di log su stderr + file di log |

Note:

- `source` e `dest` sono **ripetibili**: più righe = più valori.
- **Due modalità**, decise dalla presenza di `source`:
  - **modalità source** (una o più righe `source:`): le radici watch sono le
    `source` e il comportamento è quello storico (backward compat);
  - **auto-discovery** (nessuna riga `source:`): watch root = `$HOME`, scan di
    discovery alla ricerca dei `.sync`, `.sync` creato dopo l'avvio agganciato
    da solo (vedi §6). Una config senza `source` **non** è un errore.
- **`dest` è obbligatoria**: nessuna riga `dest:` → `ConfigError` con hint
  (le destinazioni vivono solo qui, §4.2). Nessuna `source` invece non è un errore.
- `defaults` accetta regole nella stessa sintassi di `include:`/`exclude:` di `.sync`,
  una per riga, oppure come righe multiple con la stessa chiave.
- Questo file è **fail-fast**: chiave ignota o riga invalida → `ConfigError` con numero di
  riga (in un `.sync`, che è un file altrui, la riga invalida viene invece solo warningata
  e saltata — vedi §4.2).
- **Layout dei path di destinazione** (`dest_path`, implementato in `safekeep/config.py`):
  - `relative` (default): **`<dest>/<path relativo alla radice `source` che contiene il
    progetto>`** — la radice `source` del config, **non** la root del progetto:
    `~/Code/myapp/docs/a.md` (source `~/Code`) → `<dest>/myapp/docs/a.md`. Il segmento
    del progetto resta nel path, quindi due progetti con lo stesso file relativo non si
    sovrappongono: `src/projA/docs/note.md` → `<dest>/projA/docs/note.md`,
    `src/projB/docs/note.md` → `<dest>/projB/docs/note.md`.
    In **modalità auto-discovery** la radice di riferimento è `$HOME`, così il path dest
    tiene il segmento sotto la home (`Code/myapp/docs/a.md`) e i progetti non si
    sovrappongono.
  - `full`: `<dest>/<path assoluto senza / iniziale>`:
    `~/Code/myapp/docs/a.md` → `<dest>/Users/<user>/Code/myapp/docs/a.md`.

Esempio commentato completo: [`examples/safekeep.example`](examples/safekeep.example).

---

## 4. Formato `.sync` per progetto

### 4.1 Scoperta

- Un progetto è **seguito** se contiene un file `.sync` nella sua root.
- Le sorgenti sono le `source` globali; per ciascuna radice viene cercato un `.sync`.
  In **modalità auto-discovery** (nessuna `source`, §3 e §6) la ricerca parte da `$HOME`
  con pruning dedicato: si saltano le directory che iniziano con `.` e `Library`,
  `.Trash`, `.cache`, `node_modules`, `.git`, `.venv`, `__pycache__`, `venv`,
  `Applications` (rami enormi tipo QtWebEngine, un `.sync` lì non ha senso) —
  un `.sync` dentro una di queste **non** diventa progetto.
  Saltano anche le cartelle protette dalla privacy di macOS (TCC = Transparency,
  Consent and Control: `Desktop`, `Documents`, `Downloads`, `Movies`, `Music`,
  `Pictures`, `Public`): senza il permesso "Accesso completo al disco" (Full Disk
  Access, FDA) l'`opendir()` su una di queste resta bloccato nel kernel per un
  agent launchd e il discovery non finirebbe mai. L'esclusione è applicata ai
  figli *prima* di scendere, quindi queste cartelle non vengono nemmeno aperte.
  **Directory senza `.sync` non sono seguite** (nessuna copia, nessun watch esplicito sulle
  sottocartelle oltre al normale recursive di fswatch filtrato dal matcher).
- Un `.sync` trovato più in profondità (sotto-progetto) è rispettato come unità con le sue
  regole (le destinazioni restano sempre le globali).
- `.sync` **viene sempre copiato** nel backup (serve per ricostruire la config sul destino):
  è una regola implicita `include: .sync` nel layer più basso del matcher (§4.3), quindi
  vale anche con la semantica allow-list.

### 4.2 Chiavi di sezione

| Chiave | Significato |
|---|---|
| `name: <nome>` | nome leggibile del progetto (usato nei log e in `status`) |
| `defaults: <regola>` | regole di default per questo progetto (priorità sopra quelle globali, sotto le regole del file) |

**Righe nude stile gitignore.** Una riga che non è vuota, non è commento e non ha la forma di
una chiave è una **regola `include:`**; una riga che inizia con `!` è una **regola `exclude:`**
(il `!` va tolto dal pattern):

```
.env            #  ≡  include: .env
*.md            #  ≡  include: *.md
!docs/vendor/   #  ≡  exclude: docs/vendor/
```

Le righe nude entrano nella **stessa lista ordinata** di `include:`/`exclude:` esplicite
(un solo elenco, last-match-wins in base alla posizione nel file).

> ⚠️ **È l'inverso di gitignore**: qui la riga elenca i file da **COPIARE**, non quelli da
> ignorare. In un `.gitignore` scriveresti `*.md` per *escludere* i markdown, qui `*.md`
> li *include*. Il `!` vale in entrambi, ma con il segno opposto: in `.gitignore` `!x`
> re-include, qui `!x` esclude.

**Nessuna chiave di destinazione.** Un `.sync` contiene solo regole: `dest:` e `-dest:` non
esistono più (le destinazioni vivono esclusivamente in `~/.safekeep`, §3). Una riga **a forma
di chiave** (`^[A-Za-z_-][A-Za-z0-9_-]*\s*:`) che non è una chiave nota è una **riga invalida**:
warning + skip con `log_level: info`, `ConfigError` con `log_level: debug`. Vale lo stesso per
ogni chiave ignota. La forma di chiave ha la precedenza sulla forma nuda, quindi `dest: /x`
resta invalida e non viene interpretata come pattern.

Le chiavi di sezione devono stare **prima** delle regole `include:`/`exclude:`; il parser le
accetta comunque in qualunque ordine, ma per leggibilità si raccomanda l'ordine
`name` → `defaults` → regole.

### 4.3 Regole `include:` / `exclude:` e last-match-wins

Ogni riga che non è chiave di sezione è una regola, nella forma esplicita o nuda (§4.2):

```
include: <pattern>
exclude: <pattern>
<pattern>            # forma nuda ≡ include:
!<pattern>           # forma nuda ≡ exclude:
```

**Semantica last-match-wins** (l'ultima regola che matcha il path decide):

1. Si parte da un verdict iniziale derivato dai **defaults** (builtin + globali + `defaults:` di
   progetto, in quest'ordine, ultimi che vincono tra di loro).
2. Si scorrono le regole del `.sync` **dall'alto verso il basso**.
3. L'ultima regola il cui pattern matcha il path (o un genitore directory) **sovrascrive** il
   verdict: `include:` → incluso, `exclude:` → escluso.
4. Se nessuna regola matcha (in nessun layer), vale il **default allow-list**:
   **file → NON copiato**, **directory → attraversata**. Le directory si potano SOLO su
   un'esclusione esplicita: altrimenti una dir non matchata (es. `src` con `include: *.md`)
   bloccherebbe l'accesso a `src/README.md`. Una **directory symlink** è però una **foglia**
   (il walk non la scende, §7.3): viene valutata come un *file*, quindi di default **non**
   copiata — altrimenti il default "attraversa" la copierebbe senza che alcun `include:`
   la citi.

Conseguenze importanti:

- un `exclude:` **batte** un `include:` se sta **sotto** di esso;
- un `include:` più in basso **ri-include** ciò che un `exclude:` aveva escluso;
- le regole di `.sync` **vincono** sempre su defaults e su regole globali;
- **semantica allow-list**: un `.sync` con soli `include:` copia **solo** i file che
  matchano almeno una regola; ogni file da copiare deve quindi matchare un `include:`.
- per ottenere l'effetto opposto, "copia tutto tranne X", serve un include che copra
  tutto: `include: **` come **prima** regola del `.sync` (o tra i `defaults:`).
  Attenzione: `include: **` sta **sopra** i builtin (vedi punto sopra) e quindi
  ri-includerebbe `.git/`, `node_modules/`, `.venv/`, `__pycache__/`, `.DS_Store`,
  `*.swp`: rielencali come `exclude:` subito dopo l'`include: **`.

**Pruning directory**: se il verdict per una directory è *esclusa*, l'intero sottoalbero viene
potato (nessun walk, nessun watch evento figlio processato). Es. `exclude: .vault/` non fa
visitare `.vault/` per niente. Una directory che **non** matcha nessuna regola viene invece
attraversata (il pruning serve solo per le esclusioni esplicite, non per l'allow-list sui file).

**Pattern ancorati** (`/foo`): matchano solo rispetto alla radice della sorgente, non in
qualsiasi posizione.

Esempio completo commentato: [`examples/sync.example`](examples/sync.example).

---

## 5. Semantica dei pattern

Matcher custom, ispirato a gitignore, implementato nello script (nessuna dipendenza esterna).
**Divergenza da gitignore**: il default non è "incluso" ma **deny per i file** — nessun match
→ file non copiato, directory attraversata (allow-list, §4.3).

| Pattern | Matcha |
|---|---|
| `*` | qualsiasi sequenza di caratteri **dentro** un singolo segmento di path (no `/`) |
| `**` | qualsiasi sequenza di segmenti, incluso `/` (es. `docs/**`) |
| `?` | un singolo carattere (no `/`) |
| `foo/` | la directory `foo` (e tutto il suo contenuto, grazie al pruning) |
| `/foo` | `foo` solo in cima alla radice della sorgente (ancorato) |
| `# ...` | commento (riga intera), ignorata |
| `\x` | escape del carattere `x` (es. `\#` per un file che si chiama `#foo`) |
| riga vuota | ignorata |

**Ancoraggio dei pattern** (4 regole, stile gitignore):

1. **Nessuna `/` interna** (`*.md`, `.env`, `foo`) → matcha il **basename a qualsiasi
   profondità**: `*.md` matcha `README.md`, `docs/README.md` e `a/b/c.md`.
2. **`/` interna** (`docs/a.md`, `src/*`) → **ancorato alla radice** del progetto: si matcha
   l'intero path relativo alla root.
3. **`/foo` iniziale** → sempre ancorato alla radice (caso particolare del punto 2).
4. **`foo/` finale** → solo directory; `dir/**` matcha anche la directory `dir` stessa, così il
   pruning la esclude per intero.

`*` resta limitata al singolo segmento (no `/`): nel punto 1 vale per il basename, nel punto 2
per il segmento finale del pattern.

Regole di default builtin (bassa priorità, sempre presenti salvo override esplicito):

```
.git/
node_modules/
.venv/
__pycache__/
.DS_Store
*.swp
```

Compatibilità: `include:`/`exclude:` sono le chiavi esplicite, ma **non sono obbligatorie** —
in 0.2.0 una riga nuda (senza chiave) in `.sync` è ammessa come shorthand:

| Forma | Equivale a |
|---|---|
| `pattern` | `include: pattern` |
| `!pattern` | `exclude: pattern` |
| `chiave: valore` (`dest:`, `-dest:`, chiave ignota) | riga invalida (§4.2) |

Le righe nude condividono la lista e l'ordine delle chiavi esplicite (last-match-wins): un
`!riga` scritto sotto un `include:` lo batte, come nel caso esplicito. Attenzione alla
**semantica opposta rispetto a gitignore**: la riga elenca i file da copiare, non quelli da
ignorare (§4.2).

---

## 6. Flusso evento → sync

`safekeep.py run`:

1. **Carica** la config globale; se assente/invalida → exit code ≠ 0 (launchd con `KeepAlive`
   riprovarebbe: per questo `doctor` va lanciato a mano e il plist usa `KeepAlive` solo dopo un
   load riuscito).
2. **Scopre i progetti**, costruisce la mappa `radice → progetto → regole`
   (le dest sono quelle globali di `~/.safekeep`): modalità source → scan delle
   `source` per `.sync`; **modalità auto-discovery** (nessuna `source`) → scan di
   `$HOME` con pruning dedicato (§4.1), stessa logica, radice unica.
3. **Reconcile iniziale** di tutti i progetti (vedi §9).
4. **Calcola le regex di esclusione** dai pattern (compresi builtin e regole di sezione `exclude:`
   statiche) e avvia il subprocess:

   ```
   fswatch -0 -m fsevents_monitor -r -l 1.0 -e <regex esclusioni> -- <radici watch>
   ```

   - **radici watch**: le `source` del config (modalità source, deduplicate: una
     radice annidata sotto un'altra già coperta viene saltata — `fswatch -r`
     copre già il sottoalbero), oppure `$HOME` (modalità auto-discovery);
   - `-0`: separatore NUL (path con spazi/newline sicuri);
   - `-m fsevents_monitor`: monitor nativo FSEvents (File System Events — API di notifica
     filesystem di macOS);
   - `-r`: ricorsivo;
   - `-l 1.0`: batching di almeno 1s (anti-flood a monte);
   - `-e <regex>`: pre-filtro esclusioni lato fswatch (le regex sono un filtro grezzo; la
     decisione finale resta al matcher Python, che è l'unica fonte di verità). In
     **modalità auto-discovery** le regex includono in più le exclude dedicate della
     `$HOME`: `Library`, `.Trash`, `.cache` e `.local/state/safekeep` — non osserviamo
     i nostri stessi log.

5. **Per ogni path ricevuto** (split su `\0`):

   ```
   for path in events:
       project = match_project(path)            # la radice source che lo contiene
       if project is None:
           if path finisce con '/.sync' e la directory esiste:
               discovery + reconcile del nuovo progetto   # aggancio istantaneo
           continue                             # non seguito (niente .sync)
       if is_dir_pruned(path): continue         # verdict esclusa → skip subtree
       verdict = matcher.evaluate(path)          # last-match-wins (§4.3)
       if not verdict: continue                  # non incluso → non copiato (allow-list)
       queue.add(path)
   ```

   L'eccezione su `<dir>/.sync` è il percorso di **nuovo progetto**: un `.sync`
   creato dopo l'avvio (anche in una directory finora ignota) viene scoperto e
   riconciliato subito, senza aspettare il rescan.

   **Dedup della finestra (2s)**: lo stesso path arrivato due volte entro 2s viene
   processato una volta sola, ma la soppressione **non perde l'evento**: la prima
   soppressione dopo un'emissione programma una scadenza fissa `emissione + 2s`
   che gli arrivi successivi non estendono, e alla scadenza il path viene emesso
   comunque (emissione di coda). La scadenza entra nel timeout del `select` del
   loop — nessun thread e nessun timer dedicato.

6. **Debounce/stability**: per ogni path in coda, **stability check** (§7): due `stat` con la
   stessa `size` e `mtime` → procedi; altrimenti ri-programma il path tra ~1s (max N tentativi).
7. **Copia atomica** su ogni destinazione globale.
8. **Anti-flood**: se in una finestra di batch un progetto supera **5000 path** distinti →
   si abbandona la coda incrementale per quel progetto e si lancia un **reconcile completo del
   progetto** (§9).
9. **Riconciliazione temporizzata**: timer ogni **24h** → **rescan di discovery**
   (§4.1: recupera i `.sync` di cui è perso l'evento per un downtime) + reconcile
   di tutti i progetti.
10. **Mount**: se una dest non è raggiungibile (`dest_state` → `absent`) → stato
    `pending` per quel progetto/dest; backoff esponenziale (vedi §8.3); al remount → reconcile
    del progetto verso quelle dest.
11. Il processo resta vivo: è `launchd` con `KeepAlive` a riavviarlo se muore; l'uscita pulita
    (`SIGTERM`) chiude `fswatch`, scarica la coda, esce 0.

---

## 7. Copia atomica e robustezza

### 7.1 Stability check (evita copie di file in scrittura)

```
s1 = os.stat(path)
sleep(~0.2s)
s2 = os.stat(path)
if (s1.st_size, s1.st_mtime_ns) != (s2.st_size, s2.st_mtime_ns):
    ritenta da capo (1 + 2 tentativi totali), poi False → "ritenta più tardi"
```

Due `stat` consecutivi con `size` + `mtime` identici ⇒ il file è fermo abbastanza da essere
copiato. Non è una garanzia assoluta (mtime granularity), ma sufficiente per 0.1.0; un eventuale
hashing (size+sha256) è futuro. `FileNotFoundError` su uno dei due `stat` → `False` silenzioso.

### 7.2 Copia atomica

Per ogni file da copiare:

1. crea il tmp **nella stessa cartella della destinazione** (stesso filesystem ⇒ `os.replace`
   atomico): `<dst>.safekeep.tmp.<pid>` (un eventuale residuo per quel dest viene rimosso
   prima di scrivere);
2. copia bytes (`shutil.copyfile`) + `copystat` (mtime/permessi): `copystat` fallito (es. exFAT
   senza chmod/utime) → log debug, **non fatale**;
3. `os.fsync()` sul file tmp (best-effort, log debug se fallisce);
4. `os.replace(tmp, finale)` — rename atomico POSIX.

Un lettore sul destino vede o il file vecchio o il file nuovo, mai uno parziale. Un `OSError`
di I/O (ENOSPC/EIO/EBUSY/ENODEV) **si propaga** al caller: è lui a decidere il retry.

**Containment della dest**: prima di ogni scrittura la directory di destinazione deve
risolversi **dentro** la dest. Un componente-symlink della dest che punta fuori è un
artefatto della dest stessa (§7.3 ricrea i link sorgente): viene rimosso e ricreato come
directory reale, e la scrittura prosegue solo se dopo il ripristino il path risolve ancora
dentro — altrimenti la copia è bloccata (`scrittura fuori da dest bloccata`): **mai scrivere
fuori dalla dest**, nemmeno via symlink creati da copie precedenti.

### 7.3 Symlink

- Il **symlink sorgente** viene **ricreato come symlink** sul destino (`os.symlink` con lo
  stesso target, relativo conservato): non si segue, non si copia il contenuto puntato.
- Symlink rotto sorgente: ricreato identico (anche se rotto) + log debug.
- Directory symlink: trattata come symlink (nessun recursive), coerente con "mai cancellare"
  e con l'assenza di follow. Essendo una **foglia** viene valutata dal matcher come un file
  (§4.3): senza un `include:` che la citi **non viene copiata** — la valutarla come directory
  la coprirebbe sempre e creerebbe nella dest lo scheletro di tutte le cartelle figlie.

### 7.4 Altri casi di robustezza

| Situazione | Comportamento |
|---|---|
| File sorgente sparisce tra stat e copy | `FileNotFoundError` → skip silenzioso (era già stato copiato prima, o sarà al prossimo reconcile) |
| Permesso negato su dest | log error, stato dest `error`, retry al prossimo reconcile |
| Dest non montata | stato `pending` (§8.3), nessun errore rumoroso |
| Crash a metà copia | residuo `.safekeep.tmp.<pid>` rimosso alla prossima copia sullo stesso dest; `doctor` elenca i residui rimasti |
| OOM/kill | `launchd KeepAlive` riavvia → reconcile iniziale → converge |

---

## 8. launchd

### 8.1 Plist completo

`~/Library/LaunchAgents/com.safekeep.agent.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.safekeep.agent</string>

    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/Users/REPLACEME/safekeep/bin/safekeep.py</string>
        <string>run</string>
    </array>

    <!-- Avvio al boot -->
    <key>RunAtLoad</key>
    <true/>

    <!-- Tenerlo vivo: se muore, launchd lo riavvia -->
    <key>KeepAlive</key>
    <true/>

    <!-- Opzionale: (ri)avvio quando un volume viene montato (vedi §8.3) -->
    <!-- <key>StartOnMount</key><true/> -->

    <key>StandardOutPath</key>
    <string>/tmp/safekeep.out.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/safekeep.err.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
</dict>
</plist>
```

Validazione: `plutil -lint ~/Library/LaunchAgents/com.safekeep.agent.plist`.

### 8.2 Comandi di reload

```bash
# Scarica l'agente (se presente) — 'bootout' disinstalla il job dal bootstrap domain
launchctl bootout gui/$(id -u)/com.safekeep.agent 2>/dev/null

# (Re)installa e carica l'agente — 'bootstrap' lo registra e lo avvia (RunAtLoad)
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.safekeep.agent.plist

# Forza un riavvio immediato senza bootout/bootstrap
launchctl kickstart -k gui/$(id -u)/com.safekeep.agent

# Stato
launchctl print gui/$(id -u)/com.safekeep.agent | head -40
```

### 8.3 Gestione volumi non montati (pending + backoff)

Destinazioni su volumi esterni/reti:

1. Prima di copiare: `dest_state(dest_root)` — solo `os.path.exists`, **nessun processo
   figlio** — se il path non esiste (volume non montato) → la coppia *(progetto, dest)*
   passa in stato **`pending`**.
2. **Backoff sul stat del mountpoint**: retry con intervalli esponenziali
   `1s → 2s → 4s → 8s → 16s → 32s → 60s → 60s → …` (`backoff_schedule()`, cap 60s) finché
   il mount non compare; al ritorno dello stato `mounted` → **reconcile del progetto**
   verso quelle dest (recupera tutto ciò che è cambiato durante la disconnessione — la
   semantica "mai cancellare" rende questo sicuro).
3. `status` mostra le dest `pending` con l'ultimo errore e il prossimo retry.
4. **Opzione plist `StartOnMount`**: aggiungendo `<key>StartOnMount</key><true/>` launchd
   riavvia il job al mount di un volume — utile se si preferisce un kick sincrono del sistema
   invece (o in aggiunta) al backoff applicativo. Nel 0.1.0 è documentato e lasciato commentato
   nel plist: l'event driver fallback è il backoff.

---

## 9. Comandi CLI

```
bin/safekeep.py <comando> [--config PATH] [--project PATH] [--json] [-v]
```

| Comando | Cosa fa | Exit code |
|---|---|---|
| `run` | daemon: reconcile iniziale + watch fswatch + dispatch eventi + timer 24h | 0 su SIGTERM pulito; 1 se la config è invalida o una dest è dentro la sorgente/$HOME |
| `sync-once` | un singolo passaggio: walk sorgente, copia ciò che differisce, esce; `--prune` rimuove in più dalla dest i file non più inclusi (vedi sotto) | 0 se tutto ok; 1 se la config è invalida o una dest è dentro la sorgente/$HOME |
| `status` | sola lettura: config path, modalità (`source` / auto-discovery da `$HOME`), source, progetti scoperti con N regole, ogni dest con `dest_state` (ok/absent) | 0 se la config è valida |
| `doctor` | diagnostica: config, dest non sotto source, fswatch + monitor, python ≥ 3.9, probe TCC, residui tmp, lint plist | 1 se un check **fatale** fallisce |

`sync-once --prune` è l'unica opzione che **cancella**: rimuove dalla dest i file il cui
sorgente esiste ancora ma che il matcher ora esclude (regole cambiate), solo sotto il
prefisso del progetto e solo dove la corrispondenza è certa — sorgente sparita o diventata
directory → file intatto (policy "mai cancellare"). Default senza `--prune`: nessuna
rimozione. Il controllo "dest dentro source/$HOME" è lo stesso di `doctor`
(vedi §11): con `run` e `sync-once` esce con codice ≠ 0.

Esempi:

```bash
# Primo sync manuale di tutto (dry-run: cosa copierebbe, senza copiare)
python3 bin/safekeep.py sync-once --dry-run

# Sync reale di un singolo progetto
python3 bin/safekeep.py sync-once --project ~/Code/myapp

# Stato leggibile / macchina-leggibile
python3 bin/safekeep.py status
python3 bin/safekeep.py status --json

# Diagnostica completa (lanciarla DOPO aver concesso i permessi TCC/FDA)
python3 bin/safekeep.py doctor

# Avvio manuale del daemon (senza launchd) con log verbose
python3 bin/safekeep.py run -v
```

`doctor` controlla almeno:

1. presenza e versione di `fswatch` (`fswatch --version`);
2. sintassi config globale e di ogni `.sync` (`.sync` illeggibile o senza regole valide →
   riga ✗ **non fatale**, con il conteggio delle righe scartate);
3. **che nessuna destinazione sia una sottodirectory di una sorgente** (loop di copia
   infinito): check fatale (exit ≠ 0) anche in `run` e `sync-once`, con base = `source`
   se presenti, altrimenti `$HOME` in modalità auto-discovery (§11);
4. che le dest siano scrivibili;
5. che il plist di launchd esista e passi `plutil -lint`;
6. accesso ai source (test read + probe TCC);
7. residui `.safekeep.tmp.*`.

---

## 10. Riconciliazione e anti-flood

### 10.1 Reconcile

Walk ricorsivo della sorgente; per ogni file con verdict **incluso** (prune delle dir con
verdict *esclusa*; un file non matchato da nessuna regola non è incluso → non copiato, §4.3):

- dest non esiste → copia;
- dest esiste ma `size` o `mtime` differiscono → copia (atomicamente);
- dest uguale → skip (idempotenza).

**Errori per file** (nessun errore I/O di un singolo file abortisce il walk): ogni copia
fallita viene loggata e contata in `stats['errati']`, e il resto del progetto viene
processato comunque; i file già aggiornati contano in `stats['skippati']`. Un errore di
**mount** (`ENODEV`/`EBUSY`/`ENOSPC`) dice invece che la dest non è usabile adesso: il walk
si ferma, `stats['dest_pendente']` segnala al caller di mettere quella dest in `pending`
(§8.3) — le altre dest proseguono. Al retry la dest rifallisce → il backoff **riprende da
dove era** (mai ripartire da 1s: niente loop a 1Hz).

**Quando gira:**

| Momento | Ambito |
|---|---|
| avvio di `run` (e quindi di ogni riavvio launchd) | tutti i progetti |
| remount di una dest `pending` | progetto → quelle dest |
| ogni 24h (timer) | tutti i progetti |
| `sync-once` | tutti (o `--project`) |
| flood >5000 path (§10.2) | quel progetto |

### 10.2 Anti-flood (event storm)

`fswatch -l 1.0` raggruppa già, ma un `git checkout` o un `npm install` possono produrre decine
di migliaia di path in un batch per singolo progetto.

```
if len(paths_per_project[project]) > 5000:      # soglia configurabile in futuro
    discard_queued(project)                    # la coda incrementale viene buttata
    enqueue_full_reconcile(project)            # un solo walk, copie solo dove serve
```

Perché funziona: il reconcile è già il percorso "copia tutto ciò che differisce"; buttare la
coda e riconciliare costa un walk in più ma evita 50k `stat`+`copy` duplicati e la crescita
illimitata della coda in memoria.

---

## 11. Sicurezza e TCC / FDA

- **TCC (Transparency, Consent and Control)**: il framework di macOS che chiede all'utente il
  consenso per accessi protetti (Desktop, Documenti, download, disco rimovibile…).
- **FDA (Full Disk Access)**: il permesso TCC che concede accesso a l'intero disco; su macOS
  26/27 va concesso **al processo interprete che esegue lo script** (default `/usr/bin/python3`,
  quindi "Python" in *Impostazioni → Privacy e sicurezza → Accesso completo al disco*), **e** a
  `fswatch` se lanciato come binario separato e i source stanno in aree protette. Concedere a un
  terminale che esegue il CLI non basta per il daemon avviato da launchd: il job eredita il
  contesto di login ma non i consensi "per sessione" del terminale.
- Senza FDA: `doctor` segnala `EACCES`/`Operation not permitted` con istruzioni; il daemon non
  deve loopare: un `PermissionError` su una radice source → log error + backoff lento (60s),
  non crash.
- **Dest sotto source** → errore fatale (exit ≠ 0) in `doctor`, `run` **e** `sync-once`
  (il file copiato verrebbe riosservato all'infinito: loop). La base del controllo è la
  `source` se presente, altrimenti `$HOME` in modalità auto-discovery: così viene bloccata
  anche una dest dentro la `$HOME`, dove finirebbero comunque le copie.
- **Destinazioni solo in `~/.safekeep`**: un `.sync` **non può aggiungere né rimuovere
  destinazioni per costruzione** (§4.2) — la sua sintassi non prevede chiavi di destinazione,
  quindi un `.sync` malevolo incluso in un repo clonato da terzi non può far scrivere il
  backup altrove. Le righe `dest:`/`-dest:` in un `.sync` sono righe invalide e vengono
  rifiutate (warning con `log_level: info`, errore con `log_level: debug`).
- Nessuna esecuzione di codice dai file di config: la config è **dati**, non script (parse
  rigido, path normalizzati, nessun `eval`, nessun glob verso shell).
- I log non contengono il contenuto dei file, solo path e conteggi.

---

## 12. Edge case e mitigazioni

| Edge case | Mitigazione |
|---|---|
| File sorgente in scrittura durante la copia | stability check (2 stat size+mtime) + retry |
| Crash durante la copia | tmp nella cartella dest + `os.replace` atomico; residuo rimosso alla prossima copia, `doctor` elenca i rimasti |
| Path con spazi, newline, caratteri non ASCII | fswatch `-0` (separatore NUL) + `os.fsdecode` |
| Eventi persi / daemon crash | niente journal: reconcile (boot, mount, 24h) convergenza garantita |
| `git checkout` → decine di migliaia di eventi | >5000 path/progetto → collapse a reconcile del progetto |
| File cancellato alla sorgente | resta nel backup (semantica confermata) |
| Volume dest non montato | pending + backoff stat mountpoint; reconcile al remount |
| `.sync` malevolo in repo clonato | le dest vivono solo in `~/.safekeep`: `dest:` in un `.sync` è riga invalida (§4.2, §11) |
| Dest dentro la sorgente (loop) | errore in `doctor`, `run` e `sync-once` → exit ≠ 0 (base = `source` o `$HOME` in auto-discovery, §11) |
| Subdirectory senza `.sync` | non seguita (nessuna copia) |
| Symlink | ricreati come symlink, non seguiti |
| Directory enorme (`node_modules`) | pruning dir sul verdict *esclusa* + regex pre-filtro `-e` di fswatch |
| mtime con granularità bassa (HFS+) | doppio stat + copia se size diverso anche a parità mtime |
| Ora di sistema spostata indietro | confronto per *differenza* mtime, non per "più recente": comunque copia |
| Permessi insufficienti (TCC negato) | log + backoff 60s, `doctor` con istruzioni |
| File `.sync` rimosso | il progetto smade di essere seguito; le copie esistenti restano intatte |
| `.sync` creato dopo l'avvio (o perso per downtime) | evento su `<dir>/.sync` → discovery + reconcile del nuovo progetto; in più rescan di discovery ogni 24h (§6) |
| Directory rumorose nella `$HOME` (auto-discovery) | pruning nello scan (nascoste + `Library`, `.Trash`, `.cache`, …) e regex `-e` dedicate di fswatch |
| Omonimia di path tra due progetti | **risolto**: il path dest in `relative` è calcolato come relativo alla **radice `source`** che contiene il progetto (§3), quindi include il segmento del progetto — `projA/docs/note.md` e `projB/docs/note.md` non collidono. Resta solo l'omonia fra due *radici source diverse* con la stessa prima directory (prefisso col `name:` in futuro) |

---

## 13. Piano di implementazione (T1..T7)

| Task | Contenuto | Criteri di verifica | Stato |
|---|---|---|---|
| **T1** | Matcher gitignore-like (`safekeep/matcher.py`): `*` `**` `?` `dir/` `/ancora` commenti escape, last-match-wins, dir pruning | mini-suite di casi (≥20 path × regole) tutte verdi; caso "exclude batte include" e "include più basso ri-include" passano | ✅ fatto (`tests/test_matcher.py`) |
| **T2** | Parser config globale `~/.safekeep` + parser `.sync` senza dest (`safekeep/config.py`): sezioni, regole, errori fail-fast, esempi in `examples/` | parse di `examples/safekeep.example` e `examples/sync.example` produce la struttura attesa; riga invalida → errore con numero di riga; `dest:` in `.sync` → warning con info / errore con debug | ✅ fatto (`tests/test_config.py`) |
| **T3** | Copia atomica + stability check + symlink + walk/reconcile idempotente (`safekeep/copier.py`) | test su tmpdir: contenuto + mtime preservati; src instabile/sparito → `False` senza residui tmp; symlink (anche rotti e di directory) ricreati come symlink; seconda run = 0 copie | ✅ fatto (`tests/test_copier.py`) |
| **T4** | Destinazioni non montate (`safekeep/volumes.py`): `dest_state`, `backoff_schedule()` (1→2→…→60 cap), `partition_dests` | `dest_state` su path esistente/assente; backoff con cap a 60s; split (ready, absent) | ✅ fatto (`tests/test_volumes.py`) |
| **T5** | `run`: subprocess fswatch (`-0 -m fsevents_monitor -r -l 1.0 -e …`), dispatcher eventi, debounce, timer 24h, pending/backoff mount, anti-flood (>5000 → collapse a reconcile) | modificare un file sorgente → compare sul dest entro ~2s; dest smontata → pending, remount → catch-up; flood simulato triggera il collapse | da fare |
| **T6** | CLI `status`/`doctor` + plist launchd + comandi bootout/bootstrap/kickstart (`launchd/`, `install.sh`, `uninstall.sh`) | `doctor` verde su macchina con permessi; plist passa `plutil -lint`; kill del processo → launchd lo riavvia da solo | ✅ fatto (`tests/test_cli.py`) |
| **T7** | Sicurezza (dest sotto source, `.sync` senza chiavi di dest), logging, edge case, docs (questa SPEC) | `doctor`/`run`/`sync-once` escono con ≠ 0 su dest sotto source; `.sync` non attendibile gestito (info → warning + skip, debug → fatale); containment della dest (nessuna scrittura fuori); dedup senza perdita di eventi; errori I/O confinati al file; tabella edge case coperta da casi di test | ✅ fatto (`tests/test_matcher.py`, `tests/test_config.py`, `tests/test_copier.py`, `tests/test_daemon.py`, `tests/test_cli.py`) |
| **T8** | Auto-discovery (`source` opzionale): scan di `$HOME` con pruning dedicato, watch root `$HOME` con exclude home, evento su `/.sync` → nuovo progetto, rescan al timer 24h, dedup watch roots (`safekeep/config.py`, `safekeep/daemon.py`) | config senza `source` valida (senza `dest` → errore con hint); `.sync` in `$HOME` scoperto, `Library`/nascoste potato, sotto-progetto annidato scoperto; watch root source invariata (backward compat); evento `.sync` → discover + reconcile; rescan 24h | ✅ fatto (`tests/test_config.py`, `tests/test_daemon.py`, `tests/test_cli.py`) |

Ordinamento: T1→T2 (fondamenta), T3→T4 (nucleo sync), T5 (daemon), T6 (operatività),
T7 (indurimento). Ogni task è verificabile in isolamento. Test: `python3 -m unittest
discover -s tests`.

---

## 14. Decisioni aperte

1. **Dove stanno i progetti sorgente reali?** Le `source` globali del config globale vanno
   populate: non è ancora deciso se i progetti vivano sotto `~/Code`, `~/Projects`, o radici
   multiple (incluso `~/Documents`). Finché non si decide, `examples/safekeep.example` usa
   placeholder commentati — oppure si omette del tutto `source:` e si usa la **modalità
   auto-discovery** da `$HOME` (§3, §6).
2. **Inizializzazione git del progetto `safekeep`**: il repository è già inizializzato, ma
   **nessuna operazione git** (commit/push/branch/checkout) viene eseguita senza richiesta
   esplicita dell'utente. Da decidere: primo commit, o resta così.
3. Soglia anti-flood 5000: hardcoded in 0.1.0, da rendere configurabile se servirà.
4. Hash (sha256) come via alternativa a size+mtime per l'idempotenza: futuro, se la granularità
   mtime si dimostra insufficiente su alcuni volumi.
5. `StartOnMount` nel plist: commentato di default; attivarlo dipende da dove stanno le dest.

---

## 15. Versione

- **0.1.0** — semver: prima versione pre-release/0.x, API di config e CLI soggette a cambi
  incompatibili solo con bump di MINOR finché non si raggiunge 1.0.0.
- Ogni modifica successiva di `package`/manifest rispetta `MAJOR.MINOR.PATCH`
  (semver, https://semver.org).
