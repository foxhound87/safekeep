# launchd — template

Il template dell'agent principale **non vive più nel repo**: sorgente unica nel
package (`safekeep/data/com.safekeep.agent.plist.tmpl`), renderizzato da
`safekeep daemon install` (safekeep 0.7.0 — nessun placeholder `__REPO__`:
ExecStart = shim `safekeep` sul PATH → `safekeep run`).

Resta qui solo `com.safekeep.trend.plist` (agent orario del trend, SPEC §9.2),
installato con `./install.sh --trend` — fuori scope del comando
`safekeep daemon` in 0.7.0.
