# SPEC.md — safekeep

**Versione:** 0.10.0 (semver — Semantic Versioning, https://semver.org)
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
| `dest` | path (ripetibile) | **obbligatoria\*** | destinazioni di **tutti** i progetti: le uniche esistenti (i `.sync` non possono aggiungerne o rimuoverne, vedi §4.2 e §11); \* serve **almeno una `dest:` oppure `s3_bucket:`** (dest S3-compatible, §18). Forma nominata `dest <name>: <path>` (0.11.0): la dest è citabile nelle `route` (§21) e in `restore --dest <nome>` |
| `dest <name>` | path (ripetibile) | opzionale | (0.11.0) destinazione **nominata**: stessa semantica della `dest:` anonima, ma il `<name>` la rende referenziabile nelle route (§21.2) e nei comandi; le dest anonime restano valide ma non sono citabili in route |
| `route` | `src → dest-list` (ripetibile) | opzionale | (0.11.0) instrada la sorgente `src` su una o più destinazioni (nomi e/o target S3 `s3:<prefix>` / `s3://<bucket>/<prefix>`); **longest-prefix-wins** sulla source root; sorgente senza route → tutte le dest (default retrocompat); vedi §21 |
| `layout` | `relative` \| `full` | `relative` | layout dei path di destinazione (vedi nota sotto) |
| `defaults` | lista regole | builtin | regole di default a **bassa priorità** (sotto le regole globali e sotto `.sync`) |
| `log_level` | `debug`\|`info`\|`warn`\|`error` | `info` | livello di log su stderr + file di log |
| `poll_interval` | intero ≥ 1 | `60` | secondi fra due turni del monitor a **polling** per le root su volume di rete (§20): SMB/CIFS/NFS non emettono eventi a fswatch |

Note:

- `source` e `dest` sono **ripetibili**: più righe = più valori.
- **Due modalità**, decise dalla presenza di `source`:
  - **modalità source** (una o più righe `source:`): le radici watch sono le
    `source` e il comportamento è quello storico (backward compat);
  - **auto-discovery** (nessuna riga `source:`): watch root = `$HOME`, scan di
    discovery alla ricerca dei `.sync`, `.sync` creato dopo l'avvio agganciato
    da solo (vedi §6). Una config senza `source` **non** è un errore.
- **`dest` è obbligatoria** (a meno che non sia configurata una dest S3:
  `s3_bucket`, §18): nessuna riga `dest:` e nessun `s3_bucket` → `ConfigError`
  con hint (le destinazioni vivono solo qui, §4.2). Nessuna `source` invece
  non è un errore.
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
- **Routing destinazioni** (0.11.0, §21): le chiavi `dest <name>:` e `route`
  controllano **quale** destinazione riceve **quale** sorgente. Sorgente
  senza route → tutte le destinazioni (anonime + nominate + S3), identico a
  0.10.0. Il routing vive **solo** qui, mai nel `.sync` (§4.2/§11).

Esempio completo di routing (copy-paste — destinazioni nominate, anonime,
legacy e route S3):

```ini
# destinazioni filesystem nominate (citabili nelle route)
dest nas: /Volumes/NAS/backup
dest ext: /Volumes/EXFIT/safekeep-backup
# destinazione anonima legacy (ancora valida, non citabile in route)
dest: /legacy/anonymous/path

# S3 globale legacy (ancora valida): bucket di default per le route s3:<prefix>
s3_bucket: my-bucket
s3_access_key: AKIA...
s3_secret_key_env: SAFEKEEP_S3_SECRET

# routing sorgente → destinazioni (longest-prefix-wins, §21.3)
route ~/projects → nas
route ~/Documents/notes → ext, nas
route ~/code → s3:backup/code              # bucket my-bucket, prefisso backup/code
route ~/archive → s3://other-bucket/old    # bucket ESPlicito diverso da s3_bucket
route ~ → nas                              # default per tutto ciò che non ha route più specifica
```

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
   fswatch -0 -m <monitor> -r -l 1.0 -E -e <regex esclusioni> -- <radici watch>
   ```

   - **radici watch**: le `source` del config (modalità source, deduplicate: una
     radice annidata sotto un'altra già coperta viene saltata — `fswatch -r`
     copre già il sottoalbero), oppure `$HOME` (modalità auto-discovery). **Split
     0.9.0 (§20.1)**: a ogni avvio/rescan/reload le radici vengono classificate
     con `is_network_fs` — le **locali** passano a fswatch invariato, le **di
     rete** vengono escluse dall'argv e passano al monitor a polling (fswatch
     NON riceverebbe eventi da un mount di rete); **zero radici locali →
     fswatch non viene spawnato**;
   - `-0`: separatore NUL (path con spazi/newline sicuri);
   - `-m <monitor>`: monitor nativo **dichiarato esplicitamente per piattaforma**
     (mai il default implicito di fswatch): `fsevents_monitor` su Darwin
     (FSEvents = File System Events, API di notifica filesystem di macOS) e
     `inotify_monitor` su Linux (inotify, subsystem del kernel Linux) — §14.2;
   - `-r`: ricorsivo;
   - `-l 1.0`: batching di almeno 1s (anti-flood a monte);
   - `-E` (**requisito, §6.4**): le `-e` vanno lette come **ERE (Extended Regular
     Expressions)**, non come BRE (Basic Regular Expressions) che è il default di
     fswatch. Senza `-E` i costrutti estesi — gruppi `(...)`, alternanze `|` —
     sono letterali e **nessuna esclusione filtra più nulla**: con un `.sync` in
     `$HOME` ogni evento `Library/...`/`.local/...` arriva al matcher, `classify`
     risponde `walk_dir` e il daemon satura senza processare mai l'evento di
     reload della config. Le regex servono proprio a descrivere path reali —
     espressioni con gruppi/alternanze e path con spazi (es. `VAULT CLA/`, già
     reso sicuro dal separatore NUL di `-0`) — costrutti che esistono solo in ERE;
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

**Template (0.7.0)**: `safekeep/data/com.safekeep.agent.plist.tmpl` — package
data, sorgente unica; `safekeep daemon install` lo renderizza in
`~/Library/LaunchAgents/com.safekeep.agent.plist` (§14.3). Placeholder:
`__EXEC__` (shim assoluto `safekeep` sul PATH), `__HOME__`, `__PATH__`
(dir dove riside fswatch + path di sistema — launchd non eredita il PATH di
login). **Nessun interprete nel template**: `ProgramArguments` è lo shim →
`run` (vedi §8.4).

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
        <string>__EXEC__</string>
        <string>run</string>
    </array>

    <!-- Avvio al boot -->
    <key>RunAtLoad</key>
    <true/>

    <!-- Tenerlo vivo: se muore, launchd lo riavvia -->
    <key>KeepAlive</key>
    <true/>

    <!-- Niente restart-loop frenetico: al massimo un riavvio ogni 30s -->
    <key>ThrottleInterval</key>
    <integer>30</integer>

    <!-- Background: non compete con l'interfaccia (CPU) -->
    <key>ProcessType</key>
    <string>Background</string>

    <!-- ...né con altre operazioni di I/O -->
    <key>LowPriorityIO</key>
    <true/>

    <!-- Log del daemon: ~/.local/state/safekeep/ (§3) — launchd NON espande ~ -->
    <key>StandardOutPath</key>
    <string>__HOME__/.local/state/safekeep/out.log</string>
    <key>StandardErrorPath</key>
    <string>__HOME__/.local/state/safekeep/err.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
        <!-- fswatch deve trovarsi nel PATH del processo -->
        <key>PATH</key>
        <string>__PATH__</string>
        <!-- s3_secret_key_env (§18): decommenta per esporre la variabile -->
        <!-- <key>SAFEKEEP_S3_SECRET</key><string>REPLACEME</string> -->
    </dict>
</dict>
</plist>
```

Validazione: `plutil -lint ~/Library/LaunchAgents/com.safekeep.agent.plist`
(eseguita dal comando `daemon install` stesso). Opzione `StartOnMount`
commentata non inclusa nel template: vedi §8.3.

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
   Dal **0.9.0** (§20.4) il pending si attiva anche per gli **errori di rete**
   (`NETWORK_ERRNOS`: ENOTCONN/EHOSTUNREACH/ETIMEDOUT/ENETUNREACH/ENETDOWN/
   ESTALE), oltre ai classici `MOUNT_ERRNOS` (ENODEV/EBUSY/ENOSPC): una dest su
   NAS con il server morto va in pending/backoff, non in log-loop.
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

Sintomo e log ribollente erano conseguenze, non la causa: la causa era l'exe cancellato sotto
un processo vivo. Il check `doctor` "errori di permesso recenti" (§14.4) vedeva il sintomo
ma non poteva vedere la causa — per questo 0.5.1 aggiunge anche il check exe (§14.4).

**Fix (0.5.1): `__PYTHON__` = `/usr/bin/python3`.** `install.sh` rende ora di default lo
**shim di sistema** `/usr/bin/python3` (Apple-gestito, non toccato dagli upgrade Homebrew,
già l'interprete che §11 raccomanda per il consenso FDA), validato **≥ 3.9** eseguendolo
(`py_ok`); shim assente o troppo vecchio → fallback a `python3` nel `PATH`
(`SAFEKEEP_PYTHON` resta l'override esplicito, come prima). Lo stesso `__PYTHON__`
alimenta `SAFEKEEP_PYTHON` del plist trend (§9.2), quindi **entrambi** gli agent sono
coperti dalla stessa scelta.

**0.7.0 — l'interprete esce dal plist principale**: il rendering è passato a
`safekeep daemon install` (§14.3) e `ProgramArguments` è ora **lo shim `safekeep`
(`__EXEC__`) → `run`** (§8.1): il plist non nomina più nessun interprete, quindi
la classe "exe sparito sotto un processo vivo" non si applica più all'agent
principale (lo shim console-script di pipx è uno shebang che sopravvive a
`pipx reinstall`; TCC guarda il processo, che resta identificabile). `__PYTHON__`
resta in uso **solo** per l'agent trend (`install.sh --trend`, §9.2, fuori scope
di `safekeep daemon`) — lì la scelta 0.5.1 continua a valere.

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
3.9.6; la suite è verde anche girata con quell'interprete). **Nota 0.7.0**: questo
paragrafo riguarda ora **solo l'agent trend** (`__PYTHON__`, §9.2) — il plist
principale non nomina più interprete (§8.1).

**Plist live già installati:** launchd **non rilegge** il file fino a `bootout`+`bootstrap`
(o reboot): i plist renderizzati col vecchio default restano fragili finché non vengono
re-renderizzati. `safekeep daemon install` è idempotente (sostituisce anche unit con
placeholder residui, con riga informativa); re-render + ricarica:

```bash
safekeep daemon install  # re-render del plist (idempotente, non avvia)
safekeep daemon stop     # bootout se caricato (exit 1 se non lo era — ok)
safekeep daemon start    # bootstrap + avvio
./install.sh --trend    # trattamento trend invariato (legacy, §9.2)
```

Verifica: `ps -p <pid> -o comm=` deve mostrare un exe **esistente su disco**
(la process image dello shim → python del venv pipx) e
`launchctl print gui/$(id -u)/com.safekeep.agent | grep -E 'state|program'`.

---

## 9. Comandi CLI

```
bin/safekeep.py <comando> [--config PATH] [--project PATH] [--json] [-v]
```

| Comando | Cosa fa | Exit code |
|---|---|---|
| `run` | daemon: reconcile iniziale + watch fswatch + dispatch eventi + timer 24h | 0 su SIGTERM pulito; 1 se la config è invalida o una dest è dentro la sorgente/$HOME |
| `sync-once` | un singolo passaggio: walk sorgente, copia ciò che differisce, esce; `--prune` rimuove in più dalla dest i file non più inclusi (vedi sotto) | 0 se tutto ok; 1 se la config è invalida o una dest è dentro la sorgente/$HOME |
| `restore` | ripristina dall'**ultima copia** in una dest (§19): dry-run **sempre** il default, `--force` per scrivere; selezione via `PATH`/`GLOB` o menu interattivo; `--dest <nome>` (dest nominata, 0.11.0, §21.4) con precedenza su `--from {local,s3,nas}`; auto-detect del candidato unico | 0 se tutto ok; 1 se la config è invalida, l'origine del ripristino non è determinabile o nessun argomento matcha |
| `status` | sola lettura: config path, modalità (`source` / auto-discovery da `$HOME`), source, progetti scoperti con N regole, ogni dest con `dest_state` (ok/absent) | 0 se la config è valida |
| `doctor` | diagnostica: config, dest non sotto source, fswatch + monitor di piattaforma, python ≥ 3.9, probe TCC, lint plist ed eseguibile del processo ancora su disco (solo macOS, §14.4), residui tmp, limite inotify (solo Linux); con `--json` la stessa diagnostica come documento JSON su stdout (§9.1) | 1 se un check **fatale** fallisce |
| `daemon {install,start,stop,status,uninstall}` | gestione dell'agent di boot (§14.3): `install [--enable-linger] [--dry-run]` preflight + render della unit da template embedded (idempotente, non avvia); `start`/`stop` caricano/scaricano la unit; `status` = sola lettura; `uninstall [--purge]` rimuove la unit (`--purge` toglie anche `~/.safekeep` e i log) | 0 se tutto ok; 1 se il preflight fallisce, la unit non è installata (`start`/`status`) o l'agent non è in esecuzione (`status`) |

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

# Restore: dry-run di default (mostra le righe, NON scrive)
python3 bin/safekeep.py restore ~/Code/myapp/docs/note.md

# Restore reale: solo con --force (path assoluto, relativo o glob)
python3 bin/safekeep.py restore --force ~/Code/myapp
python3 bin/safekeep.py restore --force myapp/docs/note.md
python3 bin/safekeep.py restore --force 'myapp/docs/*.md'

# Origine esplicita (multi-dest oppure dest S3, credenziali solo dal config)
python3 bin/safekeep.py restore --from s3 --force ~/Code/myapp

# Menu interattivo (solo da terminale: niente argomenti + stdin è un TTY)
python3 bin/safekeep.py restore --force

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

**Throttle delle righe di errore (0.5.1).** La stessa copia fallita può ripetersi senza
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
  rifiutate (warning con `log_level: info`, errore con `log_level: debug`). Questo vale
  **anche per il routing** (§21.1, 0.11.0): le chiavi `route`/`dest <name>:` vivono **solo**
  in `~/.safekeep`, mai in un `.sync` — il routing non abbassa il livello di confine né
  aggiunge superficie di scrittura: la destinazione finale resta comunque una `dest`
  dichiarata qui, il routing sceglie solo *quale* di queste riceve una sorgente. Il
  containment "dest sotto source → errore fatale" e la semantica **mai-cancellare**
  restano invariati per ogni route.
- **`restore` scrive solo dentro le source** (containment **invertito** rispetto al
  CR-01 di §7.2): il target è sempre il path originale, niente `--to`, e un path
  ricostruito che esce dalla source del progetto viene rifiutato (EACCES + skip).
  **Nessuna scrittura senza `--force`** — il default è sempre il dry-run, anche in
  interattivo; nessuna cancellazione; le credenziali S3 arrivano solo dal config
  `s3_*` (§18.1), mai da flag a riga di comando (§19).
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
| Server SMB/CIFS morto o mount di rete perso | `NETWORK_ERRNOS` → dest in pending + backoff (§8.3/§20.4); sorgente su rete → `poll_pending` con catch-up al remount (§20.2) |
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
| `restore` da una dest non montata | errore con l'elenco dei candidati (§19.3): un'origine dichiarata non viene mai ignorata silenziosamente — monta il volume e riprova |
| `restore` con PATH/GLOB senza match | exit ≠ 0 con messaggio (nessun file ripristinato, nessuna scrittura parziale decisa a partire da zero match) |
| `restore` verso una cartella protetta TCC senza FDA | `EACCES`/`Operation not permitted` per-file, log + hint FDA (Full Disk Access, §11/§14.4) — il resto del piano prosegue |
| Route ambigua (stessa source root o stesso prefisso dichiarati due volte) | **fail-fast** al parse con numero di riga + elenco righe in conflitto (§21.3): nessuna precedenza implicita, l'utente deve rendere la route univoca |
| Route verso destinazione inesistente (nome non dichiarato con `dest <name>:`) | **fail-fast** al parse con numero di riga + nome mancante (§21.3), come ogni chiave invalida di §3 |
| Route su source root mai scoperta (prefisso che non corrisponde ad alcuna radice nota) | **warning non fatale** (route inutilizzata): in auto-discovery la source root può non essere ancora agganciata; al rescan 24h (§6) può diventare attiva — non è un errore |
| `route ~ → x` in auto-discovery che matcherebbe TUTTO | **consentito** (§21.3): è il modo di esprimere il default per tutto ciò che non ha route più specifiche; il *longest-prefix-wins* fa vincere le route mirate (`route ~/code → …`) sui progetti pertinenti |
| Sorgente senza route | va su **tutte** le destinazioni (anonime + nominate + S3) — default retrocompat, zero breaking (§21.1) |

---

## 13. Piano di implementazione (T1..T9)

| Task | Contenuto | Criteri di verifica | Stato |
|---|---|---|---|
| **T1** | Matcher gitignore-like (`safekeep/matcher.py`): `*` `**` `?` `dir/` `/ancora` commenti escape, last-match-wins, dir pruning | mini-suite di casi (≥20 path × regole) tutte verdi; caso "exclude batte include" e "include più basso ri-include" passano | ✅ fatto (`tests/test_matcher.py`) |
| **T2** | Parser config globale `~/.safekeep` + parser `.sync` senza dest (`safekeep/config.py`): sezioni, regole, errori fail-fast, esempi in `examples/` | parse di `examples/safekeep.example` e `examples/sync.example` produce la struttura attesa; riga invalida → errore con numero di riga; `dest:` in `.sync` → warning con info / errore con debug | ✅ fatto (`tests/test_config.py`) |
| **T3** | Copia atomica + stability check + symlink + walk/reconcile idempotente (`safekeep/copier.py`) | test su tmpdir: contenuto + mtime preservati; src instabile/sparito → `False` senza residui tmp; symlink (anche rotti e di directory) ricreati come symlink; seconda run = 0 copie | ✅ fatto (`tests/test_copier.py`) |
| **T4** | Destinazioni non montate (`safekeep/volumes.py`): `dest_state`, `backoff_schedule()` (1→2→…→60 cap), `partition_dests` | `dest_state` su path esistente/assente; backoff con cap a 60s; split (ready, absent) | ✅ fatto (`tests/test_volumes.py`) |
| **T5** | `run`: subprocess fswatch (`-0 -m fsevents_monitor -r -l 1.0 -E -e …`), dispatcher eventi, debounce, timer 24h, pending/backoff mount, anti-flood (>5000 → collapse a reconcile) | modificare un file sorgente → compare sul dest entro ~2s; dest smontata → pending, remount → catch-up; flood simulato triggera il collapse | da fare |
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
fswatch -0 -m <monitor> -r -l 1.0 -E -e <regex> -- <roots>
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
- **Invariato**: `-0` (NUL-delimitato), `-E` (ERE, §6.4 — requisito perché le `-e`
  filtrino davvero), `-e`, `-r`, `-l 1.0`, le regex di esclusione e
  il resto del flusso di §6.

### 14.3 Init system: launchd (macOS) → systemd (Linux)

| | Darwin | Linux |
|---|---|---|
| init system | `launchd` | `systemd` (**user unit**, niente root) |
| template (package data, 0.7.0) | `safekeep/data/com.safekeep.agent.plist.tmpl` | `safekeep/data/safekeep.service.tmpl` |
| installato in | `~/Library/LaunchAgents/com.safekeep.agent.plist` | `~/.config/systemd/user/safekeep.service` |
| installazione | **`safekeep daemon install`** — preflight + render + scrittura, idempotente, **non avvia** | (identico: stesso comando, ramo per piattaforma) |
| avvio al boot | `RunAtLoad` | `WantedBy=default.target` |
| tenerlo vivo | `KeepAlive` (+ `ThrottleInterval` 30s) | `Restart=always` + `RestartSec=2` + `StartLimitIntervalSec=0` |
| stdout/stderr | file in `~/.local/state/safekeep/` | journal di systemd (`journalctl --user -u safekeep`) |
| load / unload | `safekeep daemon start` / `stop` (≡ `launchctl bootstrap` / `bootout`) | `safekeep daemon start` / `stop` (≡ `systemctl --user enable --now` / `disable --now`) |
| validazione | `plutil -lint` (dal comando install) | (nessuna: la sintassi la controlla `systemctl` a load) |

- **0.7.0 — sorgente unica dei template**: i template vivono nel package
  (`safekeep/data/*.tmpl`) e **`safekeep daemon install`** (`safekeep/daemonctl.py`)
  fa preflight → render → scrittura → lint. Preflight: OS supportato, `fswatch`
  presente (hint per piattaforma), python ≥ 3.9, **shim `safekeep` sul PATH** e —
  su WSL — systemd attivo (§17.3); zero scrittura se fallisce. Placeholder
  `__EXEC__` (shim assoluto `safekeep` → ExecStart = `<shim> run`: **nessun
  interprete nel template**, sopravvive a `pipx reinstall`), `__HOME__` (launchd
  non espande `~`), `__PATH__` (dir di fswatch + path di sistema: launchd/systemd
  non ereditano il PATH di login). Unit esistente con placeholder residui →
  segnalata e sostituita. `daemon install` **non avvia** nulla: poi
  `safekeep daemon start`. I sottocomandi: `install [--enable-linger] [--dry-run]`,
  `start`, `stop`, `status` (exit 0 se in esecuzione, 1 altrimenti),
  `uninstall [--purge]` (`--purge` toglie anche `~/.safekeep` e
  `~/.local/state/safekeep/`, altrimenti restano intatti).
- **`install.sh` / `uninstall.sh` deprecati da 0.7.0** (rimozione in 0.8.0): sono
  wrapper che delegano a `safekeep daemon install` / `uninstall` — non renderizzano
  più nulla. Resta **solo** il ramo `--trend` (§9.2, fuori scope di `safekeep daemon`).
- `Restart=always` + `RestartSec=2` ≈ `KeepAlive`: se il processo muore systemd lo
  riavvia dopo 2s.
- `StartLimitIntervalSec=0` disabilita il rate-limit di default di systemd (5 start
  in 10s → unit in stato `failed` e nessun riavvio più): senza, un daemon che muore
  ripetutamente finirebbe irrimediabilmente in `failed`, mentre launchd con
  `KeepAlive` ci riprova sempre. Il pacing resta al daemon stesso (`MAX_DEATHS` in
  `DEATH_WINDOW`, §6).
- **Nessuno dei due comandi avvia l'agent**: serve prima una `~/.safekeep` con dest
  reali (stessa regola di sempre). Nota macOS: dopo l'installazione va concesso
  FDA (Full Disk Access) all'interprete dietro lo shim e a `fswatch` — il comando
  lo ricorda.
- **Boot senza login (linger)**: una *user unit* parte solo con una sessione utente
  (o con il linger). `safekeep daemon install` stampa l'hint
  `loginctl enable-linger $USER`; con **`--enable-linger`** lo esegue lui
  (best-effort: `loginctl` fallito → warning, non errore; linger = unit user
  attive anche senza login). Il check `doctor` (§14.4) resta non fatale.

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
  `loginctl enable-linger $USER` (§14.3: hint di default, automatizzato con
  `safekeep daemon install --enable-linger`). Directory
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
  `/proc/mounts`). Unica eccezione dichiarata: `platform.fs_fstype` /
  `is_network_fs` (0.8.0) leggono `/proc/mounts` (Linux) o l'output di
  `mount` (macOS) per **classificare le root** (§20.1) e etichettare le dest
  in `status` — lo stato dest resta `os.path.exists`, il parsing dei mount non
  lo tocca;
- **auto-discovery** (§4.1): il pruning dedup già esclude tutte le directory
  **nascoste**, quindi su Linux copre `.config`, `.local`, `.cache`, `snap` senza
  cambi di codice; le cartelle TCC di §4.1 restano nella lista di esclusione — su
  Linux non esistono e sono inerti;
- **layout** dei path dest (§3), **semantica allow-list** e last-match-wins (§4.3),
  stability check e copia atomica (§7), anti-flood (§10.2), pending/backoff (§8.3);
- **`~/.safekeep` / `.sync`**: stesso formato, stessa semantica, stessi errori.

### 14.6 CI

Il job `test` di `.github/workflows/test.yml` installa **fswatch**
(`apt-get update && apt-get install -y fswatch`): ora che `doctor` cerca il monitor
della piattaforma giusta la suite gira con il binario reale su Linux — che era
esattamente l'incompatibilità documentata nella vecchia nota di portabilità di
`.gitlab-ci.yml` (nota storica: il file è stato **rimosso da `main`** con la
dismissione della CI GitLab, vedi sotto). Il `publish` vive in
`.github/workflows/publish.yml`; il job GitLab `publish-test` non ha mai avuto
equivalente su GitHub (**mai** publish di test).

**Install-smoke (0.7.0)**: uno step dedicato (`Daemon install smoke` su GitHub;
era il job `install-smoke` su GitLab) esegue `bash tests/ci_daemon_install.sh` —
build della wheel da sorgenti, venv pulito + `$HOME` temporaneo →
`safekeep daemon install` → unit presente, **nessun placeholder residuo**,
config chmod 600, `doctor` best-effort. Nessuna operazione che avvii/fermi un
agent (niente bootstrap/enable/start/stop/uninstall in CI). Sul runner GitHub
systemd **c'è**: il ramo errore WSL-senza-systemd (WSL_DISTRO_NAME + systemd
assente → exit ≠ 0 con istruzioni `/etc/wsl.conf`) e il ramo
non-WSL-senza-systemd (warning + unit scritta, exit 0) si esercitano ormai
solo nei test unitari (su GitLab giravano nelle immagini `-slim`, job rimosso).
Il publish su tag non rifà la suite: il gate è la run della workflow `tests`
su `main`, che include questo step.

**Matrix delle versioni di Python (0.3.1)**: il job `test` gira su **tre
gambe** — `3.9` (floor dichiarato da `requires-python`), `3.12` e `3.14`:

- `.github/workflows/test.yml`: `strategy.matrix.python-version: ['3.9', '3.12',
  '3.14']` con `actions/setup-python` sulla gamba corrente; lo step E2E
  (`tests/e2e_linux.sh`, daemon live + fswatch reale) gira su **tutte** le gambe,
  floor incluso. (La controparte GitLab (`parallel: matrix` sulle stesse tre
  versioni, immagini `python:3.x-slim` + install di `procps`) esisteva finché
  `.gitlab-ci.yml` era su `main`: rimosso con la CI GitLab.)

Così i classifier Python di §14.7 (`3.9`, `3.12`, `3.14`) sono coperti **anche dalla
CI**, non più solo dalla suite locale dell'utente.

**GitLab CI dismesso: publish su GitHub Actions OIDC**: `.gitlab-ci.yml` è stato
**rimosso da `main`** con la dismissione della CI GitLab (a seguito
dell'eliminazione del repo GitLab): da adesso GitHub Actions è l'unico pipeline.
`.github/workflows/publish.yml`
pubblica su PyPI con Trusted Publishing OIDC (OpenID Connect — `id-token: write` +
`pypa/gh-action-pypi-publish`) al push di un tag `v*`, replicando il vecchio job
`publish` GitLab (`id_tokens` aud `pypi` + `twine` senza credenziali; il build resta
`python -m build` + `twine check`). Questa workflow è l'unico canale di release: i
gate di test restano le run della workflow `tests` su `main` (il publish su tag non
rifà la suite). I job GitLab (`test`/`install-smoke`/`publish`/`publish-test`) sono
storia: dal tag `v0.9.x` in poi il publish è GH Actions.

**Check drift SPEC ↔ `__version__` (0.9.0)**: il job `test` aggiunge uno
step che estrae l'header `**Versione:**` da `SPEC.md` e `__version__` da
`safekeep/__init__.py` e **fallisce se differiscono** (shell sola: `sed` + `test`,
zero dipendenze). Obiettivo: mai più una release con header SPEC stale (a 0.8.0
l'header era fermo a 0.5.2). Su GitHub gira su **una sola gamba**
(`if: matrix.python-version == '3.12'` — il confronto è indipendente da Python;
la controparte GitLab, una riga all'inizio del job `test`, è stata rimossa con
il file). Il bump di release deve quindi toccare `__version__` **e** lo
header della SPEC nello stesso commit, altrimenti la CI va rossa.

**Ramo WSL eseguito in CI**: fin da 0.4.0 §17 dichiarava che WSL non era verificabile
in locale (nessuna macchina WSL). Il job `test` aggiunge quindi uno step
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

**E2E SMB (0.9.0, gate GitHub-only)**: `tests/e2e_smb.sh` monta uno share
`guest` con `smbd` locale + `mount.cifs` (`vers=3.0`, fallback `2.1`) dentro una
sandbox mktemp e verifica: dest su CIFS (sync-once + idempotenza, niente residui
tmp), **sorgente su CIFS con `poll_interval: 5`** (gate vero: un `touch` sul
mount deve raggiungere la dest locale entro ~20s — ibrido poll+fswatch), le
esclusioni `.sync` sotto poll, smont/remount con `poll_pending` senza crash e
catch-up, SIGTERM pulito senza fswatch orfani. Su GitHub gira **solo sulla
gamba 3.12** (`SAFEKEEP_E2E_SMB=1` + `apt-get install samba cifs-utils`): un
test identico su tre gambe non copre nulla in più e costa tre runner. Senza
l'env lo script salta con messaggio (default locale gentile). **Divergenza
storica con GitLab** (CI dismessa, `.gitlab-ci.yml` rimosso): il docker
executor non aveva `SYS_ADMIN` → `mount.cifs` non poteva montare → su GitLab
**non girava l'e2e SMB** (stessa prassi di `pgrep`/`procps`: ciò che il runner
non può, la CI lo dichiarava nei commenti di testa e lo saltava).

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
- **0.5.2** — fix (PATCH) fswatch `-E` (§6.4): le `-e` venivano lette come **BRE
  (Basic Regular Expressions)**, dove `(`/`|` sono letterali, e di conseguenza
  **nessuna esclusione filtrava**: con un `.sync` in `$HOME` gli eventi
  `Library/...`/`.local/...` arrivavano al matcher, `classify` rispondeva
  `walk_dir` e il daemon saturava senza processare mai il reload della config
  (5,5 minuti di reconcile per tornare vivo). L'argv di fswatch passa ora `-E`
  (**ERE**, Extended Regular Expressions) con test di regressione, e §6.4/§14.2
  documentano il requisito — le regex servono a descrivere gruppi/alternanze su
  path reali, spazi compresi (`VAULT CLA/`).
- **0.6.0** — destinazione **S3-compatible** (Simple Storage Service) — §18:
  chiavi `s3_*` in `~/.safekeep` (almeno una `dest:` **oppure** `s3_bucket`),
  client **botocore** via extra `safekeep[s3]` con import lazy (runtime
  stdlib-only invariato), upload + `needs_copy` via metadata
  (`x-amz-meta-mtime` ns, `x-amz-meta-mode`, symlink = object zero-byte con
  `x-amz-meta-symlink`), errori di rete → pending/backoff §8.3, `status` e
  `doctor` con righe dedicate (tutte non fatali), secret con enforcement
  `chmod 600` + fallback `s3_secret_key_env`. **NO prune/cancellazioni su
  S3, NO versioning**; `--prune` tocca solo le dest fs.
- **0.7.0** — `safekeep daemon {install,start,stop,status,uninstall}` (§14.3, §8.1):
  gestione dell'agent di boot da comando CLI — template launchd/systemd nel
  package (`safekeep/data/*.tmpl`), preflight (fswatch, shim `safekeep` sul PATH,
  python ≥ 3.9, WSL systemd-on), ExecStart = **shim `safekeep` → `run`** (niente
  `__PYTHON__` nel plist principale, §8.4), linger con `--enable-linger`,
  `uninstall --purge`. `install.sh`/`uninstall.sh` deprecati (wrapper; il ramo
  `--trend` resta), CI con smoke di install standalone su wheel + venv pulito.
- **0.8.0** — `safekeep restore` (§19): ripristino dall'**ultima copia** in una
  dest — copy-only, niente storia, niente cancellazioni. Dry-run **sempre** il
  default, `--force` per scrivere (anche in interattivo); selezione via
  `PATH`/`GLOB` (assoluto, relativo alla radice `source`, fnmatch) o menu
  interattivo per livelli (origine ambigua → progetto → voci di primo livello);
  `--from {local,s3,nas}` con auto-detect del candidato unico (≥2 → errore con
  elenco); mapping inverso dest→target per entrambi i layout; containment
  **invertito** (scrive solo dentro le source, §11); adapter S3 `iter_keys`/
  `download` con credenziali solo dal config `s3_*`; `fs_fstype`/`is_network_fs`
  in `platform.py`; voci orfane della dest → warn + skip.
- **0.9.0** — **volumi di rete (SMB/CIFS/NAS) e monitor a polling** — §20:
  FSEvents/inotify **non emettono eventi** su mount di rete → le radici
  discovery vengono classificate (`is_network_fs`) e quelle di rete passano da
  fswatch a un monitor a polling nel loop single-process (`safekeep/poll.py`:
  snapshot `lstat` + diff a `poll_interval`, catch-up al remount via
  `poll_pending` con backoff §8.3); zero radici locali → fswatch non spawnato;
  nuova chiave `poll_interval` (default 60s, §3); `NETWORK_ERRNOS` (6 errno di
  rete) → dest in pending + backoff invece di log-loop (§20.4); `doctor`/
  `status` con righe dedicate (sorgenti su rete N poll ogni Xs, root locali M,
  dest `[nas]`/`[local]`, hint TCC `/Volumes`); CI: check drift SPEC↔`__version__`
  (§14.6) e gate E2E SMB su GitHub (samba + `mount.cifs`, gamba 3.12; GitLab
  dichiara la divergenza: docker executor senza `SYS_ADMIN`).
- **0.10.0** — flag `--if-dir-exists` per `restore` (§19.6, MINOR
  retrocompatibile): default inattivo (le parent vengono create come prima);
  con il flag un file viene ripristinato solo se la sua directory di
  destinazione immediata esiste già, altrimenti saltato — niente creazione di
  alberi di progetto non clonati sul disco. Skip visibili nel piano dry-run e
  nel conteggio dell'esecuzione, per tutte le origini (`fs` e `S3`).
- **0.11.0** — **routing destinazioni** (§21, MINOR retrocompatibile): nuove
  chiavi `dest <name>:` (destinazione **nominata**) e `route <src> → <dest-list>`
  in `~/.safekeep` (mai nel `.sync`, §4.2/§11) per instradare una sorgente su
  una o più destinazioni scelte, invece che su tutte. Matching **longest-prefix-wins**
  sulla source root; route ambigua o verso destinazione inesistente → **fail-fast**
  al parse; **sorgente senza route → tutte le dest** (default retrocompat, zero
  breaking). Route S3 per-route: `s3:<prefix>` (bucket = `s3_bucket` globale) e
  `s3://<bucket>/<prefix>` (bucket esplicito), con client S3 già cached per-bucket
  (§18). Niente rewriting path per-route: il layout dest globale resta invariato.
  `restore --dest <nome>` seleziona una destinazione nominata con precedenza su
  `--from` (§19.3); i nomi compaiono anche in `status`/`doctor`.
- **1.0.0** — rimozione di `install.sh`/`uninstall.sh` (wrapper deprecati da 0.7.0).
  La dismissione della CI GitLab (`.gitlab-ci.yml`) è **già avvenuta** su `main`
  durante 0.9.x, a seguito dell'eliminazione del repo GitLab: il publish a PyPI
  vive su GitHub Actions OIDC (§14.6).
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
  Linux — `safekeep daemon install` fa render + `systemctl --user daemon-reload`
  (best-effort), linger compreso (§14.4, hint o `--enable-linger`).
- **Senza systemd** (WSL1 / systemd off): il **preflight di `safekeep daemon
  install` fallisce** (exit ≠ 0) con l'errore chiaro: abilita systemd in
  `/etc/wsl.conf` (`[boot] systemd=true`), poi `wsl --shutdown` da Windows —
  **oppure** avvia a mano `safekeep run` (niente fallback su bash profile:
  decisione). **Cambiamento 0.7.0**: `install.sh` (pre-0.7.0) terminava con
  exit 0 dopo le istruzioni — degradazione graziosa; il nuovo comando tratta la
  mancanza di systemd come preflight fallito, perché senza systemd la unit non
  ha senso e l'errore va visto. `safekeep doctor` resta **exit 0** su WSL
  senza systemd (§17.2: tutti i check WSL sono informativi e non fatali).
- **Path**: dest/source sotto `/mnt/<lettera>/` = drvfs (vedi §17.2.3).

### 17.4 Limiti dichiarati

- **Nessun test su WSL reale** (nessuna macchina WSL disponibile): detection,
  marker di `doctor` e branch di preflight/install (0.7.0: `daemonctl.preflight`
  / `install`, ex-`install.sh`) testati con env/kernel finto; un
  E2E su WSL no.
- Eventi su `/mnt/*` (drvfs/9p) e fswatch su WSL: non documentati qui, da
  verificare quando ci sarà una macchina WSL. Con 0.9.0 (§20) si aggiunge: il
  fstype `drvfs` è **locale** (non in `NETWORK_FSTYPES`) → le root sotto
  `/mnt/` restano su fswatch, non sul polling; un mount CIFS esplicito sotto
  WSL (`mount.cifs`) rientrerebbe nel polling di §20.1, ma WSL resta non
  testato su macchina reale.

---

## 18. Destinazione S3-compatible (0.6.0)

Una **sola** destinazione S3-compatible (S3 = Simple Storage Service),
affiancata alle dest filesystem: le `dest:` restano come sono, le chiavi
`s3_*` vivono **solo** in `~/.safekeep` (un `.sync` non può aggiungerle:
`_KEY_SHAPE` le classifica righe invalide, §4.2/§11).

### 18.1 Schema chiavi (`~/.safekeep`)

| Chiave | Tipo | Default | Descrizione |
|---|---|---|---|
| `s3_endpoint` | URL `http(s)://host[:porta]` | endpoint AWS | endpoint del servizio; **obbligatorio di fatto** per servizi self-hosted |
| `s3_bucket` | nome bucket | — | con `s3_bucket` presente serve **almeno una `dest:` oppure** `s3_bucket` (backward compat) |
| `s3_prefix` | path piatto | vuoto | prefisso dentro il bucket, normalizzato (niente `/` iniziale/finali); **`..` → errore** |
| `s3_region` | stringa | `us-east-1` | regione (MinIO/Silo: libera) |
| `s3_access_key` | stringa | — | **obbligatorio** quando `s3_bucket` presente |
| `s3_secret_key` | stringa | — | XOR con `s3_secret_key_env`; il file deve essere `chmod 600` (§18.4) |
| `s3_secret_key_env` | nome variabile d'env | — | secret letto da una variabile d'ambiente; **assente → errore a parse-time** |
| `s3_addressing` | `path` \| `virtual` | `path` | stile di indirizzamento; `path` per endpoint custom |
| `s3_tls_check` | `true` \| `false` | `true` | `false` = accetta certificati self-signed (off topic: disabilita solo la verifica, `http://` resta cleartext) |

Client: **botocore** (non boto3) tramite l'extra
`pipx install "safekeep[s3]"` (`[project.optional-dependencies] s3 =
["botocore>=1.31,<2"]`), import **lazy** in `safekeep/s3dest.py`: senza
extra, una config `s3_*` produce `ConfigError` fail-fast con hint
(`run`/`sync-once`/`status` escono ≠ 0; in `doctor` è una riga **non
fatale**, §9.2). Il runtime senza `s3_*` resta stdlib-only.

Validazioni fail-fast in `parse_config` (con numero di riga): bucket senza
`s3_access_key`; `s3_secret_key` **e** `s3_secret_key_env` insieme; entrambi
assenti; env richiamata ma inesistente; chiave `s3_*` ignota; `s3_prefix`
con `..`; enum invalidi. Endpoint `http://` → **warning cleartext** (non
fatale). In assenza di `s3_bucket` il requisito storico "almeno una `dest:`"
resta invariato.

### 18.2 Mapping semantica

- **Key** = `s3_prefix` normalizzato + path dest calcolato col `layout`
  globale (`relative`/`full`, §3) con separatori `/` — stesso identico
  calcolo delle dest fs (`dest_path`), così un oggetto e il suo omologo su
  disco stanno nello stesso "luogo" logico.
- **`needs_copy`** = `HeadObject`: oggetto assente → copia; altrimenti
  (size, mtime) — mtime salvato come `x-amz-meta-mtime` in **nanosecondi**,
  tolleranza 1s (`MTIME_TOLERANCE`, §7.1) → stessa semantica idempotente
  delle dest fs. Metadata `x-amz-meta-mode` = `oct(st_mode)`.
- **Symlink** = object a **zero byte** con `x-amz-meta-symlink` = target
  (mai seguito, §7.3): `needs_copy` confronta il target.
- **Upload** = `put_object` con `Body` = file aperto in streaming e i
  metadata; nessuna writer di destinazione: niente tmp, niente replace —
  un PUT object è di per sé atomico (lo scritto parziale non diventa mai
  l'oggetto "giusto" con metadata validi, e al prossimo reconcile
  size/mtime divergenti rilanciano la copia).
- **Stability check** invariato (§7.1): due stat prima dell'upload.
- **Mai cancellare**: **nessuna DeleteObject**, nessuna list-then-prune:
  `--prune` tocca **solo** le dest fs (§9). **Nessun versioning** del
  bucket richiesto né gestito.

### 18.3 Errori di rete → pending

`safekeep/s3dest.py` rilancia gli errori botocore come `OSError`:

| Eccezione botocore | errno | Effetto |
|---|---|---|
| timeout connessione/lettura, DNS (Domain Name System), endpoint irraggiungibile, 5xx, throttling/`SlowDown` | `ENODEV` | **pending + backoff** §8.3 (stesso percorso di un mount sparito) |
| 403/400 (credenziali errate, richiesta malformata) | `EACCES` | solo log (throttled §10.1), **non** pending: al prossimo reconcile ritenta |

`state()` = `HeadBucket` con **cache 30s** (`ok`/`offline`): `status` lo
mostra, `doctor` lo usa come check non fatale, il retry dei pending lo usa
come "il mount è tornato?".

### 18.4 Sicurezza credenziali

- `s3_secret_key` inline nel file: `Daemon.load_config()` controlla i
  permessi; se > 0600 → **warning runtime una-tantum** (emesso dopo
  l'init del logging) + riga `doctor` **non fatale** con remediation
  `chmod 600 ~/.safekeep`.
- Alternativa senza secret nel file: `s3_secret_key_env: NOME_VAR`
  (la variabile va nell'ambiente dell'agent: `launchd`/`systemd`).
- I log contengono solo endpoint/bucket/key mai la secret (§11).

### 18.5 Vincoli dichiarati

- **Single-process**: nessun thread propri — un PUT lento (o un HeadObject
  lento) blocca il loop del daemon per la durata del transfer, con il timeout
  botocore come tetto (connect 10s / read 60s, retry limitati). Accettabile
  in v1; se non lo fosse, il rimedio è un processo dedicato (decisione
  aperta, §15).
- **Upload senza multipart**: un singolo `put_object` in streaming (botocore
  legge il file a chunk, niente caricamento in RAM). Il multipart del
  `TransferManager` **non** esiste in botocore puro (vive in `s3transfer`):
  per non aggiungere dipendenze oltre a botocore l'extra resta
  `safekeep[s3] = botocore` e il tetto dichiarato è il limite di un singolo
  PUT (~5 GB su MinIO/Silo, 5 GB su AWS S3) — oltre, `EntityTooLarge` → log
  non fatale. Se serviranno file più grossi: extra `s3transfer` col
  TransferManager (i suoi thread vivono solo durante l'upload).
- Una sola dest S3 per config.
- **Silo** (endpoint self-hosted, hostname privato — mai in repo) è un
  **fork di MinIO**: SigV4
  (Signature Version 4), path-style, regione libera → nessun fallback o
  workaround proprietario è necessario; lo stesso client copre anche MinIO
  self-hosted, Ceph RGW, Wasabi, Backblaze B2 (compat S3) e AWS S3.

---

## 19. Ripristino `restore` (0.8.0)

Ripristina dalla **ultima copia** presente in una destinazione. Semantica
**copy-only**: safekeep non versiona (nessuna storia, §10.1/§18.2) — la dest
contiene l'ultimo stato copiato ed è quello che viene ripristinato. **Niente
cancellazioni** mai; le credenziali S3 arrivano **solo** dal config globale
`~/.safekeep` (`s3_*`, §18.1): nessun flag di credenziali a riga di comando.

### 19.1 Sintassi

```
safekeep restore [--config PATH] [-v] [--dest NOME] [--from {local,s3,nas}] [--force] [--if-dir-exists] [PATH|GLOB ...]
```

| Argomento | Significato |
|---|---|
| `PATH`/`GLOB` (uno o più) | path assoluto, path relativo alla radice `source` del progetto, o glob (`fnmatch`) sui path originali → modalità **non interattiva** |
| `--dest` | (0.11.0, §21.4) origine del ripristino = destinazione **nominata** `dest <name>:` (§21.2); **ha precedenza** su `--from`; nome inesistente → errore con l'elenco dei nomi disponibili |
| `--from` | origine del ripristino: `local` = prima `dest:`, `nas` = seconda `dest:`, `s3` = dest S3 (§18) |
| `--force` | esegue davvero le copie; **senza, il dry-run è SEMPRE il comportamento** (nessuna eccezione, §11) |
| `--if-dir-exists` | ripristina un file **solo se la sua directory di destinazione immediata esiste già** (0.10.0, §19.6); default inattivo |
| `-v` | log verboso (debug), come sugli altri comandi |

### 19.2 Tabella casi (selezione)

| Condizione | Comportamento |
|---|---|
| nargs ≥ 1 | non-interattivo: ogni argomento viene risolto sui **path originali** (target) — assoluto, relativo alla radice `source` o glob `fnmatch` |
| nargs == 0 e stdin è un TTY | interattivo: menu numerati per livello (§19.4) |
| nargs == 0 e stdin **non** è un TTY | errore (`serve un PATH/GLOB oppure un terminale interattivo`), exit ≠ 0 |
| senza `--force` | **SEMPRE dry-run**: le righe vengono stampate e nessuna scrittura avviene — anche in interattivo |

### 19.3 `--dest` e `--from` — origine del ripristino

- **`--dest <nome>`** (0.11.0, §21.4): se presente, identifica **univocamente**
  l'origine come destinazione **nominata** `dest <name>:` (§21.2). **Ha
  precedenza** su `--from`: se `--dest` è valido, l'auto-detect per classe non
  viene neppure considerato. Nome non dichiarato → errore con l'elenco dei
  nomi disponibili. Include anche le destinazioni S3 nominate via route
  (`s3:<prefix>` / `s3://<bucket>/<prefix>`, §21.2).
- **`--from` / auto-detect** (default, senza `--dest`): i candidati sono le dest configurate, in ordine di
  config — ogni `dest:` **montata**, poi la dest S3 se configurata. **Un solo
  candidato** → viene usato e il log indica quale; **due o più** → errore con
  l'elenco dei candidati (l'utente ripete con `--from`); **zero** → errore.
- una `dest:` configurata ma **non montata** (volume assente, §8.3) → errore con
  l'elenco: il ripristino non ignora silenziosamente un'origine dichiarata.
- `--from s3` → **solo** la dest S3 del config (§18): extra `safekeep[s3]` e
  credenziali `s3_*`, niente flag.
- `--from local`/`nas` senza la corrispondente `dest:` → errore con l'elenco
  dei candidati disponibili.

### 19.4 Menu interattivo (livelli)

1. **origine** — solo se l'origine è ambigua (≥2 candidati): elenco numerato.
2. **progetto** — progetti scoperti (`.sync`, §4.1) con almeno una voce nella
   origine scelta.
3. **voci di primo livello** — voci del backup sotto il prefisso del progetto.

Ogni livello: selezione per numero (anche multipla, es. `1 3`), `a` = tutto,
`q` = annulla (exit ≠ 0). **Nessuna scrittura senza `--force`, nemmeno in
interattivo** (§11): il menu produce il piano, `--force` lo esegue.

### 19.5 Mapping inverso, inventario ed esecuzione

- **target = solo il path originale**: niente `--to`. Containment **invertito**
  rispetto al CR-01 (§7.2/§11): restore scrive **solo dentro le source** del
  progetto — un path ricostruito che ne esce → `EACCES` + skip, mai scrittura.
- mapping inverso dest→target per entrambi i layout (§3): `relative` → target =
  radice `source` + relativo dest; `full` → target = `/` + relativo dest.
- inventario: per ogni progetto scoperto → prefisso dest (`dest_path` / la
  variante S3 di `S3Destination.dest_path`) → walk della dest con pruning come
  `prune_project` (§9, solo sotto il prefisso) oppure `iter_keys` paginato per
  S3 (§18).
- **matching**: gli argomenti matchano i target (assoluto o relativo alla radice
  `source`), `fnmatch` per i glob; **nessun match → exit ≠ 0** (§12).
- **pianificazione**: `needs_copy(backup, target)` → righe
  `<backup> -> <target> (sovrascrive)` oppure `(crea)` + sintesi (stile
  `sync-once`).
- **esecuzione** (solo `--force`): fs → `copy_one(backup, target,
  dest_root=radice source del progetto)` — atomica tmp + `os.replace` (§7.2);
  S3 → `download` (streaming → tmp + replace nella dir target, `chmod`/`utime`
  da `x-amz-meta-*`, symlink ripristinato da `x-amz-meta-symlink`, §18.2).
- **orfani**: una voce della dest senza progetto corrispondente (prefisso non
  sotto nessun progetto scoperto) → warn + skip, mai toccata.
- **mai delete**: restore non cancella né sovrascrive "verso il basso": copia
  solo ciò che il piano elenca.

### 19.6 `--if-dir-exists` — salta se la directory di destinazione manca (0.10.0)

Flag **default inattivo** (comportamento invariato: le parent vengono create).
Quando presente, un file viene ripristinato **solo se la directory di
destinazione immediata (parent = `dirname` del target assoluto) esiste già**:
altrimenti il file viene **saltato** — niente creazione di cartelle mancanti
(utente che non clona più un albero di progetto sul disco). Vale per tutte le
origini (`--from local|nas` fs e `--from s3`, dove il controllo sul target
locale precede la download), sia nel piano dry-run (riga con nota
`(salta: directory destinazione assente)` e conteggio `saltati` nella sintesi)
sia nell'esecuzione con `--force` (skip + motivo nel log, `saltati` nel
riepilogo). In interattivo il flag vale ugualmente: gli skip compaiono nel
piano. Tutti i match saltati → exit 0 con sintesi `0 da copiare, N saltati
(directory destinazione assente)`.

---

## 20. Volumi di rete (SMB/CIFS, NAS) e monitor a polling (0.9.0)

**Il fatto che rende necessaria questa sezione**: FSEvents (macOS) e inotify
(Linux) **non emettono eventi** per i file che cambiano su un mount di rete —
SMB/CIFS/NFS (fstype `cifs`, `smbfs`, `nfs`): il kernel non riceve notifiche
dal server. Una `source` su NAS era quindi **silenziosamente non monitorata**
da safekeep ≤ 0.8.0 (solo il reconcile giornaliero la copriva, con fino a 24h
di latenza). È una **decisione nuova in 0.9.0**: le root di rete passano da
fswatch a un **monitor a polling** dentro il loop single-process del daemon —
niente thread, stesso dispatch degli eventi.

### 20.1 Classificazione delle root

- A ogni **avvio**, **rescan di discovery** (24h) e **reload della config**, le
  radici discovery (`source:` oppure `$HOME`) vengono classificate con
  `platform.is_network_fs` (fstype del mount: `/proc/mounts` su Linux, output
  di `mount` su macOS — §14.1):
  - radici **locali** → argv di fswatch, invariato (§6.4);
  - radici **di rete** → **escluse dall'argv** di fswatch, monitorate dal
    poller (§20.2).
- **Zero radici locali → fswatch non viene spawnato**: il daemon vive di soli
  timer (reconcile 24h, pending dest, scadenze poll).
- **Limite dichiarato v1**: la classificazione è **a root** — un mount di rete
  annidato *dentro* una root locale non viene polled (resta su fswatch, che non
  ne vedrà gli eventi); classificare ogni singola directory annidata costa un
  `mount` per directory ed è fuori scope per v1.

### 20.2 Poller (`safekeep/poll.py`)

- **Snapshot** per root polled: `{relpath: (st_mtime_ns, st_size)}` letto con
  `os.lstat` (**mai** seguire i symlink: uno symlink rotto è un dato, §7.3),
  con lo stesso pruning dello scan di discovery (nascoste, `Library`,
  `node_modules`, `.git`, `venv`, … — §4.1): `.git`/`node_modules` su un NAS
  non vengono camminati.
- **Diff a ogni turno** (ogni `poll_interval` secondi, §3): path nuovi o con
  (mtime_ns, size) cambiati → `daemon.run_batch(paths)` — lo **stesso** dispatch
  degli eventi fswatch (dedup → plan → classify → copia): nessuna logica di
  copia duplicata, le regole `.sync` valgono identiche. Path **rimossi** alla
  sorgente → ignorati (semantica mai-cancellare, §1).
- L'intervallo parte a **fine walk**: un walk lento non si sovrappone al turno
  successivo.
- Il **primo walk** di una root è una baseline senza dispatch (il reconcile
  iniziale o del rescan ha già copiato tutto).
- **Mount smontato / server morto**: un `OSError` nel walk (radice inclusa:
  `os.walk(..., onerror=...)` propaga) → la root entra in `poll_pending` con
  `backoff_schedule()` (§8.3, stessa curva 1→2→4→…→60s) e lo snapshot
  **pre-smontaggio viene preservato**. Walk riuscito dopo il remount → diff
  contro lo snapshot preservato → **catch-up automatico** di tutto ciò che è
  cambiato mentre il mount era giù. L'errore resta **confinato alla propria
  root** (CR-05): nessun crash del daemon, le altre root proseguono.
- **Integrazione nel loop**: le scadenze del poll entrano in `next_deadline()`
  (come reconcile, pending e dedup): il `select` del daemon si sveglia al turno
  di poll — niente thread, niente sleep dedicati.

### 20.3 Config: `poll_interval`

`poll_interval:` nel config globale (§3): **intero ≥ 1** secondo, default
`60`, fail-fast con numero di riga come ogni altra chiave del config. Un
valore sotto al minuto ha senso solo su alberi piccoli: ogni turno è un walk
 completo della root con I/O di rete sul NAS.

### 20.4 Errori di rete sulla dest (`NETWORK_ERRNOS`)

`copier.NETWORK_ERRNOS = (ENOTCONN, EHOSTUNREACH, ETIMEDOUT, ENETUNREACH,
ENETDOWN, ESTALE)` — controllati **insieme** a `MOUNT_ERRNOS`
(ENODEV/EBUSY/ENOSPC) ai call-site esistenti (`reconcile_project.failed`,
`Daemon._copy`): una dest su NAS con il server morto dà quest'errno, e ora va
in **pending + backoff** (§8.3) invece di produrre una riga `copia fallita`
per evento (log-loop latente, chiuso in 0.9.0).

### 20.5 Copia su filesystem di rete

- **Atomicità**: il pattern tmp + `os.replace` nella cartella di dest (§7.2)
  resta corretto su CIFS/SMB: il `rename` è atomico **lato server** (il client
  lo vede come operazione singola) e il tmp vive sullo stesso filesystem della
  dest.
- **fsync best-effort** (§7.2): su CIFS può costare un round-trip extra per
  file; resta best-effort — un fsync non riuscito non blocca la copia.
- **Residuo di crash**: un crash a metà copia lascia `*.safekeep.tmp.*`
  visibile sulla dest — `doctor` lo elenca già (§14.4); su un NAS è solo
  rumore visibile, non un danno (alla prossima copia del file viene rimosso).
- **I/O lento = tetto single-process** (§18.5): un walk o una copia lenta su
  NAS blocca il loop del daemon per quella durata, come un PUT S3 lento. Il
  polling non cambia l'architettura: se il tetto non basterà, il rimedio resta
  un processo dedicato (decisione aperta, §15).

### 20.6 Diagnostica (`doctor` / `status`)

- `doctor`: due righe **non fatali** (con id slug per `--json`, SPEC §9.1):
  `sorgenti su volume di rete: N (poll ogni Xs)` e `fswatch: root locali M`.
- `status`: ogni dest viene etichettata con la fstype detect —
  `dest <path> [ok|absent|unreachable] [nas|local]`.
- **0.9.1** — mount c'è ma il server è morto: `volumes.network_error` (un
  `listdir`, `NETWORK_ERRNOS` §20.4) fa da probe; `status` marca la dest
  `[unreachable]` (l'ingannevole `[ok]` del solo `dest_state`) e `doctor` ha
  la riga non fatale `volumi di rete non raggiungibili: N (…)`. Probe solo
  nei comandi one-shot, mai nel loop del daemon.
- macOS: `EACCES` su un path sotto `/Volumes` → hint TCC "Interagisci con i
  volumi rimovibili" (System Settings → Privacy & Security), warning non fatale.

### 20.7 Limiti dichiarati (v1)

- Classificazione **a root** (§20.1): un mount annidato dentro una root locale
  non viene polled.
- Latenza: un file che cambia senza altri eventi resta nel backup fino al
  turno successivo (fino a `poll_interval` secondi); il reconcile a 24h e al
  mount resta la rete di sicurezza.
- **WSL non verificato** (§17.4): `drvfs` è fstype locale → sotto `/mnt/` si
  resta su fswatch; un mount CIFS esplicito sotto WSL entrerebbe nel polling
  ma non è testato su macchina reale.

---

## 21. Routing destinazioni (0.11.0)

### 21.1 Modello

Fino a 0.10.0 **tutte** le sorgenti vanno su **tutte** le destinazioni
(`dest:` filesystem anonime + la dest S3 di §18): broadcast globale. Il
routing introduce la scelta **sorgente → target**: una sorgente può andare su
una o più destinazioni specifiche, invece che su tutte.

- **Dove vive il routing**: solo nel config globale `~/.safekeep`, **mai** nel
  `.sync` di progetto (§4.2): le destinazioni restano fuori dal progetto —
  decisione storica di sicurezza (§11), un `.sync` malevolo di terzi non può
  mai deviare un backup.
- **Granularità**: **per progetto**, determinata dalla *source root* scoperta
  (la radice `source` che contiene il progetto, o `$HOME` in auto-discovery
  §6). La route si applica all'intero progetto sotto quella radice.
- **Default retrocompat (zero breaking)**: una sorgente **senza** route va su
  **tutte** le destinazioni (anonime + nominate + S3) — comportamento
  identico a 0.10.0. Il routing è **opt-in**.

### 21.2 Sintassi

Nuove forme di riga in `~/.safekeep` (stessa grammatica `chiave: valore` di
§3, fail-fast con numero di riga):

```ini
dest nas: /Volumes/NAS/backup        # destinazione NOMINATA (filesystem)
dest ext: /Volumes/EXFIT/safekeep-backup
dest: /legacy/anonymous/path         # destinazione anonima (legacy, ancora valida)
s3_bucket: my-bucket                 # legacy, ancora valido (§18)

route ~/projects → nas               # sorgente su una sola destinazione
route ~/Documents/notes → ext, nas   # sorgente su più destinazioni
route ~/code → s3:backup/code        # sorgente su S3: bucket = s3_bucket, prefisso backup/code
```

| Forma | Significato |
|---|---|
| `dest <name>: <path>` | destinazione filesystem **nominata**; `<name>` serve da riferimento nelle route, in `status`/`doctor`/`restore --dest` |
| `dest: <path>` | destinazione **anonima** (legacy, §3): ancora valida, mai citabile in una route (non ha nome) |
| `route <src> → <dest-list>` | instrada la sorgente `<src>` su **tutte** le destinazioni elencate in `<dest-list>` (nomi filesystem separati da virgola, e/o target S3) |
| `route <src> → s3:<prefix>` | target S3 con **bucket = `s3_bucket` globale** (§18) e prefisso proprio `<prefix>` |
| `route <src> → s3://<bucket>/<prefix>` | target S3 con **bucket esplicito** diverso da `s3_bucket`, prefisso `<prefix>` |

**Target S3 nelle route — due forme (decisioni §21.7):**

- `s3:<prefix>` (senza bucket): usa il **bucket globale `s3_bucket`** + il
  prefisso `<prefix>`. Forma **minimale** raccomandata nella maggior parte
  dei casi: un solo bucket, route che differenziano solo il prefisso.
- `s3://<bucket>/<prefix>` (bucket **esplicito**): forma **assoluta** che
  ricorda l'URI S3 standard e non collide con la forma breve. Serve quando la
  route deve finire su un bucket **diverso** da `s3_bucket`. La forma è
  coerente col resto del file (le `dest` fs usano path assoluti `/…`; la
  route S3 "assoluta" usa il suo URI assoluto `s3://…`). Coerente con lo
  stile del file: la forma breve è la scorciatoia, l'URI è l'esplicita.
- Le credenziali S3 restano **le stesse** `s3_*` globali (§18.1): una route
  S3 non porta credenziali proprie; per un bucket self-hosted su endpoint
  diverso resta una limitazione dichiarata (§21.6). `s3_access_key` è
  **obbligatorio** se esiste anche una sola route S3 (stessa validazione di
  §18.1, estesa).

### 21.3 Semantica di matching — *longest-prefix-wins*

- Il matching è sulla **source root** del progetto (non sui singoli file):
  la route il cui `<src>` è il **prefisso più lungo** della source root vince.
- **Route multiple che matchano lo stesso prefisso** (stessa source root
  dichiarata due volte, o un prefisso letteralmente identico) → **errore
  fail-fast** al parse: nessuna ambiguità tollerata, la riga più corta non
  "batte" l'altra. Il messaggio elenca le righe in conflitto.
- **Route verso una destinazione inesistente** (nome non dichiarato con
  `dest <name>:`) → **errore fail-fast** al parse con **numero di riga** e
  nome della destinazione mancante (stessa filosofia di ogni chiave di §3).
- **Sorgente senza route** → **tutte** le destinazioni (anonime + nominate +
  S3 globale): identico a 0.10.0, zero breaking (§21.1).
- `route ~ → x` in auto-discovery `$HOME` matcherebbe **tutto**: è
  **consentito** ed è il modo di esprimere un "default per tutto ciò che non
  ha una route più specifica" — il *longest-prefix-wins* fa sì che una route
  più mirata (`route ~/code → s3:backup/code`) vinca su `route ~ → nas` per
  quei progetti. Una route su `$HOME` non è dunque un errore: è la regola di
  default che le route specifiche overrideano.

### 21.4 Destinazioni nominate e comandi

- I **nomi** delle destinazioni nominate sono usati anche in `status`,
  `doctor` e `restore --dest <nome>` (§9, §19): `status` etichetta ogni dest
  col nome (`dest nas [ok] [local]`), `doctor` le elenca per nome.
- Le dest anonime **non** hanno nome e restano raggiungibili solo via
  `--from {local,s3,nas}` e auto-detect (§19.3), come oggi.
- `restore --dest <nome>` **ha precedenza** su `--from local|s3|nas`
  (§19.3): se presente, il nome identifica univocamente l'origine (l'auto-detect
  per classe resta per i comandi senza `--dest`).

### 21.5 Interazione con S3 per-route (§18)

- La destinazione S3 **globale** (le chiavi `s3_*` di §18.1, una sola) resta:
  è il bucket/prefisso di default per `s3_bucket`/`s3_prefix`. Con il routing
  può non essere più "broadcast" su tutte le sorgenti, ma resta il target S3
  di riferimento per `s3:<prefix>` senza bucket esplicito.
- Il **client S3 è già cached per-bucket** (`s3dest.py`): route S3 verso
  **bucket diversi** condividono endpoint/regione/credenziali `s3_*`; la cache
  per-bucket evita di ricreare il client a ogni route. Un bucket non presente
  in `s3_bucket` usa comunque le credenziali globali (§21.2).
- **Niente rewriting path per-route**: il layout dest (`relative`/`full`, §3)
  è **globale** e resta invariato; una route S3 usa solo il **prefisso**
  proprio (`s3:<prefix>`), il calcolo del path dentro la dest è lo stesso
  `dest_path` di sempre. La route sceglie *dove* va il progetto, non *come*
  si mappa il suo path dentro la destinazione.

### 21.6 Limiti dichiarati

- **Niente rewriting path per-route**: la destinazione mantiene il layout
  `relative`/`full` globale (§3); una route non può ridefinire il layout del
  proprio target.
- Le route S3 condividono **endpoint/regione/credenziali** `s3_*` globali
  (§18.1): route verso un servizio S3 su endpoint diverso da `s3_endpoint`
  non sono supportate in v1.
- Il routing è **per source root** (progetto), non per singolo file: una
  route non si applica a sotto-insiemi arbitrari di file dentro un progetto.

### 21.7 Decisioni

- **(a)** Bucket **esplicito** diverso da `s3_bucket` → `s3://<bucket>/<prefix>`
  (URI assoluto, non collide con la forma breve `s3:<prefix>`). Motivazione:
  coerente con lo stile del file — le `dest` fs usano path assoluti, la route
  S3 "assoluta" usa l'URI standard; la forma breve resta la scorciatoia per
  il bucket globale.
- **(b)** `s3:<prefix>` **senza** bucket usa il **`s3_bucket` globale** →
  **sì**. Motivazione: minimale — un solo bucket, route che differenziano solo
  per prefisso, niente ripetizione inutile del nome bucket.
