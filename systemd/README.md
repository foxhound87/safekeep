# systemd — template

Il template della user unit **non vive più nel repo**: sorgente unica nel
package (`safekeep/data/safekeep.service.tmpl`), renderizzato da
`safekeep daemon install` (safekeep 0.7.0 — ExecStart = shim `safekeep` sul
PATH → `safekeep run`, `__PATH__` con la dir di fswatch).

```sh
pipx install safekeep
safekeep daemon install     # render + scrittura in ~/.config/systemd/user/
safekeep daemon start       # systemctl --user enable --now
```

`install.sh` è un wrapper deprecato che delega a questi comandi.
