#!/usr/bin/env python3
"""Q1 acceptance analyzer (frozen protocol: observer-q0-20260810/
Q1-ACCEPTANCE-PROTOCOL.md + its AMENDMENT). Consumes the observer UART dump and
the generator UART dump, runs STRUCTURAL-INTEGRITY gates first (any failure ->
hard REJECT), then reconstructs pairs and applies the metric gates.

Exit 0 = ACCEPT all rungs; 3 = REJECT/STOP; 2 = usage/self-test fail.

Observer:  REC oseq=.. s0=.. len=.. crc=.. rssi=.. pairid=.. txtag=.. st=0x..
           addr=.. end=.. air_us=.. gap_us=..
           FROZEN: records=.. ring_full_drops=.. ...
           LOSS: addr_irq=.. end_irq=.. records=.. ring_full_drops=..
                 addr_minus_end=.. end_minus_recorded=.. reversal=.. stale_addr=..
Generator: GEN pair=.. rung_us=.. lenA=.. lenB=.. gap_gen_ticks=..
           GEN-DONE pairs=.. missed_deadline=..
Usage: analyze_q1.py <observer_dump> <generator_dump>
       analyze_q1.py --selftest <observer_dump> <generator_dump>
"""
import sys, re, statistics as st

TICKS_PER_US = 16
RETENTION_MIN = 0.95
FIDELITY_TOL_TICKS = 2          # observer 1-tick resolution + 1-tick Q0 calib
# preamble+AA on-air duration added to the commanded END->START gap G:
#   1M: (1+4) bytes * 8us * 16 = 640 ticks ; 2M: (2+4) bytes * 4us * 16 = 384
PREAMBLE_AA = {1: 640, 2: 384}


REC_KEYS = ('oseq', 'pairid', 'txtag', 'crc', 'st', 'addr', 'end')

def parse_obs(path):
    recs, frozen, loss, malformed = [], {}, {}, 0
    for ln in open(path, errors='replace'):
        if ln.startswith('REC '):
            d = dict(re.findall(r'(\w+)=(-?\w+)', ln))
            try:  # tolerate truncated/corrupt UART lines -> controlled reject
                recs.append(dict(oseq=int(d['oseq']), pairid=int(d['pairid']),
                                 txtag=int(d['txtag']), crc=int(d['crc']),
                                 st=int(d['st'], 16), addr=int(d['addr']),
                                 end=int(d['end'])))
            except (KeyError, ValueError):
                malformed += 1
        elif ln.startswith('FROZEN:'):
            frozen = {k: int(v) for k, v in re.findall(r'(\w+)=(-?\d+)', ln)}
        elif ln.startswith('LOSS:'):
            loss = {k: int(v) for k, v in re.findall(r'(\w+)=(-?\d+)', ln)}
    return recs, frozen, loss, malformed


def parse_gen(path):
    g, done = {}, {}
    dup = []
    for ln in open(path, errors='replace'):
        if ln.startswith('GEN pair='):
            d = dict(re.findall(r'(\w+)=(-?\d+)', ln))
            pid = int(d['pair'])
            if pid in g:
                dup.append(pid)
            g[pid] = dict(rung=int(d['rung_us']), gap_gen=int(d['gap_gen_ticks']),
                          lenA=int(d['lenA']), lenB=int(d['lenB']))
        elif ln.startswith('GEN-DONE'):
            done = {k: int(v) for k, v in re.findall(r'(\w+)=(-?\d+)', ln)}
    return g, done, dup


def integrity_gates(recs, frozen, loss, gen, done, gdup, phy, malformed):
    """Return list of (name, ok, detail). ALL must pass or the run is REJECTed."""
    G = []
    n = len(recs)
    G.append(('obs: no malformed REC lines', malformed == 0, f'{malformed} malformed'))
    # --- generator side ---
    G.append(('gen: GEN-DONE present', bool(done), done))
    G.append(('gen: missed_deadline==0', done.get('missed_deadline', 1) == 0,
              done.get('missed_deadline')))
    G.append(('gen: unique pair ids', not gdup, f'{len(gdup)} dups {gdup[:5]}'))
    G.append(('gen: GEN-DONE pairs==parsed', done.get('pairs') == len(gen),
              f"done={done.get('pairs')} parsed={len(gen)}"))
    # --- observer structural ---
    G.append(('obs: FROZEN present', bool(frozen), frozen))
    G.append(('obs: LOSS present', bool(loss), loss))
    G.append(('obs: parsed==FROZEN.records==LOSS.records',
              frozen.get('records') == n and loss.get('records') == n,
              f"parsed={n} frozen={frozen.get('records')} loss={loss.get('records')}"))
    G.append(('obs: ring_full_drops==0',
              frozen.get('ring_full_drops', 1) == 0 and loss.get('ring_full_drops', 1) == 0,
              frozen.get('ring_full_drops')))
    for k in ('addr_minus_end', 'end_minus_recorded', 'reversal', 'stale_addr'):
        G.append((f'obs: LOSS.{k}==0', loss.get(k, 1) == 0, loss.get(k)))
    G.append(('obs: addr_irq==end_irq==records',
              loss.get('addr_irq') == loss.get('end_irq') == n, loss))
    # observer sequence numbers must be contiguous 0..n-1 (catches concatenation)
    oseqs = [r['oseq'] for r in recs]
    contig = (sorted(oseqs) == list(range(n)))
    G.append(('obs: oseq contiguous 0..n-1 (no dup/gap)', contig,
              f'min={min(oseqs) if oseqs else "-"} max={max(oseqs) if oseqs else "-"} '
              f'distinct={len(set(oseqs))}/{n}'))
    # DUPLICATION is a hard reject (impossible from one clean run); a MISSING
    # or CRC-corrupted packet is a RETENTION event (tolerated to 95%), not a
    # structural failure. So: no pairid may have >2 records; singletons/CRC
    # losses are handled later as retention.
    # payload fields (pairid,txtag) are only trustworthy on CRC-good/status-good
    # records; build duplication + commanded-pair checks from THOSE only. CRC/
    # status losses count against retention (below), never structural reject.
    good = [r for r in recs if r['crc'] == 1 and r['st'] == 0]
    badcrc = sum(1 for r in recs if r['crc'] != 1)
    badst = sum(1 for r in recs if r['st'] != 0)
    print(f'  [note] {badcrc} CRC-bad + {badst} status-bad records excluded from '
          f'payload/structure checks + clean pairs (retention loss, not reject)')
    perpair = {}
    for r in good:
        perpair.setdefault(r['pairid'], []).append(r['txtag'])
    over = {p: t for p, t in perpair.items() if len(t) > 2}
    G.append(('obs: no CRC-good pairid has >2 records (duplication)',
              not over, f'{len(over)} over, e.g. {list(over.items())[:3]}'))
    # every CRC-good observer pairid must be a commanded generator pairid
    unknown = [p for p in perpair if p not in gen]
    G.append(('obs: all pairids are commanded', not unknown,
              f'{len(unknown)} unknown {unknown[:5]}'))
    # PHY consistency: generator lengths are the known pair
    G.append(('cfg: PHY offset known', phy in PREAMBLE_AA, f'phy={phy}'))
    return G


def analyze(obs_path, gen_path, phy):
    recs, frozen, loss, malformed = parse_obs(obs_path)
    gen, done, gdup = parse_gen(gen_path)
    if not recs or not gen:
        print('REJECT: empty observer or generator dump.'); return 3

    print('=== STRUCTURAL-INTEGRITY GATES (all must pass) ===')
    gates = integrity_gates(recs, frozen, loss, gen, done, gdup, phy, malformed)
    hard_ok = True
    for name, ok, detail in gates:
        print(f'  [{"PASS" if ok else "REJECT"}] {name}   ({detail})')
        if not ok:
            hard_ok = False
    if not hard_ok:
        print('\n=== REJECT: structural integrity failed (see above) ===')
        return 3

    # --- pair reconstruction: CLEAN records only (crc==1 & status==0); a
    # dropped/corrupted packet leaves a singleton -> retention loss ---
    clean = [r for r in recs if r['crc'] == 1 and r['st'] == 0]
    pairs = []
    for a, b in zip(clean, clean[1:]):
        if a['pairid'] != b['pairid'] or a['txtag'] == b['txtag']:
            continue
        first, second = (a, b) if a['txtag'] < b['txtag'] else (b, a)
        span = (second['addr'] - first['end']) & 0xFFFFFFFF
        pairs.append(dict(pairid=a['pairid'], span=span,
                          rung=gen[a['pairid']]['rung'],
                          gap_gen=gen[a['pairid']]['gap_gen']))
    rungs = sorted({v['rung'] for v in gen.values()}, reverse=True)
    cmd_per = {r: sum(1 for v in gen.values() if v['rung'] == r) for r in rungs}

    print('\n=== METRIC GATES ===')
    ok = True
    medians = {}
    off = PREAMBLE_AA[phy]
    for r in rungs:
        rp = [p for p in pairs if p['rung'] == r]
        n_cmd = cmd_per[r]
        # retention: reconstructed pairs; must be <= commanded and >= 95%
        if len(rp) > n_cmd:
            print(f'  rung {r}: REJECT reconstructed {len(rp)} > commanded {n_cmd} '
                  f'(impossible -> duplication)'); ok = False; continue
        retention = len(rp) / n_cmd if n_cmd else 0.0
        spans = sorted(p['span'] for p in rp)
        med = st.median(spans) if spans else None
        med_gap = st.median([p['gap_gen'] for p in rp]) if rp else None
        medians[r] = med
        expect = r * TICKS_PER_US + off
        iqr = (spans[3*len(spans)//4] - spans[len(spans)//4]) if len(spans) >= 4 else 0
        rok = retention >= RETENTION_MIN
        fok = med is not None and abs(med - expect) <= FIDELITY_TOL_TICKS
        print(f'\n  --- rung G={r}us (commanded {n_cmd}) ---')
        print(f'    retention {len(rp)}/{n_cmd}={retention*100:.1f}% (>=95%) '
              f'{"PASS" if rok else "FAIL"}')
        print(f'    fidelity vs COMMANDED: span median={med} vs G*16+{off}={expect} '
              f'|d|={abs(med-expect) if med is not None else "-"} (<= {FIDELITY_TOL_TICKS}) '
              f'{"PASS" if fok else "FAIL"}')
        print(f'    spread: IQR={iqr} ticks (no observed spread at 62.5ns if 0), n={len(spans)}')
        print(f'    [info] span-gap_gen = {med-med_gap if med is not None else "-"} ticks '
              f'(constant across rungs = fixed TX-emit vs RX-detect event-latency, '
              f'NOT an independent calibration)')
        if not (rok and fok):
            ok = False

    print('\n  --- separation (bias-immune deltas) ---')
    ms = [(r, medians[r]) for r in rungs if medians.get(r) is not None]
    for (r1, m1), (r2, m2) in zip(ms, ms[1:]):
        d = abs(m1 - m2); exp = (r1 - r2) * TICKS_PER_US
        sok = abs(d - exp) <= FIDELITY_TOL_TICKS
        print(f'    G={r1}({m1}) vs G={r2}({m2}): delta={d} vs commanded {exp} '
              f'{"PASS" if sok else "FAIL"}')
        if not sok: ok = False

    print('\n' + ('=== ACCEPT: all Q1 gates pass for this run ==='
                  if ok else '=== REJECT/STOP: a metric gate failed ==='))
    return 0 if ok else 3


def _tmp(text):
    import tempfile
    p = tempfile.mktemp(suffix='.txt'); open(p, 'w').write(text); return p


def selftest(obs_path, gen_path, obs2m=None, gen2m=None):
    import os, re as _re
    data = open(obs_path).read()
    results = []

    def check(name, path, gen, phy, expect):
        rc = analyze(path, gen, phy)
        ok = (rc == expect)
        results.append((name, ok, f'rc={rc} expect={expect}'))
        return ok

    print('SELFTEST 1: clean 1M run must ACCEPT (rc 0)')
    check('clean-1M-accept', obs_path, gen_path, 1, 0)

    print('\nSELFTEST 2: DOUBLED capture must REJECT (duplication, rc 3)')
    p = _tmp(data + data); check('doubled-reject', p, gen_path, 1, 3); os.unlink(p)

    print('\nSELFTEST 3: MALFORMED REC line must REJECT cleanly (rc 3, no crash)')
    p = _tmp(data + 'REC oseq=99999 s0=0x02 len=20 crc=1 pairid=\n')
    check('malformed-reject', p, gen_path, 1, 3); os.unlink(p)

    print('\nSELFTEST 4: CRC-bad record w/ CORRUPTED pairid -> retention loss, '
          'NOT structural reject (rc 0)')
    lines = data.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith('REC '):   # corrupt the FIRST record: crc->0, pairid->out-of-range
            ln = _re.sub(r'crc=\d', 'crc=0', ln)
            ln = _re.sub(r'pairid=\d+', 'pairid=99999', ln)
            lines[i] = ln; break
    p = _tmp('\n'.join(lines) + '\n')
    check('crcbad-corrupt-pairid-accept', p, gen_path, 1, 0); os.unlink(p)

    if obs2m and gen2m:
        print('\nSELFTEST 5: clean 2M run must ACCEPT (rc 0)')
        check('clean-2M-accept', obs2m, gen2m, 2, 0)

    print('\n=== SELFTEST RESULTS ===')
    allok = True
    for name, ok, detail in results:
        print(f'  [{"PASS" if ok else "FAIL"}] {name}  ({detail})'); allok &= ok
    print(f'SELFTEST: {"PASS" if allok else "FAIL"}')
    return 0 if allok else 1


if __name__ == '__main__':
    a = sys.argv[1:]
    if a and a[0] == '--selftest':
        # --selftest <obs1m> <gen1m> [<obs2m> <gen2m>]
        sys.exit(selftest(*a[1:5]) if len(a) >= 5 else selftest(a[1], a[2]))
    phy = 1
    if '--phy' in a:
        i = a.index('--phy'); phy = int(a[i+1]); del a[i:i+2]
    if len(a) != 2:
        print(__doc__); sys.exit(2)
    sys.exit(analyze(a[0], a[1], phy))
