"""tidy-downloads: sort a downloads folder into a target tree, from a plan you read first.

  scan       read the first lines of PDF, DOCX, XLSX, PPTX, TXT files into a local cache
  plan       decide a destination for every file and write plan.csv (moves nothing; the default)
  apply      carry out the latest plan: never overwrites, never deletes, journals every move
  undo       put files back from the journal
  reconcile  after an interruption, add to the journal the moves that happened but were not recorded
  explain    show which rule decides for one file, and why
"""
import argparse
import csv
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .engine import Rules, classify, folder_key

SKIP = {'.DS_Store', '.localized', 'desktop.ini', 'Thumbs.db'}
TEXT_EXTS = ('pdf', 'docx', 'xlsx', 'pptx', 'txt', 'md')
EXAMPLE = Path(__file__).with_name('rules.example.toml')


# ------------------------------------------------------------------ places
def config_path():
    if os.name == 'nt':
        return Path(os.environ.get('APPDATA', Path.home())) / 'tidy-downloads' / 'rules.toml'
    return Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'tidy-downloads' / 'rules.toml'


def state_dir():
    """Text cache, plans and journals: personal data, kept out of any repository."""
    if os.environ.get('TIDY_DOWNLOADS_STATE'):
        return Path(os.environ['TIDY_DOWNLOADS_STATE'])
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'tidy-downloads'
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'tidy-downloads'
    return Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local' / 'state')) / 'tidy-downloads'


def latest_run(run=None):
    runs = state_dir() / 'runs'
    names = sorted(p.name for p in runs.iterdir()) if runs.is_dir() else []
    if not names:
        sys.exit('No plan yet. Run: tidy-downloads plan')
    return runs / (run or names[-1])


def full(root, rel):
    return os.path.join(root, *rel.split('/'))


def read_plan(run):
    with open(run / 'plan.csv', encoding='utf8') as f:
        return list(csv.DictReader(f))


def read_journal(run):
    journal = run / 'journal.tsv'
    return journal.read_text(encoding='utf8').splitlines() if journal.exists() else []


def load_rules(path):
    if not Path(path).exists():
        sys.exit(f'No rules file at {path}.\nStart from the example: cp "{EXAMPLE}" "{path}"')
    return Rules.load(path)


# ------------------------------------------------------------------ scan
def xml_text(z, names):
    out = []
    for n in names:
        try:
            out.append(re.sub(r'<[^>]+>', ' ', z.read(n).decode('utf8', 'ignore')))
        except KeyError:
            pass
    return ' '.join(out)


def read_text(p):
    ext = p.lower().rsplit('.', 1)[-1]
    if ext == 'pdf':
        if shutil.which('pdftotext'):
            return subprocess.run(['pdftotext', '-l', '2', p, '-'], capture_output=True, timeout=15).stdout.decode('utf8', 'ignore')
        try:
            from pypdf import PdfReader  # optional
            return ' '.join((pg.extract_text() or '') for pg in PdfReader(p).pages[:2])
        except ImportError:
            return ''
    if ext in ('docx', 'xlsx', 'pptx'):
        with zipfile.ZipFile(p) as z:
            if ext == 'docx':
                return xml_text(z, ['word/document.xml'])
            if ext == 'pptx':
                return xml_text(z, sorted(x for x in z.namelist() if re.match(r'ppt/slides/slide\d+\.xml', x))[:3])
            return xml_text(z, ['xl/workbook.xml', 'xl/sharedStrings.xml'])
    with open(p, errors='ignore') as f:
        return f.read(1500)


def walk_files(source):
    for dp, dn, fn in os.walk(source):
        dn[:] = [d for d in dn if not d.startswith('.') and d not in ('node_modules', '.git')]
        for f in fn:
            if f not in SKIP and not f.startswith('.'):
                yield os.path.relpath(os.path.join(dp, f), source).replace(os.sep, '/')


def cmd_scan(a):
    cache_file = state_dir() / 'text_cache.json'
    cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    deadline = time.time() + a.budget if a.budget else None
    n = 0
    for rel in walk_files(a.source):
        if rel in cache or rel.lower().rsplit('.', 1)[-1] not in TEXT_EXTS:
            continue
        if deadline and time.time() > deadline:
            break
        try:
            t = read_text(full(a.source, rel))
        except Exception:
            t = ''
        cache[rel] = re.sub(r'\s+', ' ', t)[:600]
        n += 1
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(cache, ensure_ascii=False))
    print(f'read {n} new files, {len(cache)} in cache ({cache_file})')


# ------------------------------------------------------------------ plan
def md5(p):
    h = hashlib.md5()
    with open(p, 'rb') as f:
        for ch in iter(lambda: f.read(1 << 20), b''):
            h.update(ch)
    return h.hexdigest()


def dup_rank(rel):
    """Keep the copy without '(1)' or 'copy' in its name, then the shortest."""
    b = rel.rsplit('/', 1)[-1]
    return bool(re.search(r'\(\d+\)| cop(y|ie)|_\(\d\)| \d\.', b, re.I)), len(b), rel


def mdate(p):
    return datetime.date.fromtimestamp(os.stat(p).st_mtime)


def build_plan(R, source, cache):
    rows, files = [], []
    for e in sorted(os.listdir(source)):
        p = os.path.join(source, e)
        if e in SKIP or e.startswith('.') or folder_key(e) in R.ignore_dirs:
            continue
        if os.path.isdir(p) and e not in R.descend_dirs:
            inside = [os.path.join(dp, f) for dp, _, fs in os.walk(p) for f in fs]
            if not inside:
                continue
            mt = datetime.date.fromtimestamp(max(os.stat(f).st_mtime for f in inside))
            dest = R.folders.get(folder_key(e))
            if dest:
                rows.append([e, f'{dest}/{folder_key(e)}', 'folder', mt.isoformat(), 'folder', 'known folder'])
            else:
                rows.append([e, f"{fill_media(R, 'unknown_folder', mt)}/{e}", 'unknown folder', mt.isoformat(), 'folder', ''])
        elif os.path.isdir(p):
            files += [f'{e}/{r}' for r in walk_files(p)]
        elif os.path.isfile(p):
            files.append(e)
    bysize = {}
    for r in files:
        bysize.setdefault(os.path.getsize(full(source, r)), []).append(r)
    dups = {}
    for size, grp in bysize.items():
        if len(grp) < 2 or size == 0:
            continue
        byhash = {}
        for r in grp:
            byhash.setdefault(md5(full(source, r)), []).append(r)
        for g in byhash.values():
            g.sort(key=dup_rank)
            for r in g[1:]:
                dups[r] = g[0]
    for r in files:
        mt = mdate(full(source, r))
        base = r.rsplit('/', 1)[-1]
        if r in dups:
            rows.append([r, f"{fill_media(R, 'duplicate', mt)}/{base}", 'duplicate', mt.isoformat(), 'file', f'same content as {dups[r]}'])
            continue
        d = classify(R, r, mt, cache.get(r, ''))
        rows.append([r, f'{d.folder}/{base}', d.rule, d.date.isoformat(), 'file', d.why])
    return rows


def fill_media(R, kind, d):
    return R.media[kind].replace('{archive}', R.archive).replace('{year}', str(d.year))


def summary(rows, source):
    vol, cnt = Counter(), Counter()
    for r in rows:
        folder = r[1].rsplit('/', 1)[0]
        p = full(source, r[0])
        size = os.path.getsize(p) if os.path.isfile(p) else sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(p) for f in fs)
        vol[folder] += size
        cnt[folder] += 1
    total = sum(vol.values())
    print(f'\n{len(rows)} items, {total / 1e9:.2f} GB, into {len(cnt)} folders\n')
    for folder, c in cnt.most_common(25):
        print(f'{c:6d}  {vol[folder] / 1e6:9.1f} MB  {folder}')
    if len(cnt) > 25:
        print(f'   ... and {len(cnt) - 25} more folders')
    unknown = [r[0] for r in rows if r[2] == 'unknown']
    print(f'\nUnidentified: {len(unknown)} ({len(unknown) / max(len(rows), 1):.1%})')
    for u in unknown[:20]:
        print('  ', u if len(u) < 90 else u[:60] + '...' + u[-20:])


def ask_llm(rows, cache, yes):
    """Optional: suggest a folder for unidentified files with Claude. Sends names and 300 characters of text."""
    unknown = [r for r in rows if r[2] == 'unknown']
    allowed = sorted({r[1].rsplit('/', 1)[0] for r in rows if r[2] not in ('unknown', 'duplicate', 'media', 'photo')})
    if not unknown or not allowed:
        return
    print(f'\n--llm sends {len(unknown)} file names and up to 300 characters of their text to Claude.'
          ' This leaves your machine. Text can contain personal data.')
    if not yes and input('Send? [y/N] ').strip().lower() != 'y':
        return
    items = '\n'.join(f"{i}\t{r[0].rsplit('/', 1)[-1]}\t{cache.get(r[0], '')[:300]}" for i, r in enumerate(unknown))
    prompt = ('Pick a destination folder for each file, only from this list, or "none".\nFolders:\n' + '\n'.join(allowed) +
              '\n\nFiles (index, name, start of text):\n' + items + '\n\nAnswer with one line per file: index<TAB>folder. Nothing else.')
    cmd = os.environ.get('TIDY_LLM_CMD', 'claude -p').split()
    out = subprocess.run(cmd + [prompt], capture_output=True, text=True, timeout=600).stdout
    n = 0
    for line in out.splitlines():
        i, _, folder = line.partition('\t')
        if i.strip().isdigit() and int(i) < len(unknown) and folder.strip() in allowed:  # never trust a folder outside the plan
            r = unknown[int(i)]
            r[1], r[2], r[5] = f"{folder.strip()}/{r[0].rsplit('/', 1)[-1]}", 'llm', 'suggested by Claude'
            n += 1
    print(f'Claude placed {n} of {len(unknown)} unidentified files (rule "llm" in the plan).')


def cmd_plan(a):
    R = load_rules(a.rules)
    cache_file = state_dir() / 'text_cache.json'
    cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    rows = build_plan(R, a.source, cache)
    if a.llm:
        ask_llm(rows, cache, a.yes)
    run = state_dir() / 'runs' / datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    run.mkdir(parents=True, exist_ok=True)
    (run / 'meta.json').write_text(json.dumps({'source': str(a.source), 'target': str(a.target)}))
    with open(run / 'plan.csv', 'w', newline='', encoding='utf8') as f:
        w = csv.writer(f)
        w.writerow(['source', 'destination', 'rule', 'date', 'type', 'why'])
        w.writerows(rows)
    summary(rows, a.source)
    print(f'\nPlan: {run / "plan.csv"}\nNothing has moved. Read it, then: tidy-downloads apply --limit 1')


# ------------------------------------------------------------------ apply, undo, reconcile
def tmp_name(dst):
    # short hashed name: appending '.part' to a 250-byte name would pass the 255-byte limit
    return os.path.join(os.path.dirname(dst), '.' + hashlib.md5(dst.encode()).hexdigest()[:12] + '.part')


def move_one(src, dst):
    """Rename when possible. Across volumes: copy to a temporary name, rename, then delete the source.
    The right to delete the source is checked first: without it, a copy would leave the file in two places."""
    try:
        os.rename(src, dst)
        return
    except OSError:
        pass
    if not os.access(os.path.dirname(os.path.abspath(src)), os.W_OK):
        raise PermissionError(f'no right to delete from {os.path.dirname(src)}; grant it, then run apply again')
    is_dir, tmp = os.path.isdir(src), tmp_name(dst)
    try:
        if is_dir:
            shutil.copytree(src, tmp, copy_function=shutil.copy2, dirs_exist_ok=True)
        else:
            shutil.copy2(src, tmp)
        os.rename(tmp, dst)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True) if is_dir else os.path.exists(tmp) and os.remove(tmp)
        raise
    shutil.rmtree(src) if is_dir else os.remove(src)  # dst now holds a full copy: a failure here loses nothing


def inside(root, p):
    return os.path.commonpath([os.path.abspath(root), os.path.abspath(p)]) == os.path.abspath(root)


def cmd_apply(a):
    run = latest_run(a.run)
    meta = json.loads((run / 'meta.json').read_text())
    source, target = meta['source'], meta['target']
    R = load_rules(a.rules)
    journal = run / 'journal.tsv'
    done = {line.split('\t')[0] for line in read_journal(run)}
    todo = [r for r in read_plan(run)
            if r['source'] not in done and os.path.exists(full(source, r['source']))]
    if a.limit:
        todo = todo[:a.limit]
    deadline = time.time() + a.budget if a.budget else None
    lock, reserved, moved, errors = threading.Lock(), set(), [0], []
    j = open(journal, 'a', encoding='utf8')

    def work(row):
        if deadline and time.time() > deadline:
            return
        src = full(source, row['source'])
        dst0 = full(target, row['destination'])
        if not inside(target, dst0):  # the plan is a file the user can edit
            errors.append(f"outside the target, skipped: {row['destination']}")
            return
        if os.path.isfile(src) and os.path.isfile(dst0) and os.path.getsize(src) == os.path.getsize(dst0) and md5(src) == md5(dst0):
            d = datetime.date.fromisoformat(row['date'])
            dst0 = os.path.join(full(target, fill_media(R, 'already_in_target', d)), os.path.basename(src))
        with lock:
            dst, i = dst0, 2
            root, ext = os.path.splitext(dst0)
            while os.path.exists(dst) or dst in reserved:
                dst, i = f'{root} (dl-{i}){ext}', i + 1
            reserved.add(dst)
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            move_one(src, dst)
        except OSError as e:
            errors.append(f"{row['source']}: {e}")
            return
        with lock:
            j.write(f"{row['source']}\t{os.path.relpath(dst, target).replace(os.sep, '/')}\n")
            j.flush()  # one line per move: an interruption loses nothing
            moved[0] += 1

    with ThreadPoolExecutor(a.workers) as ex:
        list(ex.map(work, todo))
    j.close()
    left = sum(1 for r in read_plan(run) if os.path.exists(full(source, r['source'])))
    print(f'moved {moved[0]}, left in the plan {left}, errors {len(errors)}')
    for e in errors[:20]:
        print('  ', e)
    if errors:
        sys.exit(1)


def cmd_undo(a):
    run = latest_run(a.run)
    meta = json.loads((run / 'meta.json').read_text())
    back = skipped = 0
    for line in reversed(read_journal(run)):
        s, d = line.split('\t')
        src, dst = full(meta['target'], d), full(meta['source'], s)
        if os.path.exists(src) and not os.path.exists(dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            move_one(src, dst)
            back += 1
        else:
            skipped += 1
    print(f'put back {back}, skipped {skipped} (already back, or something else now has that name)')


def cmd_reconcile(a):
    run = latest_run(a.run)
    meta = json.loads((run / 'meta.json').read_text())
    journal = run / 'journal.tsv'
    done = {line.split('\t')[0] for line in read_journal(run)}
    added, lost, partial = [], [], []
    for r in read_plan(run):
        if r['source'] in done:
            continue
        src, dst = full(meta['source'], r['source']), full(meta['target'], r['destination'])
        if os.path.exists(tmp_name(dst)):
            partial.append(tmp_name(dst))
        if os.path.exists(src):
            continue
        (added if os.path.exists(dst) else lost).append((r['source'], r['destination']))
    with open(journal, 'a', encoding='utf8') as j:
        for s, d in added:
            j.write(f'{s}\t{d}\n')
    print(f'added to the journal {len(added)}, not found {len(lost)}, partial copies {len(partial)}')
    for s, _ in lost[:20]:
        print('   not found:', s)
    for p in partial:
        print('   partial copy (compare with the source, then remove):', p)


def cmd_explain(a):
    R = load_rules(a.rules)
    p = os.path.abspath(a.file)
    rel = os.path.relpath(p, a.source).replace(os.sep, '/') if inside(a.source, p) else os.path.basename(p)
    cache_file = state_dir() / 'text_cache.json'
    text = json.loads(cache_file.read_text()).get(rel, '') if cache_file.exists() else ''
    d = classify(R, rel, mdate(p), text)
    print(f'file:        {rel}\ndestination: {d.folder}/\nstage:       {d.rule}\nwhy:         {d.why}\ndate used:   {d.date}\n'
          f'text read:   {len(text)} characters' + ('' if text else ' (run scan to use the text)'))


# ------------------------------------------------------------------ entry point
def main(argv=None):
    ap = argparse.ArgumentParser(prog='tidy-downloads', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--source', default=str(Path.home() / 'Downloads'), help='folder to sort (default: ~/Downloads)')
    ap.add_argument('--target', default=str(Path.home() / 'Documents'), help='where files go (default: ~/Documents)')
    ap.add_argument('--rules', default=str(config_path()), help=f'rules file (default: {config_path()})')
    ap.set_defaults(llm=False, yes=False)  # `tidy-downloads` alone runs plan
    sub = ap.add_subparsers(dest='cmd')
    s = sub.add_parser('scan')
    s.add_argument('--budget', type=float, help='stop after this many seconds; run again to continue')
    p = sub.add_parser('plan')
    p.add_argument('--llm', action='store_true', help='ask Claude about unidentified files (sends names and text, asks first)')
    p.add_argument('--yes', action='store_true', help='with --llm: do not ask for confirmation')
    for name in ('apply', 'undo', 'reconcile'):
        c = sub.add_parser(name)
        c.add_argument('--run', help='a run folder name (default: the latest plan)')
        if name == 'apply':
            c.add_argument('--limit', type=int, help='move at most this many items (try 1 first)')
            c.add_argument('--budget', type=float, help='stop after this many seconds; run again to continue')
            c.add_argument('--workers', type=int, default=8)
    e = sub.add_parser('explain')
    e.add_argument('file')
    a = ap.parse_args(argv)
    a.source, a.target = os.path.expanduser(a.source), os.path.expanduser(a.target)
    {'scan': cmd_scan, 'plan': cmd_plan, None: cmd_plan, 'apply': cmd_apply, 'undo': cmd_undo,
     'reconcile': cmd_reconcile, 'explain': cmd_explain}[a.cmd](a)


if __name__ == '__main__':
    main()
