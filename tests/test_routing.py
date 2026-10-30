"""Routing destinazioni 0.11.0 (SPEC.md §21) — parsing, validazioni, mapping.

Solo config: `dest <name>:`, `route <src>: <dest-list>` (due forme S3), gli
errori fail-fast con numero di riga e la funzione pura `dests_for_source`.
Path finti assoluti (nessun path reale della macchina tranne `~`, che è
espanso a runtime) — come nel resto della suite.
"""
import os
import unittest

from safekeep.config import ConfigError, dests_for_source, parse_config

HOME = os.path.expanduser('~')
ORIGIN = 'cfg'


def s3_block(prefix=None):
    """Blocco `s3_*` valido e minimale (§18.1): bucket + credenziali."""
    lines = ['s3_bucket: bkt', 's3_access_key: AK', 's3_secret_key: SK']
    if prefix is not None:
        lines.append(f's3_prefix: {prefix}')
    return '\n'.join(lines) + '\n'


def parse(text):
    return parse_config(text, origin=ORIGIN)


class DestNominateTest(unittest.TestCase):
    """`dest <name>: <path>` (SPEC.md §21.2) — la dest citabile nelle route."""

    def test_nominata_e_anonime_nell_ordine_del_config(self):
        cfg = parse('dest: /d/legacy\ndest nas: /d/nas\ndest: /d/other\n')
        # le nominate finiscono in cfg.dests come le anonime (i consumatori
        # legacy — status/doctor/restore — le vedono tutte, §21.4)
        self.assertEqual(cfg.dests, ['/d/legacy', '/d/nas', '/d/other'])
        self.assertEqual(cfg.dest_names, {'nas': '/d/nas'})

    def test_percorso_con_tilde_espanso(self):
        cfg = parse('dest nas: ~/backup-nas\n')
        self.assertEqual(cfg.dest_names['nas'],
                         os.path.join(HOME, 'backup-nas'))

    def test_nome_duplicato_errore_con_riga(self):
        text = 'dest nas: /d1\ndest x: /d2\ndest nas: /d3\n'
        with self.assertRaises(ConfigError) as cm:
            parse(text)
        msg = str(cm.exception)
        self.assertIn('cfg:3', msg)
        self.assertIn('nas', msg)
        self.assertIn('già dichiarata', msg)

    def test_nome_non_valido_errore_con_riga(self):
        for name in ('na me', 'na/me', 'na.me'):
            with self.subTest(name=name):
                with self.assertRaises(ConfigError) as cm:
                    parse(f'dest {name}: /d\n')
                msg = str(cm.exception)
                self.assertIn('cfg:1', msg)
                self.assertIn('nome dest non valido', msg)

    def test_nominata_senza_colon_errore_con_riga(self):
        # nessuna `:` dopo il nome → riga malformata (§3, fail-fast con riga)
        with self.assertRaises(ConfigError) as cm:
            parse('dest nas /d\n')
        msg = str(cm.exception)
        self.assertIn('cfg:1', msg)
        self.assertIn('malformata', msg)

    def test_nominata_senza_percorso_errore_con_riga(self):
        with self.assertRaises(ConfigError) as cm:
            parse('dest nas:\n')
        msg = str(cm.exception)
        self.assertIn('cfg:1', msg)
        self.assertIn('senza percorso', msg)

    def test_dest_legacy_con_spazio_prima_dei_colon_resta_anonima(self):
        # REGRESSIONE: `dest : /x` era valido (chiave `dest` + spazio) e deve
        # restare la forma anonima, non finire nel ramo nominato
        cfg = parse('dest : /d\n')
        self.assertEqual(cfg.dests, ['/d'])
        self.assertEqual(cfg.dest_names, {})

    def test_colon_doppio_nel_percorso(self):
        cfg = parse('dest nas: /d/a:b\n')
        self.assertEqual(cfg.dest_names['nas'], '/d/a:b')

    def test_nominata_con_carattere_nul_errore_con_riga(self):
        with self.assertRaises(ConfigError) as cm:
            parse('dest nas: /d\0x\n')
        msg = str(cm.exception)
        self.assertIn('cfg:1', msg)
        self.assertIn('NUL', msg)


class RouteParseTest(unittest.TestCase):
    """`route <src>: <dest-list>` (SPEC.md §21.2) — parse e validazioni."""

    def test_route_fs_con_due_dest_e_spazi_tagliati(self):
        cfg = parse('dest nas: /d/nas\ndest ext: /d/ext\n'
                    'route /src: nas , ext\n')
        self.assertEqual(len(cfg.routes), 1)
        r = cfg.routes[0]
        self.assertEqual(r.src, '/src')
        self.assertEqual(r.dests, ['/d/nas', '/d/ext'])
        self.assertEqual(r.s3, [])
        self.assertEqual(r.lineno, 3)

    def test_duplicati_nella_lista_normalizzati_set_ordine_config(self):
        cfg = parse('dest nas: /d/nas\ndest ext: /d/ext\n'
                    'route /src: ext, nas, ext, nas\n')
        self.assertEqual(cfg.routes[0].dests, ['/d/ext', '/d/nas'])

    def test_ordine_della_lista_non_quello_delle_dichiarazioni(self):
        cfg = parse('dest nas: /d/nas\ndest ext: /d/ext\n'
                    'route /src: ext, nas\n')
        self.assertEqual(cfg.routes[0].dests, ['/d/ext', '/d/nas'])

    def test_sorgente_con_tilde_espansa(self):
        cfg = parse('dest nas: /d/nas\nroute ~/code: nas\n')
        self.assertEqual(cfg.routes[0].src,
                         os.path.abspath(os.path.join(HOME, 'code')))

    def test_route_s3_forma_breve_bucket_globale_prefisso_letterale(self):
        cfg = parse('dest nas: /d/nas\n' + s3_block('global/pfx') +
                    'route /src: s3:backup/code\n')
        # bucket = s3_bucket globale; il prefisso è LETTERALE: s3_prefix
        # globale ('global/pfx') NON si applica (SPEC.md §21.2/§21.7b)
        self.assertEqual(cfg.routes[0].s3, [('bkt', 'backup/code')])

    def test_route_s3_forma_uri_bucket_esplicito(self):
        cfg = parse('dest nas: /d/nas\n' + s3_block() +
                    'route /src: s3://other-bucket/old\n')
        self.assertEqual(cfg.routes[0].s3, [('other-bucket', 'old')])

    def test_route_s3_mista_con_fs(self):
        cfg = parse('dest nas: /d/nas\ndest ext: /d/ext\n' + s3_block() +
                    'route /src: nas, s3:pfx, ext\n')
        r = cfg.routes[0]
        self.assertEqual(r.dests, ['/d/nas', '/d/ext'])   # fs, ordine lista
        self.assertEqual(r.s3, [('bkt', 'pfx')])

    def test_route_s3_duplicati_normalizzati(self):
        cfg = parse('dest nas: /d/nas\n' + s3_block() +
                    'route /src: s3:p, nas, s3:p\n')
        self.assertEqual(cfg.routes[0].s3, [('bkt', 'p')])
        self.assertEqual(cfg.routes[0].dests, ['/d/nas'])

    def test_senza_colon_malformata_con_riga(self):
        with self.assertRaises(ConfigError) as cm:
            parse('dest nas: /d/nas\nroute /src nas\n')
        msg = str(cm.exception)
        self.assertIn('cfg:2', msg)
        self.assertIn('malformata', msg)

    def test_senza_sorgente_con_riga_entrambe_le_forme(self):
        # `route : x` (ramo dedicato) e `route: x` (chiave `route` sola)
        for text in ('route : nas\n', 'route: nas\n'):
            with self.subTest(text=text):
                with self.assertRaises(ConfigError) as cm:
                    parse(text)
                msg = str(cm.exception)
                self.assertIn('cfg:1', msg)
                self.assertIn('route senza sorgente', msg)

    def test_sorgente_relativa_errore_con_riga(self):
        with self.assertRaises(ConfigError) as cm:
            parse('dest nas: /d/nas\nroute code: nas\n')
        msg = str(cm.exception)
        self.assertIn('cfg:2', msg)
        self.assertIn('non assoluta', msg)

    def test_route_verso_destinazione_inesistente_errore_con_riga(self):
        text = 'dest nas: /d/nas\nroute /src: nas2\n'
        with self.assertRaises(ConfigError) as cm:
            parse(text)
        msg = str(cm.exception)
        self.assertIn('cfg:2', msg)
        self.assertIn('inesistente', msg)
        self.assertIn('nas2', msg)

    def test_route_senza_destinazioni_errore_con_riga(self):
        with self.assertRaises(ConfigError) as cm:
            parse('dest nas: /d/nas\nroute /src:\n')
        msg = str(cm.exception)
        self.assertIn('cfg:2', msg)
        self.assertIn('senza destinazioni', msg)

    def test_route_token_vuoto_errore_con_riga(self):
        # `nas,` (virgola in coda) e `nas,, ext`: fail-fast, niente token vuoti
        for destlist in ('nas,', 'nas,, ext'):
            with self.subTest(destlist=destlist):
                with self.assertRaises(ConfigError) as cm:
                    parse(f'dest nas: /d/nas\nroute /src: {destlist}\n')
                msg = str(cm.exception)
                self.assertIn('cfg:2', msg)
                self.assertIn('destinazione vuota', msg)

    def test_route_duplicata_errore_elenca_le_righe(self):
        text = 'dest nas: /d/nas\nroute /src: nas\nroute /src: nas\n'
        with self.assertRaises(ConfigError) as cm:
            parse(text)
        msg = str(cm.exception)
        self.assertIn('cfg:3', msg)          # riga che chiude il conflitto
        self.assertIn('riga 2', msg)         # riga della prima dichiarazione

    def test_route_duplicata_dopo_normalizzazione(self):
        # `/a/b/` e `/a/b` sono la STESSA sorgente → ambiguità (§21.3)
        text = 'dest nas: /d/nas\nroute /a/b: nas\nroute /a/b/: ext\n'
        with self.assertRaises(ConfigError) as cm:
            parse(text)
        self.assertIn('cfg:3', str(cm.exception))

    def test_route_sovrapposte_ma_diverse_sono_legittime(self):
        # SOLO l'uguaglianza esatta è errore: prefissi diversi che si
        # sovrappongono sono il caso longest-prefix-wins (§21.3), non un bug
        cfg = parse('dest nas: /d/nas\ndest ext: /d/ext\n'
                    'route /a: nas\nroute /a/b: ext\n')
        self.assertEqual([r.src for r in cfg.routes], ['/a', '/a/b'])

    def test_route_s3_senza_s3_bucket_errore_con_riga(self):
        # forma breve: il bucket è per definizione il globale (§21.7b) —
        # senza `s3_bucket:` nel file → fail-fast con riga
        text = 'dest nas: /d/nas\nroute /src: s3:pfx\n'
        with self.assertRaises(ConfigError) as cm:
            parse(text)
        msg = str(cm.exception)
        self.assertIn('cfg:2', msg)
        self.assertIn('s3_bucket', msg)

    def test_route_s3_prefisso_vuoto_errore_con_riga(self):
        # forma breve senza prefisso e URI con prefisso vuoto
        for token in ('s3:', 's3://bkt/'):
            with self.subTest(token=token):
                with self.assertRaises(ConfigError) as cm:
                    parse('dest nas: /d/nas\n' + s3_block() +
                          f'route /src: {token}\n')
                msg = str(cm.exception)
                self.assertIn('cfg:5', msg)
                self.assertIn('prefisso S3 vuoto', msg)

    def test_route_s3_uri_senza_bucket_errore_con_riga(self):
        for token in ('s3:///pfx', 's3://', 's3://bkt'):
            with self.subTest(token=token):
                with self.assertRaises(ConfigError) as cm:
                    parse('dest nas: /d/nas\n' + s3_block() +
                          f'route /src: {token}\n')
                msg = str(cm.exception)
                self.assertIn('cfg:5', msg)
                self.assertIn('malformata', msg)

    def test_route_s3_prefisso_con_dotdot_errore_con_riga(self):
        with self.assertRaises(ConfigError) as cm:
            parse('dest nas: /d/nas\n' + s3_block() +
                  'route /src: s3:a/../b\n')
        msg = str(cm.exception)
        self.assertIn('cfg:5', msg)
        self.assertIn('..', msg)

    def test_route_s3_uri_senza_blocco_s3_errore_access_key(self):
        # §21.2: s3_access_key obbligatorio se esiste anche una sola route
        # S3 — la forma URI non ha bisogno di s3_bucket ma sì delle
        # credenziali globali §18.1 (unica fonte di credenziali, §21.5)
        text = 'dest nas: /d/nas\nroute /src: s3://bkt/pfx\n'
        with self.assertRaises(ConfigError) as cm:
            parse(text)
        msg = str(cm.exception)
        self.assertIn('cfg:2', msg)
        self.assertIn('s3_access_key', msg)

    def test_route_con_carattere_nul_errore_con_riga(self):
        with self.assertRaises(ConfigError) as cm:
            parse('dest nas: /d/nas\nroute /src: na\0s\n')
        msg = str(cm.exception)
        self.assertIn('cfg:2', msg)
        self.assertIn('NUL', msg)

    def test_config_solo_route_senza_dest_restano_gli_errori_di_route(self):
        # le route si risolvono PRIMA del controllo "nessuna dest:": un errore
        # di route è più specifico dell'hint generico e riporta la riga
        with self.assertRaises(ConfigError) as cm:
            parse('route /src: nas\n')
        self.assertIn('inesistente', str(cm.exception))


class DestsForSourceTest(unittest.TestCase):
    """Mapping puro `dests_for_source(root, cfg)` (SPEC.md §21.1/§21.3)."""

    LEGACY = 'dest: /d1\ndest nas: /d2\n'
    LEGACY_S3 = LEGACY + s3_block('safekeep')

    def test_legacy_senza_route_va_su_tutte_le_dest(self):
        cfg = parse(self.LEGACY)
        self.assertEqual(dests_for_source('/src', cfg),
                         (['/d1', '/d2'], []))
        # nessuna route → S3 globale ereditato da s3_bucket (con s3_prefix)
        cfg = parse(self.LEGACY_S3)
        self.assertEqual(dests_for_source('/src', cfg),
                         (['/d1', '/d2'], [('bkt', 'safekeep')]))

    def test_route_matchata_solo_le_sue_dest_s3_globale_escluso(self):
        cfg = parse('dest: /d1\ndest ext: /d2\n' + s3_block('safekeep') +
                    'route /src: ext\n')
        # route c'è → SOLO quelle dest; la dest anonima e la S3 globale
        # restano fuori (il routing sostituisce il broadcast, §21.1)
        self.assertEqual(dests_for_source('/src/proj', cfg), (['/d2'], []))
        # sorgente non matchata → broadcast di sempre
        self.assertEqual(dests_for_source('/other', cfg),
                         (['/d1', '/d2'], [('bkt', 'safekeep')]))

    def test_route_s3_mappata_senza_applicare_s3_prefix_globale(self):
        cfg = parse('dest nas: /d/nas\n' + s3_block('safekeep') +
                    'route /src: s3:backup/code\n')
        self.assertEqual(dests_for_source('/src', cfg),
                         ([], [('bkt', 'backup/code')]))

    def test_longest_prefix_wins_home_e_home_code(self):
        cfg = parse('dest nas: /d/nas\ndest ext: /d/ext\n'
                    f'route ~: nas\nroute ~/code: ext\n')
        self.assertEqual(dests_for_source(HOME, cfg), (['/d/nas'], []))
        self.assertEqual(dests_for_source(os.path.join(HOME, 'code'), cfg),
                         (['/d/ext'], []))
        # sotto la sorgente più profonda: vince il prefisso più lungo
        self.assertEqual(
            dests_for_source(os.path.join(HOME, 'code', 'repo'), cfg),
            (['/d/ext'], []))

    def test_prefisso_per_segmenti_e_non_substring(self):
        cfg = parse('dest nas: /d/nas\ndest ext: /d/ext\nroute /a/b: ext\n')
        # `/a/bc` NON è sotto `/a/b` (segmento, non substring) → nessun match
        self.assertEqual(dests_for_source('/a/bc', cfg),
                         (['/d/nas', '/d/ext'], []))
        self.assertEqual(dests_for_source('/a/b', cfg), (['/d/ext'], []))
        self.assertEqual(dests_for_source('/a/b/c', cfg), (['/d/ext'], []))

    def test_prefisso_per_segmenti_con_route_home_e_codex(self):
        cfg = parse('dest nas: /d/nas\ndest ext: /d/ext\n'
                    'route ~: nas\nroute ~/code: ext\n')
        # `~/codex` non è sotto `~/code` → cade sulla route `~`
        self.assertEqual(
            dests_for_source(os.path.join(HOME, 'codex'), cfg),
            (['/d/nas'], []))

    def test_route_sulla_root_del_filesystem(self):
        cfg = parse('dest nas: /d/nas\nroute /: nas\n')
        self.assertEqual(dests_for_source('/qualcosa/anche', cfg),
                         (['/d/nas'], []))

    def test_root_normalizzata_prima_del_match(self):
        cfg = parse('dest nas: /d/nas\ndest ext: /d/ext\nroute /a/b: ext\n')
        self.assertEqual(dests_for_source('/a/b/', cfg), (['/d/ext'], []))
        self.assertEqual(dests_for_source('/a/b/../b', cfg), (['/d/ext'], []))

    def test_route_con_fs_e_s3_insieme(self):
        cfg = parse('dest nas: /d/nas\ndest ext: /d/ext\n' + s3_block() +
                    'route /src: ext, s3:pfx, nas\n')
        self.assertEqual(dests_for_source('/src', cfg),
                         (['/d/ext', '/d/nas'], [('bkt', 'pfx')]))

    def test_route_su_una_sorgente_non_mozza_le_altre(self):
        # il routing è opt-in per sorgente (§21.1): una route su un altro src
        # non tocca questa root, che resta su tutte le dest (default)
        cfg = parse('dest: /d1\ndest nas: /d2\nroute /altro: nas\n')
        self.assertEqual(dests_for_source('/src', cfg), (['/d1', '/d2'], []))


if __name__ == '__main__':
    unittest.main()
