import contextlib
import csv
import hashlib
import io
import os
import shutil
import stat
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from tidy_downloads import cli
from fixtures import make_tree

EXAMPLE = Path(__file__).parents[1] / 'src' / 'tidy_downloads' / 'rules.example.toml'


def md5s(root):
    """Content hashes of every file under root, as a multiset."""
    out = Counter()
    for dp, _, fs in os.walk(root):
        for f in fs:
            if not f.startswith('.'):
                out[hashlib.md5(Path(dp, f).read_bytes()).hexdigest()] += 1
    return out


def tree(root):
    return {os.path.relpath(os.path.join(dp, f), root): hashlib.md5(Path(dp, f).read_bytes()).hexdigest()
            for dp, _, fs in os.walk(root) for f in fs}


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src, self.dst, self.state = (os.path.join(self.tmp, x) for x in ('Downloads', 'Documents', 'state'))
        os.makedirs(self.src)
        self.n = make_tree(self.src)
        self.before = tree(self.src)
        os.environ['TIDY_DOWNLOADS_STATE'] = self.state

    def tearDown(self):
        shutil.rmtree(self.tmp)
        os.environ.pop('TIDY_DOWNLOADS_STATE')

    def run_cli(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(['--source', self.src, '--target', self.dst, '--rules', str(EXAMPLE), *args])
        return buf.getvalue()

    def plan_rows(self):
        run = cli.latest_run()
        return cli.read_plan(run)

    def test_plan_moves_nothing_and_explains(self):
        self.run_cli('scan')
        out = self.run_cli('plan')
        self.assertEqual(tree(self.src), self.before)
        self.assertIn('Unidentified:', out)
        rows = {r['source']: r for r in self.plan_rows()}
        self.assertEqual(rows['report (1).pdf']['rule'], 'duplicate')
        self.assertEqual(rows['report.pdf']['rule'], 'reading')
        self.assertEqual(rows['Board\xa0pack Q1']['destination'], 'Acme/Admin/Board/Board pack Q1')  # known folder despite \xa0
        self.assertTrue(rows['Random project']['destination'].startswith('Archive/2025/Folders/'))
        self.assertEqual(rows['calendar.ics']['rule'], 'unknown')
        self.assertTrue(rows['PDF/lease renewal.pdf']['destination'].startswith('Personal/Home/'))
        self.assertEqual(rows['file_0042.docx']['destination'], 'Old Studio/HR & payroll/file_0042.docx')  # read from the text
        if shutil.which('pdftotext'):
            self.assertEqual(rows['scan_0001.pdf']['destination'], 'Archive/2025/Scans to identify/scan_0001.pdf')
            self.assertEqual(rows['document.pdf']['destination'], 'Acme/Admin/Legal/document.pdf')
        out = self.run_cli('explain', os.path.join(self.src, 'Globex NDA.pdf'))
        self.assertIn('Acme/Clients/Globex', out)
        self.assertIn("'globex'", out)

    def test_apply_then_undo_restores_everything(self):
        self.run_cli('plan')
        out = self.run_cli('apply', '--limit', '1')
        self.assertIn('moved 1', out)
        self.run_cli('apply')
        self.assertEqual(sum(len(fs) for _, _, fs in os.walk(self.src)), 0)
        self.assertEqual(md5s(self.dst), Counter(self.before.values()))   # nothing lost, nothing doubled
        self.run_cli('undo')
        self.assertEqual(tree(self.src), self.before)
        self.assertEqual(sum(len(fs) for _, _, fs in os.walk(self.dst)), 0)

    def test_never_overwrites_and_archives_copies_already_in_target(self):
        self.run_cli('plan')
        rows = {r['source']: r for r in self.plan_rows()}
        same = Path(self.dst, rows['pitch.pptx']['destination'])
        other = Path(self.dst, rows['paper.txt']['destination'])
        for p, content in ((same, Path(self.src, 'pitch.pptx').read_bytes()), (other, b'different')):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content)
        self.run_cli('apply')
        self.assertEqual(other.read_bytes(), b'different')
        self.assertTrue(Path(str(other).replace('paper.txt', 'paper (dl-2).txt')).exists())
        self.assertTrue(Path(self.dst, 'Archive/2025/Duplicates (already in target)/pitch.pptx').exists())

    def test_interruption_then_reconcile(self):
        self.run_cli('plan')
        self.run_cli('apply', '--limit', '30', '--workers', '1')
        run = cli.latest_run()
        done = {line.split('\t')[0] for line in cli.read_journal(run)}
        # a crash between the move and the journal line: the file moved, the journal does not know
        row = next(r for r in self.plan_rows() if r['source'] not in done and r['type'] == 'file')
        dst = Path(self.dst, row['destination'])
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.rename(os.path.join(self.src, *row['source'].split('/')), dst)
        Path(cli.tmp_name(str(Path(self.dst, self.plan_rows()[-1]['destination'])))).parent.mkdir(parents=True, exist_ok=True)
        Path(cli.tmp_name(str(Path(self.dst, self.plan_rows()[-1]['destination'])))).write_bytes(b'half')  # a copy cut short
        out = self.run_cli('reconcile')
        self.assertIn('added to the journal 1', out)
        self.assertIn('partial copies 1', out)
        Path(cli.tmp_name(str(Path(self.dst, self.plan_rows()[-1]['destination'])))).unlink()
        self.run_cli('apply')
        self.assertEqual(md5s(self.dst), Counter(self.before.values()))
        self.run_cli('undo')
        self.assertEqual(tree(self.src), self.before)

    def test_across_volumes_copies_with_a_short_temporary_name(self):
        real = os.rename
        seen = []

        def rename(a, b):
            if os.path.abspath(a).startswith(self.src):   # as if Downloads were on another disk
                raise OSError(18, 'Invalid cross-device link')
            seen.append(os.path.basename(a))
            return real(a, b)
        self.run_cli('plan')
        with mock.patch('os.rename', rename):
            self.run_cli('apply')
        self.assertEqual(md5s(self.dst), Counter(self.before.values()))
        self.assertTrue(seen and all(len(n.encode()) < 20 for n in seen))  # never '<244-byte name>.part'

    @unittest.skipIf(os.name == 'nt' or os.geteuid() == 0, 'needs POSIX permissions')
    def test_source_that_cannot_be_deleted_is_not_duplicated(self):
        self.run_cli('plan')
        os.chmod(self.src, stat.S_IRUSR | stat.S_IXUSR)  # read-only Downloads
        try:
            real = os.rename
            cross = lambda a, b: (_ for _ in ()).throw(OSError(18, 'cross-device')) if os.path.abspath(a).startswith(self.src) else real(a, b)
            with mock.patch('os.rename', cross), self.assertRaises(SystemExit):
                self.run_cli('apply', '--limit', '3')
        finally:
            os.chmod(self.src, stat.S_IRWXU)
        self.assertEqual(sum(len(fs) for _, _, fs in os.walk(self.dst)), 0)  # no copy, no temporary file
        self.assertEqual(tree(self.src), self.before)

    @unittest.skipIf(os.name == 'nt', 'uses a shell script as a fake claude')
    def test_llm_only_accepts_folders_from_the_plan(self):
        fake = os.path.join(self.tmp, 'fake-claude')
        Path(fake).write_text('#!/bin/sh\nprintf "0\\tAcme/Admin/Legal\\n1\\t../../etc\\n"\n')
        os.chmod(fake, 0o755)
        with mock.patch.dict(os.environ, {'TIDY_LLM_CMD': fake}):
            out = self.run_cli('plan', '--llm', '--yes')
        self.assertIn('Claude placed 1 of', out)
        self.assertEqual(sum(r['rule'] == 'llm' for r in self.plan_rows()), 1)

    def test_plan_rejects_destination_outside_target(self):
        self.run_cli('plan')
        run = cli.latest_run()
        rows = self.plan_rows()
        rows[0]['destination'] = '../escape.pdf'
        with open(run / 'plan.csv', 'w', newline='', encoding='utf8') as f:
            w = csv.DictWriter(f, fieldnames=rows[0].keys())
            w.writeheader()
            w.writerows(rows[:1])
        with self.assertRaises(SystemExit):
            self.run_cli('apply')
        self.assertFalse(Path(self.tmp, 'escape.pdf').exists())


if __name__ == '__main__':
    unittest.main()
