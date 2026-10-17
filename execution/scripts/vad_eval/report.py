import json, statistics as st

def analyze(rows, key, label=None):
    g = [r[key] for r in rows if r['cls'] == 'genuine']
    f = [r[key] for r in rows if r['cls'] == 'fabrication']
    if not g or not f:
        return None
    # AUC = P(genuine scored higher than fabrication), ties count 0.5
    wins = ties = 0
    for a in g:
        for b in f:
            if a > b: wins += 1
            elif a == b: ties += 1
    auc = (wins + 0.5*ties)/(len(g)*len(f))
    # best threshold: maximize (genuine kept) while (fabrication rejected)
    cands = sorted(set(g+f))
    best = None
    for t in cands:
        tpr = sum(1 for a in g if a >= t)/len(g)   # genuine passed
        fpr = sum(1 for b in f if b >= t)/len(f)   # fabrication passed
        j = tpr - fpr
        if best is None or j > best[0]:
            best = (j, t, tpr, fpr)
    lo = max(min(g), min(f)); hi = min(max(g), max(f))
    overlap_g = sum(1 for a in g if lo <= a <= hi)/len(g)
    overlap_f = sum(1 for b in f if lo <= b <= hi)/len(f)
    return dict(key=label or key, n_gen=len(g), n_fab=len(f),
                gen_mean=st.mean(g), fab_mean=st.mean(f),
                gen_med=st.median(g), fab_med=st.median(f),
                auc=auc, best_j=best[0], best_t=best[1],
                tpr=best[2], fpr=best[3],
                overlap_g=overlap_g, overlap_f=overlap_f)

def show(d):
    if not d: 
        print('  (insufficient data)'); return
    print(f"  {d['key']:<22} n_gen={d['n_gen']} n_fab={d['n_fab']}")
    print(f"     genuine  mean={d['gen_mean']:.3f} median={d['gen_med']:.3f}")
    print(f"     fabricat mean={d['fab_mean']:.3f} median={d['fab_med']:.3f}")
    print(f"     AUC (P[genuine>fabrication]) = {d['auc']:.3f}   <-- 0.5=no signal, <0.5=INVERTED")
    print(f"     best threshold {d['best_t']:.3f}: keeps {d['tpr']*100:.1f}% genuine, still passes {d['fpr']*100:.1f}% fabrication (J={d['best_j']:.3f})")
    print(f"     overlap: {d['overlap_g']*100:.1f}% of genuine and {d['overlap_f']*100:.1f}% of fabrication fall in the shared range")

if __name__ == '__main__':
    import sys
    rows = json.load(open(sys.argv[1]))
    keys = [k for k in rows[0] if k.startswith(('webrtcvad','silero','spectral'))]
    print(f'corpus: {len(rows)} scored rows')
    for k in keys:
        show(analyze(rows, k))
        print()
