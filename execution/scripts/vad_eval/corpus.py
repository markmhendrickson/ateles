"""Build the labelled corpus: (audio_file, start_s, end_s, class) rows."""
import json, glob, os, unicodedata

REC = os.path.expanduser("~/Documents/data/recordings")
EXTS = ['.m4a', '.mp4', '.wav', '.mp3', '.flac']
# Operator speaks English, Spanish, Catalan (locale_profile ent_ea9a413189860f872c6cc99a)

def scripts_of(t):
    s = set()
    for ch in t:
        if not ch.isalpha():
            continue
        try:
            n = unicodedata.name(ch)
        except ValueError:
            continue
        s.add(n.split()[0])
    return s

def audio_for(base):
    for e in EXTS:
        p = os.path.join(REC, base + e)
        if os.path.exists(p):
            return p
    return None

def build():
    rows = []
    for f in sorted(glob.glob(os.path.join(REC, '*_live.jsonl'))):
        base = os.path.basename(f)[:-len('_live.jsonl')]
        aud = audio_for(base)
        if not aud:
            continue
        for line in open(f, encoding='utf-8', errors='replace'):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = (d.get('text') or '').strip()
            if not t:
                continue
            if d.get('start_s') is None or d.get('end_s') is None:
                continue
            sc = scripts_of(t)
            nonlatin = sc - {'LATIN'}
            # Class assignment:
            #   fabrication  -> non-Latin script (operator speaks only Latin-script langs)
            #   genuine      -> Latin script, and NOT filtered/skipped by the existing gate
            if nonlatin:
                cls = 'fabrication'
            elif d.get('filtered') or d.get('skipped') or d.get('silence'):
                cls = 'excluded'
            else:
                cls = 'genuine'
            if cls == 'excluded':
                continue
            rows.append({
                'audio': aud,
                'file': os.path.basename(f),
                'chunk': d.get('chunk'),
                'start_s': float(d['start_s']),
                'end_s': float(d['end_s']),
                'rms_db': d.get('rms_db'),
                'cls': cls,
                'scripts': sorted(nonlatin),
                'nchars': len(t),
            })
    return rows

if __name__ == '__main__':
    rows = build()
    import collections
    c = collections.Counter(r['cls'] for r in rows)
    print('corpus rows:', len(rows), dict(c))
    print('distinct audio files:', len(set(r['audio'] for r in rows)))
