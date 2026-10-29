"""Classification engine: loads the rules file and decides where one file goes.

Generic by design: every name, keyword and folder comes from the rules file.
The only built-in knowledge is file-type heuristics (photos, screenshots, media).
"""
import datetime
import re
import tomllib
import unicodedata
from dataclasses import dataclass, field


# ------------------------------------------------------------------ text helpers
def norm(s):
    """Lower case, accents removed, NFC/NFD and non-breaking spaces made equal (macOS names are NFD)."""
    s = unicodedata.normalize('NFKD', s.lower())
    return ''.join(ch for ch in s if not unicodedata.combining(ch)).replace('’', "'")


def folder_key(s):
    return unicodedata.normalize('NFC', s).replace('\xa0', ' ').strip()


_RX = {}


def hit(h, words):
    """First word found in h, or None. Words of 4 letters or fewer need a boundary:
    'nda' must not match 'calendar', 'irm' not 'confirmation'. A digit counts as a boundary."""
    for w in words:
        if len(w) <= 4 and w.isalpha():
            r = _RX.get(w) or _RX.setdefault(w, re.compile(r'(?<![a-z])' + re.escape(w) + r'(?![a-z])'))
            if r.search(h):
                return w
        elif w in h:
            return w
    return None


def name_date(n):
    """A date written in a file name: 2025-03-14, 20250314, 14-03-2025, 2025-03, or invoice numbers like 2503-F-2025."""
    m = re.search(r'(?<!\d)(20[12]\d)[-_. ]?(0[1-9]|1[0-2])[-_. ]?(0[1-9]|[12]\d|3[01])?(?!\d{2})', n)
    if m:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3) or 1))
    m = re.search(r'(?<!\d)(0[1-9]|[12]\d|3[01])[-_. ]?(0[1-9]|1[0-2])[-_. ]?(20[12]\d)(?!\d)', n)
    if m:
        return datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    m = re.search(r'(?<!\d)(2[0-9])(0[1-9]|1[0-2])-[fda]-20\d\d', n)
    if m:
        return datetime.date(2000 + int(m.group(1)), int(m.group(2)), 1)
    return None


def file_date(base, mtime):
    """In a downloads folder the modification date is the download date. A date in the name wins when plausible."""
    d = name_date(base)
    if d and datetime.date(2015, 1, 1) <= d <= mtime + datetime.timedelta(days=60):
        return d
    return mtime


# ------------------------------------------------------------------ built-in file-type heuristics
IMAGE_EXTS = ('png', 'jpg', 'jpeg', 'webp', 'gif', 'svg', 'bmp', 'tif', 'tiff')
ARCHIVE_EXTS = ('zip', 'rar', '7z', 'gz', 'tar', 'dmg', 'pkg', 'exe', 'msi', 'iso')


def media(b, ext):
    if ext == 'heic' or re.search(r'^(img|dsc|dscn|pxl|mvimg)[_ -]?\d', b) \
            or re.search(r'^(photo-20|whatsapp image|image jpeg|image_20\d{6})', b) or re.search(r'^\d{10}(\d{3})?\.jpe?g$', b):
        return 'photo'
    if re.search(r'^(screenshot|capture d.ecran|capture|screen shot|simulator screen)', b):
        return 'screenshot'
    if ext in ('mp4', 'mov', 'mkv', 'avi', 'webm'):
        return 'video'
    if ext in ('m4a', 'mp3', 'ogg', 'wav', 'aac', 'opus') or b.startswith('whatsapp ptt'):
        return 'audio'
    if ext in IMAGE_EXTS[:6] and (
            re.search(r'^(output|image|untitled|download|telechargement|file|frame|group|gemini_generated_image|chatgpt image|dall.e|image codex)[ _()\d-]*', b)
            or re.search(r'^[0-9a-f]{8}-[0-9a-f]{4}-', b) or re.search(r'^(g|h)[a-z0-9_-]{10,14}\.jpe?g$', b) or re.search(r'^\d{1,4}\.(png|jpe?g)$', b)):
        return 'image'
    return None


# ------------------------------------------------------------------ rules file
MEDIA_DEFAULTS = {
    'photo': 'Personal/Photos/{year}', 'screenshot': '{archive}/{year}/Screenshots', 'video': '{archive}/{year}/Videos',
    'audio': '{archive}/{year}/Audio', 'image': '{archive}/{year}/Images', 'archive_file': '{archive}/{year}/Archives',
    'unknown': '{archive}/{year}/Unidentified', 'duplicate': '{archive}/{year}/Duplicates',
    'unknown_folder': '{archive}/{year}/Folders', 'already_in_target': '{archive}/{year}/Duplicates (already in target)',
}


@dataclass
class Entity:
    folder: str
    aliases: list = field(default_factory=list)
    ids: list = field(default_factory=list)
    start: datetime.date | None = None
    end: datetime.date | None = None
    themes: dict = field(default_factory=dict)  # theme id -> folder, overrides the theme default


class Rules:
    def __init__(self, data):
        s = data.get('settings', {})
        self.archive = s.get('archive', 'Archive')
        self.descend_dirs = set(s.get('descend_dirs', []))
        self.ignore_dirs = {folder_key(x) for x in s.get('ignore_dirs', [])}
        self.text_exts = s.get('text_exts', ['pdf', 'docx', 'doc'])
        self.media = {**MEDIA_DEFAULTS, **data.get('media', {})}
        self.folders = {folder_key(k): v for k, v in data.get('folders', {}).items()}
        self.entities = [Entity(e['folder'], _kw(e.get('aliases')), _kw(e.get('ids')), e.get('start'), e.get('end'),
                                e.get('themes', {})) for e in data.get('entities', [])]
        alias_rx = '|'.join(re.escape(a) for e in self.entities for a in sorted(e.aliases, key=len, reverse=True))
        self.alias_rx = re.compile(alias_rx) if alias_rx else None
        self.rules = {stage: [_rule(r) for r in data.get('rules', []) if r.get('stage', 'pro') == stage] for stage in ('personal', 'pro')}
        self.themes = [_theme(t) for t in data.get('themes', [])]
        self.theme_dest = {t['id']: t['dest'] for t in self.themes}
        self.admin = {t['id'] for t in self.themes if t.get('admin')}
        self.text_themes = [dict(t, keywords=_kw(t['keywords'])) for t in data.get('text_themes', [])]
        self.routes = [dict(r, keywords=_kw(r['keywords'])) for r in data.get('routes', [])]

    @classmethod
    def load(cls, path):
        with open(path, 'rb') as f:
            return cls(tomllib.load(f))


def _kw(words):
    return [norm(w) for w in (words or [])]


def _rule(r):
    r = dict(r)
    for k in ('keywords', 'and_keywords', 'text_keywords', 'and_text_keywords', 'unless', 'unless_text'):
        if k in r:
            r[k] = _kw(r[k])
    for k in ('name', 'path', 'text'):
        if k in r:
            r[k] = re.compile(r[k])
    return r


def _theme(t):
    t = dict(t, keywords=_kw(t.get('keywords')), patterns=[re.compile(p) for p in t.get('patterns', [])])
    if 'entity_name_exempt' in t:
        t['entity_name_exempt'] = re.compile(t['entity_name_exempt'])
    return t


# ------------------------------------------------------------------ classification
@dataclass
class Decision:
    folder: str      # destination folder, relative to the target
    rule: str        # which stage decided
    date: datetime.date
    why: str         # what matched, for `explain`


def match_rule(r, nn, b, tt):
    """All conditions present in the rule must hold. Returns what matched, or None."""
    why = []
    for key, hay in (('keywords', nn), ('and_keywords', nn), ('text_keywords', tt), ('and_text_keywords', tt)):
        if key in r:
            w = hit(hay, r[key])
            if not w:
                return None
            why.append(f'{key}={w!r}')
    for key, hay in (('name', b), ('path', nn), ('text', tt)):
        if key in r:
            m = r[key].search(hay)
            if not m:
                return None
            why.append(f'{key}~{m.group(0)!r}')
    if 'unless' in r and hit(nn, r['unless']) or 'unless_text' in r and hit(tt, r['unless_text']):
        return None
    return ', '.join(why)


def pick_entity(R, nn, tt, d):
    """An identifier or a name in the file wins; otherwise the entity active on that date."""
    for e in R.entities:
        w = hit(tt, e.ids) or hit(nn, e.ids)
        if w:
            return e, f'id {w!r}'
        w = hit(nn, e.aliases) or hit(tt, e.aliases)
        if w and (e.end is None or d < e.end):
            return e, f'name {w!r}'
    for e in R.entities:
        if (e.start is None or d >= e.start) and (e.end is None or d < e.end):
            return e, f'date {d}'
    return (R.entities[-1], 'last entity') if R.entities else (None, 'no entity')


def find_theme(R, n, b, ext):
    for t in R.themes:
        w = next((p.pattern for p in t['patterns'] if p.search(n)), None)
        if not w and t['keywords'] and (not t.get('require_exts') or ext in t['require_exts']):
            w = hit(n, t['keywords'])
            w = w and repr(w)
        if not w and ext in t.get('exts', []):
            w = f'.{ext}'
        if w:
            if t.get('needs_entity_in_name') and not (R.alias_rx and R.alias_rx.search(b)) \
                    and not ('entity_name_exempt' in t and t['entity_name_exempt'].search(b)):
                return t['otherwise'], f'{w}, but no entity in the name'
            return t['id'], w
    return None, None


def classify(R, rel, mtime, text=''):
    """rel: path relative to the source, with '/' separators. mtime: datetime.date."""
    base = rel.rsplit('/', 1)[-1]
    b = norm(base)
    ext = b.rsplit('.', 1)[-1] if '.' in b else ''
    ctx = '/'.join(p for p in rel.split('/')[:-1] if p not in R.descend_dirs and not re.fullmatch(r'20\d\d', p))
    nn = norm(ctx + '/' + base)
    tt = norm(text or '')
    d = file_date(base, mtime)

    def fill(dest, ent=None):
        dest = dest.replace('{entity}', ent.folder if ent else '').replace('{year}', str(d.year)).replace('{archive}', R.archive)
        dest = re.sub(r'\{theme:(\w+)\}', lambda m: (ent.themes.get(m[1]) if ent else None) or R.theme_dest[m[1]], dest)
        return dest.strip('/')

    for r in R.rules['personal']:
        why = match_rule(r, nn, b, tt)
        if why is not None:
            return Decision(fill(r['dest']), 'personal', d, why)
    m = media(b, ext)
    if m:
        return Decision(fill(R.media[m]), 'photo' if m == 'photo' else 'media', d, m)

    ent, ent_why = pick_entity(R, nn, tt, d)
    for r in R.rules['pro']:
        why = match_rule(r, nn, b, tt)
        if why is not None:
            return Decision(fill(r['dest'], ent), 'rule', d, f'{why}; entity by {ent_why}')

    stripped = R.alias_rx.sub(' ', nn) if R.alias_rx else nn  # 'Apple' in a company name must not mean fruit
    th, why = find_theme(R, stripped, b, ext)
    if th is None and tt:
        for t in R.text_themes:
            if ext in t.get('exts', R.text_exts):
                w = hit(tt[:t.get('window', 500)], t['keywords'])
                if w:
                    th, why = t['theme'], f'text {w!r}'
                    break
    if th not in R.admin and ent:
        for r in R.routes:
            if r['entity'] == ent.folder:
                w = hit(nn, r['keywords'])
                if w:
                    return Decision(fill(f"{ent.folder}/{r['dest']}"), r.get('label', 'route'), d, f'{w!r}; entity by {ent_why}')
    if th is None:
        kind = 'image' if ext in IMAGE_EXTS else 'archive_file' if ext in ARCHIVE_EXTS else 'unknown'
        return Decision(fill(R.media[kind]), 'unknown' if kind == 'unknown' else 'media', d, 'no rule matched')
    folder = (ent.themes.get(th) if ent else None) or R.theme_dest[th]
    return Decision(fill(f'{ent.folder}/{folder}' if ent else folder), th, d, f'{why}; entity by {ent_why}')
