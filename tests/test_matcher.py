import unittest

from safekeep.matcher import BUILTIN_RULES, Matcher, parse_rule, parse_rules


def matcher(*rule_lines):
    """Matcher puro sulle regole date (i builtin sono testati a parte)."""
    return Matcher(parse_rules(rule_lines))


class PatternMatrixTest(unittest.TestCase):
    # (regole, path, is_dir, atteso) — ≥20 casi path × regole
    # (regole, path, is_dir, atteso) — ≥20 casi path × regole.
    # `include: **` nei casi che verificano il matching di un exclude serve a
    # isolare l'ancoraggio dal default allow-list (file no-match → False).
    CASES = [
        # last-match-wins: exclude sotto include batte
        (['include: *.md', 'exclude: docs/vendor/CHANGELOG.md'], 'docs/vendor/CHANGELOG.md', False, False),
        (['include: *.md', 'exclude: docs/vendor/CHANGELOG.md'], 'README.md', False, True),
        (['include: *.md', 'exclude: docs/vendor/CHANGELOG.md'], 'docs/other.md', False, True),
        # last-match-wins: include più in basso ri-include
        (['exclude: *.log', 'include: important.log'], 'important.log', False, True),
        (['exclude: *.log', 'include: important.log'], 'other.log', False, False),
        (['exclude: node_modules/', 'include: node_modules/LICENSE-key.txt'], 'node_modules/LICENSE-key.txt', False, True),
        (['exclude: node_modules/', 'include: node_modules/LICENSE-key.txt'], 'node_modules/x.js', False, False),
        (['exclude: node_modules/', 'include: node_modules/LICENSE-key.txt'], 'node_modules', True, False),
        # nessuna regola matcha → allow-list: file NON copiato, dir attraversata
        (['exclude: *.log'], 'main.py', False, False),
        (['exclude: *.log'], 'src', True, True),
        (['include: *.md'], 'x.txt', False, False),
        (['include: *.md'], 'docs', True, True),
        (['include: docs/*.md'], 'docs', True, True),      # dir con include interno → attraversata
        (['include: docs/*.md'], 'docs/note.md', False, True),
        (['exclude: docs/'], 'docs', True, False),         # dir esclusa esplicita → potata
        (['include: .env'], '.env', False, True),
        (['include: .env'], '.env.example', False, False), # .env.* va scritto esplicitamente
        # ancoraggio: /foo (radice) vs foo (basename ovunque)
        (['include: **', 'exclude: /foo'], 'foo', False, False),
        (['include: **', 'exclude: /foo'], 'foo', True, False),
        (['include: **', 'exclude: /foo'], 'a/foo', False, True),
        (['include: **', 'exclude: /foo'], 'a/b/foo', False, True),
        (['include: **', 'exclude: foo'], 'foo', False, False),
        (['include: **', 'exclude: foo'], 'a/b/foo', False, False),
        # slash interna = ancorato alla root
        (['include: **', 'exclude: docs/a.md'], 'docs/a.md', False, False),
        (['include: **', 'exclude: docs/a.md'], 'x/docs/a.md', False, True),
        # *.md a qualsiasi profondità (basename)
        (['exclude: *.md'], 'README.md', False, False),
        (['exclude: *.md'], 'a/b/c.md', False, False),
        # * non attraversa /
        (['include: **', 'exclude: docs/*.md'], 'docs/a.md', False, False),
        (['include: **', 'exclude: docs/*.md'], 'docs/sub/a.md', False, True),
        # ** (dir/** matcha anche la dir stessa → pruning)
        (['exclude: docs/**'], 'docs', True, False),
        (['exclude: docs/**'], 'docs/a/b.txt', False, False),
        (['include: **', 'exclude: docs/**'], 'docsx', False, True),
        (['include: **', 'exclude: docs/**'], 'a/docs/b', False, True),
        (['include: docs/**', 'exclude: docs/vendor/CHANGELOG.md'], 'docs', True, True),
        (['include: docs/**', 'exclude: docs/vendor/CHANGELOG.md'], 'docs/vendor/CHANGELOG.md', False, False),
        (['include: **', 'exclude: a/**/b'], 'a/b', False, False),
        (['include: **', 'exclude: a/**/b'], 'a/x/y/b', False, False),
        (['include: **', 'exclude: a/**/b'], 'a/x/b.txt', False, True),
        # foo/ solo directory
        (['exclude: .vault/'], '.vault', True, False),
        (['exclude: .vault/'], '.vault/f.txt', False, False),
        (['include: **', 'exclude: .vault/'], '.vault', False, True),  # file omonimo: non è dir
        (['exclude: dir/'], 'dir', True, False),
        (['exclude: dir/'], 'other/dir/x', False, False),
        (['include: **', 'exclude: dir/'], 'other/dirfile', False, True),
        # ? e [abc]
        (['exclude: file?.txt'], 'file1.txt', False, False),
        (['include: **', 'exclude: file?.txt'], 'file12.txt', False, True),
        (['exclude: file?.txt'], 'd/fileA.txt', False, False),
        (['exclude: [abc].log'], 'a.log', False, False),
        (['include: **', 'exclude: [abc].log'], 'd.log', False, True),
        (['exclude: [abc].log'], 'x/a.log', False, False),
        (['include: **', 'exclude: [!a].log'], 'a.log', False, True),
        (['exclude: [!a].log'], 'b.log', False, False),
        # commenti ed escape
        (['# commento', '', '   ', 'exclude: \\#foo'], '#foo', False, False),
        (['# commento', '', '   ', 'include: **', 'exclude: \\#foo'], 'foo', False, True),
        (['exclude: \\#foo'], 'x/#foo', False, False),
        (['exclude: *.log   # i log'], 'a.log', False, False),
        # recipe "copia tutto tranne X": include: ** copre tutto
        (['include: **'], 'qualunque/file.bin', False, True),
        (['include: **', 'exclude: *.tmp'], 'x.tmp', False, False),
    ]

    def test_matrix(self):
        self.assertGreaterEqual(len(self.CASES), 20)
        for i, (rules, path, is_dir, expected) in enumerate(self.CASES):
            with self.subTest(i=i, rules=rules, path=path):
                self.assertEqual(matcher(*rules).evaluate(path, is_dir), expected)

    def test_path_con_slash_finali(self):
        # fswatch può emettere `dir/` per le directory
        self.assertFalse(matcher('exclude: foo/').evaluate('foo/', True))


class BuiltinTest(unittest.TestCase):
    def test_builtin_excludes(self):
        m = Matcher(BUILTIN_RULES)
        for path, is_dir in [('.git', True), ('.git/config', False), ('node_modules', True),
                             ('a/node_modules', True), ('.venv', True), ('x/__pycache__', True),
                             ('.DS_Store', False), ('a/b/.DS_Store', False), ('main.swp', False),
                             ('sub/deep/x.swp', False)]:
            with self.subTest(path=path):
                self.assertFalse(m.evaluate(path, is_dir))

    def test_builtin_solo_non_copiano_i_file_ma_attraversano_le_dir(self):
        m = Matcher(BUILTIN_RULES)
        # allow-list: senza nessun include il file non matchato non si copia…
        self.assertFalse(m.evaluate('main.py', False))
        self.assertFalse(m.evaluate('docs/a.md', False))
        # …ma le directory non esclusa vengono attraversate (pruning solo su exclude)
        self.assertTrue(m.evaluate('docs', True))
        self.assertTrue(m.evaluate('a/b', True))


class LayeringTest(unittest.TestCase):
    def test_ordine_builtin_globale_progetto(self):
        glob = parse_rules(['exclude: *.tmp', 'exclude: *.bak', 'include: .DS_Store'])
        proj_defaults = parse_rules(['include: keep.tmp', 'include: keep.bak'])
        proj_rules = parse_rules(['exclude: keep.tmp'])
        self.assertFalse(Matcher(BUILTIN_RULES).evaluate('.DS_Store', False))
        self.assertTrue(Matcher(BUILTIN_RULES, glob).evaluate('.DS_Store', False))   # globale > builtin
        self.assertFalse(Matcher(BUILTIN_RULES, glob).evaluate('x.tmp', False))
        base = Matcher(BUILTIN_RULES, glob, proj_defaults)
        self.assertTrue(base.evaluate('keep.bak', False))                             # defaults progetto > globale
        self.assertTrue(base.evaluate('keep.tmp', False))
        full = Matcher(BUILTIN_RULES, glob, proj_defaults, proj_rules)
        self.assertFalse(full.evaluate('keep.tmp', False))                            # regole .sync > defaults

    def test_matcher_vuoto_non_copia_i_file_ma_attraversa_le_dir(self):
        m = Matcher()
        self.assertFalse(m.evaluate('qualunque/path', False))   # allow-list: file no-match
        self.assertTrue(m.evaluate('qualunque', True))          # dir no-match → attraversata


class ParseRulesTest(unittest.TestCase):
    def test_commenti_e_righe_vuote(self):
        rules = parse_rules(['# commento', '', '   ', 'include: a.txt', 'exclude: b.txt'])
        self.assertEqual([(r.pattern, r.include) for r in rules],
                         [('a.txt', True), ('b.txt', False)])

    def test_riga_invalida_con_numero_riga(self):
        with self.assertRaises(ValueError) as cm:
            parse_rules(['include: a', 'riga nuda', 'exclude: b'], origin='x.sync')
        self.assertIn('x.sync:2', str(cm.exception))

    def test_pattern_vuoto_errore(self):
        with self.assertRaises(ValueError) as cm:
            parse_rules(['exclude:'], origin='e.sync')
        self.assertIn('e.sync:1', str(cm.exception))

    def test_parse_rule(self):
        self.assertIsNone(parse_rule('# x'))
        self.assertIsNone(parse_rule(''))
        self.assertTrue(parse_rule('include: x').include)
        self.assertFalse(parse_rule('exclude: x').include)
        self.assertEqual(parse_rule('  exclude: docs/**  ').pattern, 'docs/**')

    def test_escape_commento_nel_pattern(self):
        r = parse_rule(r'exclude: \#foo')
        self.assertFalse(Matcher([r]).evaluate('#foo', False))


class PatternInvalidoTest(unittest.TestCase):
    """REGRESSIONE CR-03: `re.error` NON è un ValueError, un `.sync` malevolo
    (`[z-a]*`, NUL) faceva esplodere parser e daemon."""

    def test_regex_invalida_diventa_valueerror(self):
        with self.assertRaises(ValueError) as cm:
            parse_rule('include: [z-a]*')
        self.assertIn('regex non valido', str(cm.exception))

    def test_pattern_con_nul_diventa_valueerror(self):
        with self.assertRaises(ValueError) as cm:
            parse_rule('exclude: a\0b')
        self.assertIn('NUL', str(cm.exception))

    def test_pattern_validi_invariati(self):
        r = parse_rule('include: [abc]*.md')
        self.assertTrue(r.include)
        self.assertTrue(Matcher([r]).evaluate('a.md'))


if __name__ == '__main__':
    unittest.main()
