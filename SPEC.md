# SPEC.md — safekeep

**Versione:** 0.5.1 (semver — Semantic Versioning, https://semver.org)
**Stato:** pre-implementazione
**Piattaforma target:** macOS (FSEvents, launchd) e Linux (inotify, systemd) — §14

---

## 1. Panoramica e obiettivo

`safekeep` è un motore di backup **event-driven** per **macOS e Linux** (POSIX): osserva le
cartelle sorgente con **fswatch** (utility installabile via Homebrew su macOS, dai package
manager delle distro su Linux) e copia i file modificati su una o più destinazioni,
senza mai cancellare nulla nel backup.

Obiettivi, in ordine di priorità:

1. **Nessuna perdita di dati nel backup.** La semantica è *solo copia/aggiorna, mai cancellare*:
   un file rimosso o rinominato alla sorgente **resta nel backup** (storicizzazione di fatto).
2. **Semplicità operativa.** Unico script Python 3 (solo stdlib), zero dipendenze esterne oltre a
   `fswatch` e al sistema di avvio (`launchd` su macOS, `systemd` su Linux — §14.3).
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

**Onestà su cosa è testato.** macOS è la piattaforma primaria ed è testata in locale
(suite + daemon launchd reale). Per Linux il supporto è reale ma coperto da: **CI su
Debian/Ubuntu** (suite completa con `fswatch` reale installato) e **E2E su Omarchy/Arch**
(suite, `doctor`, sync one-shot e propagazione degli eventi su una VM Linux reale).
Restano fuori copertura: altre distro (Fedora et al. — coperte solo dagli hint di
installazione), systemd senza sessione di login (linger, §14.3) e architetture non x86_64.
Dettaglio del design di portabilità: §14.

---

## 2. Architettura

```
                        ┌──────────────────────────────────────────────┐
                        │   init system (§14.3):                       │
                        │   launchd (plist) / systemd (user unit)      │
                        │   RunAtLoad/WantedBy + KeepAlive/Restart     │
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
                                           │ fswatch -0 -m <monitor>          │
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
| launchd plist / systemd user unit | `~/Library/LaunchAgents/com.safekeep.agent.plist` / `~/.config/systemd/user/safekeep.service` | tenere vivo il processo, avvio al boot (§14.3) |
| CLI diagnostica | `status` / `doctor` | salute, TCC (Transparency, Consent and Control, solo macOS), validazione config |

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
   fswatch -0 -m <monitor> -r -l 1.0 -e <regex esclusioni> -- <radici watch>
   ```

   - **radici watch**: le `source` del config (modalità source, deduplicate: una
     radice annidata sotto un'altra già coperta viene saltata — `fswatch -r`
     copre già il sottoalbero), oppure `$HOME` (modalità auto-discovery);
   - `-0`: separatore NUL (path con spazi/newline sicuri);
   - `-m <monitor>`: monitor nativo **dichiarato esplicitamente per piattaforma**
     (mai il default implicito di fswatch): `fsevents_monitor` su Darwin
     (FSEvents = File System Events, API di notifica filesystem di macOS) e
     `inotify_monitor` su Linux (inotify, subsystem del kernel Linux) — §14.2;
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

## 8. launchd (macOS)

Su **macOS** l'init system (sistema di avvio) è `launchd`: è ciò che descrive questa
sezione. Su **Linux** è `systemd` con una **user unit** — il confronto completo, il
template della unit e gli script di installazione sono in **§14.3**; questa sezione
rimane la fonte per il ramo macOS.

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

### 8.4 Interprete dell'agent: path stabile (incidente 03/10/2026, fix 0.5.1)

**Incidente reale (timeline).** Fino a 0.5.0 `install.sh` rendeva `__PYTHON__` con default
`/opt/homebrew/bin/python3`, che Homebrew (package manager di macOS) risolve nel path
**Cellar** della versione installata:

| Quando | Cosa |
|---|---|
| 03/10 18:54 | launchd avvia il daemon: exe = `Cellar/python@3.14/3.14.6/...` |
| 03/10 21:20 | `brew upgrade` porta Python 3.14.6 → 3.14.8 e **cancella** la vecchia cartella Cellar: l'eseguibile su cui gira il processo sparita da disco |
| ore dopo | **TCC** (Transparency, Consent and Control, sistema permessi di macOS) non riesce più a identificare il processo (`proc_pidpath_audittoken() failed`) e nega i volumi rimovibili → `EPERM` (Operation not permitted) su **ogni** copia per ~45h: 5.251 righe `ERROR copia fallita …` identiche nel log, 4.829 in un giorno |
| recovery | ri-grant manuale dei permessi + `launchctl kickstart -k` (riparte sul nuovo exe → torna a funzionare) |

Sintomo e log ribollente erano conseguenze, non causa: la causa era l'exe cancellato sotto
un processo vivo. Il check `doctor` "errori di permesso recenti" (§14.4) vedeva il sintomo
ma non poteva vedere la causa — per questo 0.5.1 aggiunge anche il check exe (§14.4).

**Fix (0.5.1): `__PYTHON__` = `/usr/bin/python3`.** `install.sh` rende ora di default lo
**shim di sistema** `/usr/bin/python3` (Apple-gestito, non toccato dagli upgrade Homebrew,
già l'interprete che §11 raccomanda per il consenso FDA), validato **≥ 3.9** eseguendolo
(`py_ok`); shim assente o troppo vecchio → fallback a `python3` nel `PATH`
(`SAFEKEEP_PYTHON` resta l'override esplicito, come prima). Lo stesso `__PYTHON__`
alimenta `SAFEKEEP_PYTHON` del plist trend (§9.2), quindi **entrambi** gli agent sono
coperti dalla stessa scelta.

Soluzioni valutate:

- **(a) shim di sistema** — **scelta**. L'exe su cui gira il daemon non può essere
  cancellato da `brew upgrade`, quindi la classe "exe vivo sparito → TCC non identifica il
  processo" sparisce; il plist resta dichiarativo (launchd vede un `ProgramArguments`
  stabile, identificabile da TCC a ogni riavvio).
- **(b) wrapper shell** che risolve `python3` a ogni avvio — **scelta rifiutata**: la
  risoluzione avviene solo all'avvio, mentre il processo in esercizio resta sull'exe già
  risolto → un upgrade Homebrew con il daemon vivo ricreerebbe **esattamente** lo stesso
  incidente; in più il plist dichiarerebbe `/bin/bash` come programma, non l'interprete.

**Rischio CLT (Command Line Tools) dichiarato:** su macOS `/usr/bin/python3` è uno shim che
richiede i CLT: senza, l'esecuzione mostra il prompt d'installazione. Mitigazione doppia:
`install.sh` validerebbe comunque l'interprete eseguendolo (shim che non risponde →
fallback `command -v python3`, comportamento pre-0.5.1) e Homebrew presente implica quasi
certamente CLT (verificato in locale: `xcode-select -p` → Xcode.app; `/usr/bin/python3` →
3.9.6; la suite è verde anche girata con quell'interprete).

**Plist live già installati:** launchd **non rilegge** il file fino a `bootout`+`bootstrap`
(o reboot): i plist renderizzati col vecchio default restano fragili finché non vengono
re-renderizzati. `install.sh` è idempotente; re-render + ricarica:

```bash
./install.sh            # re-render del plist principale (idempotente, non riparte da solo)
launchctl bootout gui/$(id -u)/com.safekeep.agent 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.safekeep.agent.plist
./install.sh --trend    # stesso trattamento per l'agent trend (fa da solo bootout+bootstrap)
```

Verifica: `ps -p <pid> -o comm=` deve mostrare `/usr/bin/python3` (exe esistente su disco)
e `launchctl print gui/$(id -u)/com.safekeep.agent | grep -E 'state|program'`.

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
| `doctor` | diagnostica: config, dest non sotto source, fswatch + monitor di piattaforma, python ≥ 3.9, probe TCC, lint plist ed eseguibile del processo ancora su disco (solo macOS, §14.4), residui tmp, limite inotify (solo Linux); con `--json` la stessa diagnostica come documento JSON su stdout (§9.1) | 1 se un check **fatale** fallisce |

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

# Stessa diagnostica, machine-readable (per tracciare l'andamento nel tempo)
python3 bin/safekeep.py doctor --json

# Avvio manuale del daemon (senza launchd) con log verbose
python3 bin/safekeep.py run -v
```

`doctor` controlla almeno:

1. presenza e versione di `fswatch` (`fswatch --version`) e **monitor atteso per la
   piattaforma** presente in `fswatch -M` (`fsevents_monitor` su Darwin,
   `inotify_monitor` su Linux, §14.4) con hint di installazione per distro;
2. sintassi config globale e di ogni `.sync` (`.sync` illeggibile o senza regole valide →
   riga ✗ **non fatale**, con il conteggio delle righe scartate);
3. **che nessuna destinazione sia una sottodirectory di una sorgente** (loop di copia
   infinito): check fatale (exit ≠ 0) anche in `run` e `sync-once`, con base = `source`
   se presenti, altrimenti `$HOME` in modalità auto-discovery (§11);
4. che le dest siano scrivibili;
5. che l'agent per l'init system esista e sia valido: plist launchd + `plutil -lint`
   su **Darwin**, unit systemd su **Linux** (§14.3 e §14.4); su Darwin, se il job è
   caricato, anche che il processo gira su un **eseguibile ancora presente su disco**
   (0.5.1, §8.4 e §14.4 — warning non fatale se no);
6. accesso ai source (test read + probe TCC — solo Darwin);
7. residui `.safekeep.tmp.*`;
8. su **Linux**, informativo e **non fatale**: `fs.inotify.max_user_watches` ≥ 16384
   (§14.4).

### 9.1 `doctor --json` — output strutturato

`safekeep doctor --json` stampa **un solo documento JSON su stdout** ed esce con lo
**stesso exit code** della modalità normale (0 se nessun check fatale fallisce, 1 se
almeno uno fallisce). Motivazione: poter tracciare nel tempo l'andamento degli errori
di permesso — un wrapper appende una riga per run a un CSV/file e il trend si legge
da lì. **Niente storicizzazione in safekeep**: nessun DB, nessun modulo nuovo, nessuna
retention — solo il dump strutturato di ciò che la modalità normale già calcola.

Schema del documento:

```json
{
  "safekeep": "0.4.0",
  "timestamp": "2026-10-04T10:20:30",
  "exit": 0,
  "checks": [
    {"id": "config_valida", "status": "ok",
     "message": "config valida: ~/.safekeep (2 source, 2 dest)"}
  ],
  "permission_errors": {"count_24h": 0, "last": null}
}
```

| Campo | Tipo | Significato |
|---|---|---|
| `safekeep` | stringa | versione (`safekeep.__version__`) |
| `timestamp` | stringa | `datetime.now()` in ISO 8601 (`YYYY-MM-DDTHH:MM:SS`, orario locale, senza timezone — coerente con i prefissi delle righe di log, §14.4) |
| `exit` | intero | copia dell'exit code restituito |
| `checks` | array | **stessa sequenza della modalità normale**: stessi check, stesso ordine, stesso esito, uno per riga — nessun check aggiunto o omesso |
| `checks[].id` | stringa | slug del messaggio: prefisso fino a `:` o `(`, minuscolo, tutto ciò che non è alfanumerico → `_`; id uguali ripetuti suffissati `_2`, `_3` (dedup, così l'id resta chiave usabile) |
| `checks[].status` | stringa | `ok` = riga ✔; `warn` = riga ✗ **non fatale**; `fail` = riga ✗ **fatale** (quella che fa uscire con 1) |
| `checks[].message` | stringa | testo esatto della riga, senza il prefisso `✔`/`✗` |
| `permission_errors.count_24h` | intero | errori di permesso nelle ultime 24h (§14.4); `0` anche quando il log è assente — in quel caso la riga di check riporta "check saltato" |
| `permission_errors.last` | stringa o `null` | timestamp dell'ultimo errore (`YYYY-MM-DD HH:MM:SS`), `null` se nessun errore o log assente |

Invariati rispetto alla modalità normale: stessi check, stessa non-fatalità, stessi hint
dentro `message` (in JSON sono testo, non righe formattate). Il flag vale **solo** per
`doctor`: gli altri comandi non lo hanno.

**Log e stdout — decisione**: nessuna mescolanza, e non serve deviare né silenziare
nulla. `doctor` non configura mai il logging (nessuna `setup_logging`: quella riserva
i log a `run`/`sync-once`, che hanno un `StreamHandler(sys.stderr)` + file rotante, §3)
e tutti i messaggi d'errore della CLI escono su stderr (§9). Quindi in modalità
`--json` l'unico contenuto di stdout è il documento JSON, parseabile con `json.load`.
Regola valida anche per il futuro: se `doctor` dovrà loggare, lo farà su stderr, mai su
stdout.

### 9.2 `bin/safekeep-trend.sh` — wrapper trend errori di permesso (orario)

`doctor --json` (§9.1) dà il punto **nello** tempo; per graficare l'andamento serve una
serie. Stessa regola di §9.1: **niente storicizzazione in safekeep** (nessun DB, nessun
modulo, nessuna retention) — un wrapper bash esterno esegue `doctor --json` e appende
una riga a un CSV. Frequenza **oraria**: 24 righe/giorno, ~1 KB/giorno, trascurabile.

**Schema CSV** — header scritto **una sola volta** (idempotente: creato solo se il file
non esiste o è vuoto), una riga per run:

```csv
timestamp,exit,count_24h,last
2026-10-04T10:20:30,0,0,
```

| Campo | Origine (documento §9.1) |
|---|---|
| `timestamp` | `timestamp` |
| `exit` | `exit` — il codice di `doctor`, **non** quello del wrapper |
| `count_24h` | `permission_errors.count_24h` |
| `last` | `permission_errors.last` (`YYYY-MM-DD HH:MM:SS`), **vuoto** se `null` |

Nessuno dei quattro campi contiene virgole (ISO 8601, intero, intero, `HH:MM:SS`):
la riga si legge con uno `split(',')` senza quoting.

**Esecuzione**: estrazione dei campi con `python3 -c` + `json` — **mai `grep` su
JSON**. L'exit code di `doctor` è un **dato**, non un errore: il wrapper appende la
riga anche quando `doctor` esce 1 (un check fatale è esattamente ciò che il trend deve
catturare). Fallisce (exit 1, messaggio su stderr) solo se l'output non è JSON
parsabile — **mai** una riga inventata.

**Override** (gli stessi meccanismi che usano i test):

| Env / flag | Default | Cosa |
|---|---|---|
| `HOME` | reale | da cui derivano config e path CSV (test: HOME finto) |
| `SAFEKEEP_TREND_CSV` | `~/.local/state/safekeep/permission-trend.csv` | path del CSV |
| `SAFEKEEP_PYTHON` | `python3` | interprete che esegue `doctor` ed estrae i campi |
| `--config PATH` | `~/.safekeep` | config passata a `doctor` |

**Agent (macOS, §8)**: template `launchd/com.safekeep.trend.plist`, reso da
`install.sh --trend` con lo **stesso** meccanismo di placeholder del plist principale
(`__REPO__`, `__PYTHON__`, `__HOME__` + `__SCRIPT__` = `bin/safekeep-trend.sh`).
`StartInterval` 3600 (orario) e **niente** `RunAtLoad`: la prima riga arriva dopo un'ora,
oppure si forza con `launchctl kickstart gui/$(id -u)/com.safekeep.trend`. Log unico
di stdout/stderr: `~/.local/state/safekeep/trend.log`. `install.sh --trend` fa
render + `launchctl bootstrap` **senza toccare né ricaricare la unit dell'agente
principale** (§8.2); `uninstall.sh --trend` fa `bootout` + rimozione del solo plist
trend.

**Fuori scope**: retention/rotazione del CSV, grafici, alert, agent su Linux (il
wrapper lì si lancia a mano o da cron/timer — non c'è ancora richiesta).

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

**Throttle delle righe di errore (0.5.1).** La stessa copia fallita può ripeterse senza
fine (un volume negato produce un errore per evento e per reconcile: 5.251 righe identiche
nell'incidente di §8.4). `log_copy_error()` in `safekeep/copier.py` deduplica per
**(path, errno)**: la prima occorrenza scrive la riga storica `copia fallita src → dst: …`
invariata; poi, per ogni chiave, al massimo **una riga ogni 300s** (`THROTTLE_INTERVAL`)
con il suffisso `— ripetuto N volte` (N = fallimenti compresi fra due righe loggate,
escluso quello che scrive la riga). I due call site storici (`reconcile_project` in §10.1
e `_copy` del daemon) passano da lì, quindi event-driven e reconcile sono coperti insieme.
Le fallite con errno di **mount** (`ENODEV`/`EBUSY`/`ENOSPC`) non sono throttlate: ce n'è
al massimo una per walk (il walk si ferma) e la loro riga (`errore di mount …`) non è
cambiata. I conteggi **non leggono mai dal log**: `stats['errati']` e le righe di sintesi
di `sync-once` restano identici. L'unico consumatore del log è `recent_permission_errors`
(§14.4), adattato a restare onesto (vedi là). Limite dichiarato: il dizionario in memoria
cresce con i path distinti falliti e non viene mai ripulito — una voce per (path, errno) di
un albero di dimensioni normali, da valutare solo se una dest negata produce milioni di
path distinti.

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

## 13. Piano di implementazione (T1..T9)

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
| **T9** | Portabilità POSIX/Linux 0.3.0 (§14): helper `safekeep/platform.py` unico lettore di `sys.platform`, `-m <monitor>` per piattaforma in `fswatch_argv`, `systemd/safekeep.service` (template) + ramo `uname -s` in `install.sh`/`uninstall.sh`, `doctor` per OS (hint distro, monitor per OS, TCC/plist solo Darwin, limite inotify non fatale), `fswatch` nei job Linux di CI, docs | suite verde su macOS **e** su Linux (CI Debian + E2E Omarchy/Arch): argv con monitor di piattaforma, `doctor` exit 0 con fswatch reale su Linux, unit renderizzata, `touch` sul sorgente → file sul dest entro 10s | ✅ fatto (`tests/test_platform.py`, `tests/e2e_linux.sh`, CI Debian + E2E Omarchy/Arch) |

Ordinamento: T1→T2 (fondamenta), T3→T4 (nucleo sync), T5 (daemon), T6 (operatività),
T7 (indurimento). Ogni task è verificabile in isolamento. Test: `python3 -m unittest
discover -s tests`.

---

## 14. Portabilità POSIX/Linux (0.3.0)

**Scope**: macOS resta la piattaforma primaria; 0.3.0 aggiunge il supporto pieno a
**Linux (POSIX con systemd)** — daemon, doctor, installazione dell'agent, CI e docs.
Il design è qui, il codice lo implementa: nessun pezzo qui sotto va scritto *dopo* il
codice.

**Onestà su cosa è testato**: macOS reale (suite locale + daemon launchd reale); Linux =
**CI su Debian/Ubuntu** (suite completa con `fswatch` reale installato) + **E2E su
Omarchy/Arch** (VM Linux reale dell'utente: suite, `doctor`, sync one-shot, propagazione
degli eventi). Non coperto: altre distro (solo hint di installazione), systemd senza
sessione di login (linger, §14.3), architetture non x86_64.

### 14.1 Rilevamento piattaforma

Un **solo helper centralizzato**, `safekeep/platform.py`: è l'unico posto che legge
`sys.platform`. Nessun check sparsi nei moduli.

| Simbolo | Valore |
|---|---|
| `is_darwin()` / `is_linux()` | `sys.platform == 'darwin'` / `sys.platform == 'linux'` |
| `fswatch_monitor()` | `fsevents_monitor` (Darwin) \| `inotify_monitor` (Linux) |
| `install_hint()` | comando di installazione di `fswatch` per la distro (`brew` / `pacman` / `apt` / `dnf`) |
| `init_system()` | `launchd` (Darwin) \| `systemd` (Linux) |
| `INOTIFY_LIMIT_PATH` | `/proc/sys/fs/inotify/max_user_watches` |
| `INOTIFY_MIN_WATCHES` | `16384` |

Tre funzioni/tre costanti, tutto qui. I valori sono letti **a chiamata** (non
all'import), così i test patchano `sys.platform` ed esercitano entrambi i rami anche
girando su macOS. Consumatori: `daemon.fswatch_argv()` (§14.2) e `doctor` (§14.4).

### 14.2 fswatch: monitor dichiarato per piattaforma

```
fswatch -0 -m <monitor> -r -l 1.0 -e <regex> -- <roots>
```

| `<monitor>` | Piattaforma | equivale a |
|---|---|---|
| `fsevents_monitor` | Darwin | FSEvents (File System Events, API di notifica di macOS) |
| `inotify_monitor` | Linux | inotify (subsystem del kernel Linux) |

- I due nomi sono **espliciti entrambi**: nessun default implicito di fswatch (fswatch
  da solo sceglierebbe comunque il monitor giusto, ma l'argv qui è identico e
  prevedibile su tutte le piattaforme).
- I nomi sono quelli che `fswatch -M` (`--list-monitors`) stampa sulla piattaforma.
  Attenzione: su Linux il nome è **`inotify_monitor`**, *non* `inotify` — `-m inotify`
  non è un alias accettato e fswatch esce con errore.
- **Invariato**: `-0` (NUL-delimitato), `-e`, `-r`, `-l 1.0`, le regex di esclusione e
  il resto del flusso di §6.

### 14.3 Init system: launchd (macOS) → systemd (Linux)

| | Darwin | Linux |
|---|---|---|
| init system | `launchd` | `systemd` (**user unit**, niente root) |
| template | `launchd/com.safekeep.agent.plist` | `systemd/safekeep.service` |
| installato in | `~/Library/LaunchAgents/com.safekeep.agent.plist` | `~/.config/systemd/user/safekeep.service` |
| avvio al boot | `RunAtLoad` | `WantedBy=default.target` |
| tenerlo vivo | `KeepAlive` (+ `ThrottleInterval` 30s) | `Restart=always` + `RestartSec=2` + `StartLimitIntervalSec=0` |
| stdout/stderr | file in `~/.local/state/safekeep/` | journal di systemd (`journalctl --user -u safekeep`) |
| load / unload | `launchctl bootstrap` / `bootout` | `systemctl --user enable --now` / `disable --now` |
| validazione | `plutil -lint` | (nessuna: la sintassi la controlla `systemctl` a load) |

- Entrambi i file sono **template** con placeholder `__REPO__` e `__PYTHON__`
  (come il plist: nessun path assoluto nel repo, così il repo si può spostare),
  resi da `install.sh`.
- `Restart=always` + `RestartSec=2` ≈ `KeepAlive`: se il processo muore systemd lo
  riavvia dopo 2s.
- `StartLimitIntervalSec=0` disabilita il rate-limit di default di systemd (5 start
  in 10s → unit in stato `failed` e nessun riavvio più): senza, un daemon che muore
  ripetutamente finirebbe irrimediabilmente in `failed`, mentre launchd con
  `KeepAlive` ci riprova sempre. Il pacing resta al daemon stesso (`MAX_DEATHS` in
  `DEATH_WINDOW`, §6).
- **`install.sh` / `uninstall.sh` ramificati su `uname -s`**: Darwin = comportamento
  attuale **invariato** (render del plist, nessun `bootstrap`); Linux = render della
  unit + `systemctl --user daemon-reload` + istruzioni stampate. Nessuno dei due script
  **avvia** l'agent: serve prima una `~/.safekeep` con dest reali (stessa regola del
  plist di macOS). Il plist macOS non viene toccato.
- **Boot senza login**: una *user unit* parte solo con una sessione utente (o con il
  linger). Per averla al boot senza nessun login serve
  `loginctl enable-linger $USER` — **documentato, non automatizzato**: richiederebbe
  `sudo` e gli script non chiedono mai password.

### 14.4 `doctor`

| Check | Darwin | Linux |
|---|---|---|
| hint installazione `fswatch` | `brew install fswatch` | `pacman -S fswatch` (Arch/Omarchy) / `sudo apt install fswatch` (Debian/Ubuntu) / `sudo dnf install fswatch` (Fedora) — rilevato dal package manager presente |
| monitor atteso in `fswatch -M` | `fsevents_monitor` (fatale) | `inotify_monitor` (fatale) |
| probe TCC (`~/Documents`) | ✔ | **assente** (TCC è un meccanismo di macOS) |
| lint plist (`plutil -lint`) | ✔ | **assente** (`plutil` su Linux non esiste: già degradava a "lint saltato") |
| agent dell'init system | plist (se presente) | unit systemd (se presente), riga informativa |
| limite inotify | — | `fs.inotify.max_user_watches` letto da `/proc/sys/fs/inotify/max_user_watches` |
| errori di permesso recenti nel log | ✔/✗ | ✔/✗ |
| exe del daemon ancora su disco (0.5.1) | ✔/✗/saltato | — |
| linger (solo con unit installata) | — | ✔/✗ |

- Il check sul limite inotify è **informativo e non fatale**: ✗ + hint
  `sudo sysctl -w fs.inotify.max_user_watches=524288` se il valore è **< 16384**
  (`INOTIFY_MIN_WATCHES`). Perché esiste: su alberi grandi (~25k file) il limite di
  watch di inotify è il rischio #1 — raggiunto, fswatch non registra più watch su
  quei path e gli eventi smettono di arrivare (le copie riprendono solo al prossimo
  reconcile, §10.1). File assente o illeggibile → check saltato con riga ✔ (non è
  Linux, o non è un kernel con inotify).
- **Errori di permesso recenti nel log (0.3.1)**: `doctor` legge il log **corrente**
  `~/.local/state/safekeep/safekeep.log` (niente rotazioni `.1`…`.5`) e conta le righe
  che sono errori di permesso — regex `\[Errno (?:1|13)\]|Operation not permitted|
  Permission denied`, quindi copre sia `EPERM` (`[Errno 1] Operation not permitted`)
  sia `EACCES` (`[Errno 13] Permission denied`) — **e** il cui timestamp cade nelle
  ultime **24h**. Il timestamp è il prefisso della riga nel formato `%(asctime)s`
  (`YYYY-MM-DD HH:MM:SS`, prime 19 posizioni). Esito: conteggio `> 0` → **warning
  non fatale** con conteggio, ultimo timestamp e hint (vedi sotto); `0` → riga ✔;
  log assente o illeggibile → check **saltato** con riga ✔ (nessun log = niente da
  controllare). **Limite dichiarato**: una riga il cui timestamp non si parsifica
  (o che è fuori dalla finestra, comprese le timestamp future per clock skew) **non
  viene conteggiata** — non essendo sicuri che sia recente, non la contiamo; il
  conteggio è quindi un minimo, non un massimo. Perché esiste: segue direttamente
  l'incidente EPERM (223 file copiabili solo dopo aver riconcesso i permessi al
  volume) — quegli errori finiscono nel log e senza questo check l'unico modo per
  vederli era leggere il file a mano. Hint stampati col warning: `safekeep sync-once
  --dry-run` per vedere i pending, riconcedere i permessi al volume / FDA (Full Disk
  Access), path del log. Finestra e non-fatalità volute: è un sintomo recente, non
  uno stato del sistema. **Throttle (0.5.1)**: le copie fallite ripetute per lo stesso
  (path, errno) non scrivono più una riga ciascuna (§10.1) — prima riga come sempre,
  poi al massimo una ogni 300s col suffisso `— ripetuto N volte`. Per non ingannare il
  conteggio, `recent_permission_errors` somma **N + 1** per ogni riga con il suffisso
  (la riga stessa + i N fallimenti compressi): `count_24h` resta il numero **vero** di
  fallimenti nella finestra, sono solo le righe scritte sul file a essere di meno.
  Riga senza suffisso → 1, come prima.
- **exe del daemon ancora su disco (0.5.1, Darwin-only)**: se il job
  `com.safekeep.agent` è caricato, `daemon_exe()` legge il `pid` da
  `launchctl print gui/$UID/com.safekeep.agent` e l'eseguibile assoluto da
  `ps -p <pid> -o comm=`; se quel path **non esiste più** su disco → warning **forte ma
  non fatale** con hint `launchctl kickstart -k gui/$(id -u)/com.safekeep.agent`
  (caso dell'incidente §8.4: upgrade Homebrew sotto un daemon vivo). Perché non fatale:
  è un sintomo con rimedio manuale di una riga, e `doctor` deve restare eseguibile
  proprio mentre diagnostica quello stato (diagnostica, non monitor: stessa
  non-fatalità del check errori di permesso, che è il lato sintomo della stessa
  medaglia). Lettura **solo** (`launchctl print` + `ps`): nessuna sessione utente
  richiesta, niente scritture — coerente con "nessun check systemctl live" qui sotto.
  Job non caricato, processo non trovato, `launchctl`/`ps` assenti o falliti,
  non-Darwin → check **saltato** con riga ✔ (non determinabile ⇒ nessun allarme).
  Funziona anche con un plist live renderizzato col vecchio default: guarda il
  **processo**, non il file.
- **linger (0.3.1, solo Linux con unit installata)**: se
  `~/.config/systemd/user/safekeep.service` esiste, `doctor` verifica che l'utente sia
  in `/var/lib/systemd/linger/` (fonte di verità di systemd, leggibile senza root —
  l'equivalente umano è `loginctl show-user $USER -p Linger`). Non abilitato →
  **warning non fatale**: "l'agent non partirà al boot senza login" + hint
  `loginctl enable-linger $USER` (§14.3: documentato, non automatizzato). Directory
  `/var/lib/systemd/linger/` assente (niente systemd) → check saltato con riga ✔.
  Unit assente o non-Linux → il check **non compare** (stessa regola del resto della
  sezione: i check Linux esistono solo dove ha senso).
- **WSL (0.4.0)**: le tre righe aggiunte per WSL (rilevamento, hint systemd,
  info `/mnt/` drvfs) sono tutte **informative e non fatali** — scope, limiti e
  test in §17.
- Nessun check `systemctl` live in `doctor`: il daemon deve poter girare anche in un
  container senza sessione utente. Lo stato dell'agent si guarda con
  `systemctl --user status safekeep`.

### 14.5 Invariati (lo dichiara questa SPEC)

Con Linux **non cambiano**:

- **matcher / config / copier / volumes**: nessun file di codice dipende dalla
  piattaforma; `dest_state` resta `os.path.exists` (§8.3, niente parsing di
  `/proc/mounts`);
- **auto-discovery** (§4.1): il pruning dedup già esclude tutte le directory
  **nascoste**, quindi su Linux copre `.config`, `.local`, `.cache`, `snap` senza
  cambi di codice; le cartelle TCC di §4.1 restano nella lista di esclusione — su
  Linux non esistono e sono inerti;
- **layout** dei path dest (§3), **semantica allow-list** e last-match-wins (§4.3),
  stability check e copia atomica (§7), anti-flood (§10.2), pending/backoff (§8.3);
- **`~/.safekeep` / `.sync`**: stesso formato, stessa semantica, stessi errori.

### 14.6 CI

I job `test` di `.gitlab-ci.yml` e `.github/workflows/test.yml` installano **fswatch**
(`apt-get update && apt-get install -y fswatch`): ora che `doctor` cerca il monitor
della piattaforma giusta la suite gira con il binario reale su Linux — che era
esattamente l'incompatibilità documentata nella vecchia nota di portabilità di
`.gitlab-ci.yml` (nota **aggiornata**, non contraddetta). I job `publish` e
`publish-test` restano invariati.

**Matrix delle versioni di Python (0.3.1)**: entrambi i job `test` girano su **tre
gambe** — `3.9` (floor dichiarato da `requires-python`), `3.12` e `3.14`:

- `.github/workflows/test.yml`: `strategy.matrix.python-version: ['3.9', '3.12',
  '3.14']` con `actions/setup-python` sulla gamba corrente; lo step E2E
  (`tests/e2e_linux.sh`, daemon live + fswatch reale) gira su **tutte** le gambe,
  floor incluso.
- `.gitlab-ci.yml`: `parallel: matrix` sulle stesse tre versioni con le immagini
  `python:3.9-slim` / `python:3.12-slim` / `python:3.14-slim`, e lo step E2E
  aggiunto al job `test`. Le immagini `-slim` non contengono `procps` (quindi niente
  `pgrep`, che lo script E2E usa per il figlio del daemon): il job installa
  `procps` insieme a `fswatch`.

Così i classifier Python di §14.7 (`3.9`, `3.12`, `3.14`) sono coperti **anche dalla
CI**, non più solo dalla suite locale dell'utente.

**Ramo WSL eseguito in CI**: fin da 0.4.0 §17 dichiarava che WSL non era verificabile
in locale (nessuna macchina WSL). Entrambi i job `test` aggiungono quindi uno step
`WSL_DISTRO_NAME=SafekeepCITest bash tests/e2e_linux.sh --doctor-only` → grep sul
marker `WSL rilevato: SafekeepCITest` → exit 0: sull'env del runner `is_wsl()` diventa
`True` e `doctor` percorre **davvero** il blocco WSL (rilevamento + hint systemd +
drvfs), non un test che lo finge. `--doctor-only` è il flag aggiunto a
`tests/e2e_linux.sh`: crea il sandbox (source/dest/config tmp — un `--config`
inesistente è un check fatale), lancia `doctor` e si ferma senza avviare il daemon.
Gate = marker presente **e** exit 0: i tre check WSL sono non fatali (§17.2), quindi
un check fatale qualsiasi (config, fswatch, monitor, python) rende comunque lo step
rosso. Nessuna dipendenza nuova: solo python3 + `bash`/`grep`/`mktemp` e il `fswatch`
che i job installano già.

### 14.7 Metadata release (`pyproject.toml`)

Le metadata PyPI di un upload sono **immutabili**: ciò che cambia in `pyproject.toml`
si vede su pypi.org solo dal prossimo upload, mentre i rilasci già pubblicati (0.2.0)
restano con le vecchie. Per 0.3.0 quindi, una tantum:

- **`[project.urls]`**: `Repository` = `https://github.com/foxhound87/safekeep`
  (GitHub è il repo primario e pubblico), `Homepage`/`Documentation` = sito docs su
  GitHub Pages `https://foxhound87.github.io/safekeep/` — **nessuna voce GitLab**;
- **keywords**: rinnovate attorno al nuovo scope (backup, macos, linux, fswatch, sync,
  allow-list, launchd, systemd);
- **classifiers**: OS `MacOS` + `POSIX` + `POSIX :: Linux`, Python **solo** le versioni
  effettivamente eseguite dalla suite (3.9 floor + 3.12 CI + 3.14 locale).

---

## 15. Decisioni aperte

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

## 16. Versione

- **0.1.0** — semver: prima versione pre-release/0.x, API di config e CLI soggette a cambi
  incompatibili solo con bump di MINOR finché non si raggiunge 1.0.0.
- **0.3.0** — supporto Linux (POSIX con systemd): §14 — helper di piattaforma, monitor
  fswatch per OS, user unit systemd, `doctor` per OS, `fswatch` in CI, docs.
  - Metadata PyPI rinnovate per la release (`pyproject.toml`): `[project.urls]` con
    `Repository` = `https://github.com/foxhound87/safekeep` e `Homepage`/`Documentation` =
    sito docs su GitHub Pages `https://foxhound87.github.io/safekeep/` (niente voci GitLab),
    keywords aggiornate (backup, macos, linux, fswatch, sync, allow-list, launchd, systemd),
    classifier OS (`MacOS`, `POSIX`, `POSIX :: Linux`) e Python (`3.9` floor, `3.12` CI,
    `3.14` locale). I metadata su PyPI sono immutabili dopo l'upload: questi cambi si
    vedono da `0.3.0`, mentre `0.2.0` resta con le vecchie.
- **0.3.1** — `doctor` + CI: due nuovi check **non fatali** (errori di permesso nel
  log corrente nelle ultime 24h, e linger dell'utente dove c'è la unit systemd —
  §14.4) e **matrix Python** 3.9 / 3.12 / 3.14 in entrambi i job `test`, con lo step
  E2E su tutte le gambe (§14.6).
- **0.4.0** — WSL (Windows Subsystem for Linux) — §17: `is_wsl()`/`wsl_distro()`,
  tre nuovi righe informative in `doctor` (rilevamento, hint systemd, info
  `/mnt/` drvfs) e `install.sh` gentile senza systemd. **Non verificato su WSL
  reale** (nessuna macchina WSL disponibile): test con env/kernel finto.
- **0.5.0** — `doctor --json` (§9.1) + wrapper trend (§9.2): la diagnostica
  esce come **un documento JSON su stdout** (stessi check, stesso exit code,
  `permission_errors` strutturato) e `bin/safekeep-trend.sh` appende ogni ora
  `timestamp,exit,count_24h,last` a
  `~/.local/state/safekeep/permission-trend.csv` (header idempotente; agent
  launchd `com.safekeep.trend` con `StartInterval` 3600, render+bootstrap di
  `install.sh --trend` che **non tocca** il daemon principale). CI: step WSL
  eseguito davvero (§14.6) e `actions/checkout@v7` + `actions/setup-python@v7`
  (runtime `node24`, niente warning di deprecazione Node 20).
- **0.5.1** — fix (PATCH) tre problemi reali dell'incidente EPERM (§8.4):
  1. `install.sh` rende `__PYTHON__` = `/usr/bin/python3` (shim di sistema
     stabile, validato ≥ 3.9 con fallback nel `PATH`) invece del path Cellar di
     Homebrew — plist agent e trend non rompono più a ogni `brew upgrade`
     (§8.4; (b) wrapper shell rifiutata, motivazione in §8.4);
  2. nuovo check `doctor` **non fatale** (Darwin, se il job è caricato): il
     processo deve girare su un eseguibile **ancora presente su disco** —
     warning forte con hint `launchctl kickstart -k` se l'exe è stato
     cancellato sotto un processo vivo (§14.4);
  3. throttle delle righe `copia fallita` ripetute per (path, errno): una riga
     + `— ripetuto N volte` ogni 300s, e `recent_permission_errors` somma N+1
     così `count_24h` resta il conteggio vero (§10.1 e §14.4).
- Ogni modifica successiva di `package`/manifest rispetta `MAJOR.MINOR.PATCH`
  (semver, https://semver.org).

---

## 17. WSL — scope implementato (0.4.0)

**Cosa c'è**: rilevamento + degradazione graziosa. **Cosa non c'è**: codice
verificato su WSL reale — **nessuna macchina WSL disponibile, quindi nessun
test su WSL reale**; tutto quello che segue è coperto da test con env/kernel
finto e da review del codice. Il runtime che WSL eredita è il Linux di 0.3.0
(§14), già **E2E-verifyto** su Linux reale (CI Debian + E2E Omarchy/Arch): il
sync non cambia.

### 17.1 Rilevamento (`safekeep/platform.py`)

| Simbolo | Comportamento |
|---|---|
| `is_wsl()` | `True` se l'env `WSL_DISTRO_NAME` è settato **oppure** `platform.release()` contiene `microsoft` (kernel WSL1/WSL2) |
| `wsl_distro()` | valore di `WSL_DISTRO_NAME`, stringa vuota se assente |

Entrambi si leggono **a chiamata** (come tutto il modulo, §14.1): i test
patchano l'env e `platform.release()`. `sys.platform` su WSL resta `linux`,
per questo il rilevamento non può passare da lì.

### 17.2 `doctor` — tre righe, tutte informative e non fatali

1. **rilevamento**: `WSL rilevato: <distro>` (o `sconosciuta` se l'env manca e
   è il kernel a dirlo).
2. **systemd user assente** — `/run/systemd/system` non esiste (WSL1, oppure
   WSL2 con systemd spento): hint non-fatale "abilita systemd in
   `/etc/wsl.conf`":
   ```ini
   [boot]
   systemd=true
   ```
   poi `wsl --shutdown` da Windows — **oppure** esegui `safekeep run` a mano
   (fallback senza agent).
3. **source/dest sotto `/mnt/`** → info **drvfs**: I/O lento (traduzione 9p),
   case-insensitive di default — valuta una dest su ext4 nativa della distro
   invece di `/mnt/c/...`.

Nessuno dei tre è fatale: `doctor` esce 0 anche su WSL1 senza systemd.

### 17.3 Init e installazione

- **WSL2 con systemd attivo**: la *user unit* di §14.3 si comporta come su
  Linux (`install.sh` fa render + `systemctl --user daemon-reload`), linger
  compreso (§14.4).
- **Senza systemd** (WSL1 / systemd off): `install.sh` **non installa la unit**
  e termina con **exit 0** dopo aver stampato le istruzioni di avvio manuale
  (`safekeep run`) — degradazione graziosa, non errore duro.
- **Path**: dest/source sotto `/mnt/<lettera>/` = drvfs (vedi §17.2.3).

### 17.4 Limiti dichiarati

- **Nessun test su WSL reale** (nessuna macchina WSL disponibile): detection,
  marker di `doctor` e branch di `install.sh` testati con env/kernel finto; un
  E2E su WSL no.
- Eventi su `/mnt/*` (drvfs/9p) e fswatch su WSL: non documentati qui, da
  verificare quando ci sarà una macchina WSL.
