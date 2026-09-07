#!/usr/bin/env python3
"""Q2 acceptance analyzer (frozen: debug-evidence/observer-q1-20260810/
Q2-ACCEPTANCE-PROTOCOL.md rev-12). Real BLE connection, two transmitters, near/far.

The observer span is a `gap_proxy` (= tIFS*16 + preamble/AA(640) - 1 tick), NOT
the physical tIFS. Symmetric controls ESTABLISH a frozen `gap_proxy_dev` only if
the whole cell ACCEPTs (else CANDIDATE); near/far runs gate against it. All
retentions are LOWER BOUNDS (the tx-delta denominator over-counts by the snapshot
slop, which is itself gated to <=3% of the capture duration).

DENOMINATOR (guaranteed conservative by COMMAND ORDERING, not arrival heuristics):
the runner runs an ARMED->GO handshake. The observer boots, prints ARMED, and
WAITS for GO. The runner: (a) commands both endpoints to emit an atomic START
`Q2SNAP` (seq=0), (b) sends the observer GO -> CAPTURE-START..CAPTURE-END, (c)
after CAPTURE-END commands both endpoints to emit an atomic END `Q2SNAP` (seq=1).
Because the START snaps precede GO and the END snaps follow CAPTURE-END (verified
on the runner's SINGLE host clock: START_snap_host<=Ts and END_snap_host>=Te),
the endpoint counter interval [START,END] CONTAINS the observer interval -> the
denominator is >= the true count. Denominators (NOT sched): central ACTUAL-TX
completion; peripheral CRC-good RESPONSE-OPPORTUNITY (conservative, >= actual
transmitted responses). sched is a health/packets-per-event diagnostic only.

Args: analyze_q2.py <obs> <central> <periph> --mode symmetric|nearfar
      [--near central|periph] [--calib gap-proxy-calibration.json]
      (near/far REQUIRES --calib: a provenance-bound ESTABLISHED artifact from >=2
       accepted controls. There is NO naked --tifs-dev CLI flag -- the raw integer
       is reachable only in-process by the self-test.)
      analyze_q2.py --roleswap --calib ART <obsA> <cA> <pA> <nearA> <obsB> <cB> <pB> <nearB>
      analyze_q2.py --selftest
Exit 0 ACCEPT, 3 REJECT, 2 usage/selftest-fail.
"""
import sys, os, re, hashlib, statistics as st

TICKS_PER_US = 16
TIFS_EXPECT = 150 * TICKS_PER_US + 640 - 1
# pair-gap acceptance window (gap = tIFS*16 + 639). Must SPAN the FSU tIFS regime
# so reduced-tIFS pairs are found: 100us->2239, 150us->3047 both inside. (Pre-Q3
# this was 2900..3200, which would REJECT every 100us pair -> a false null.)
PAIR_GAP_LO, PAIR_GAP_HI = 1200, 3300
TIFS_TOL = 2
RETENTION_MIN = 0.95
SYM_SEP_MAX = 6
NF_SEP_MIN = 20
NF_FAR_BAND = (-85, -70)
EXPECT_CHANS = {10, 11}
EXPECT_MAP = "000c000000"
EXPECT_PHY = 1                 # 1M by default; the 2M port (analyze_q3_2m._apply_2m_overrides) rebinds -> 2
CAP_TOL = 1500                 # host-measured dur must match the observer's DECLARED cap +/- this
                               # (arrival jitter only; the observer's k_msleep is ms-accurate)
MAX_SLOP_MS = 5000             # gross-error sanity ceiling per boundary
SLOP_FRAC = 0.03               # combined per-endpoint slop must be <= 3% of the
                               # capture duration (else the >=95% lower bound is
                               # not tight -- the denominator over-counts)
PREAMBLE_AA_OFF = 640 - 1      # gap_proxy = tIFS*16 + preamble/AA(640) - 1 calib

# --- calibration-artifact contract (SHARED by combine_calib.py producer and the
# validate_calib() consumer, so both agree on every field/rule) ---
CALIB_MIN_CONTROLS = 2
CALIB_SPREAD_MAX_TICKS = 2
CALIB_ROUNDING = 'round-half-to-even'
CALIB_PROXY = 'prev-END -> next-ADDRESS'
def calib_round(x):
    return int(round(x))   # Python round() is banker's rounding (half-to-even)

HERE = os.path.dirname(os.path.abspath(__file__))
PROTO_PATH = os.path.join(HERE, '..', 'debug-evidence', 'observer-q1-20260810',
                          'Q2-ACCEPTANCE-PROTOCOL.md')

def file_sha256(path):
    try:
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for b in iter(lambda: f.read(65536), b''):
                h.update(b)
        return h.hexdigest()
    except OSError:
        return 'MISSING'

def valid_digest(h):
    """a real sha256 -- never 'MISSING'/'UNVERIFIED'/empty."""
    return bool(re.fullmatch(r'[0-9a-f]{64}', h or ''))

def verify_input_evidence(base_dir, inp):
    """RESOLVE a calibration input's cell dir (relative to the artifact) and REHASH
    the referenced evidence -- logs + manifest -- against the digests recorded in
    the artifact, then re-check the manifest's accepted verdict + image/build
    schema. Fabricated digests over nonexistent dirs are rejected here. Returns a
    reason string on failure, or None on success."""
    import json
    d = inp.get('dir')
    if not d:
        return 'input missing dir (cannot resolve evidence)'
    cell = d if os.path.isabs(d) else os.path.normpath(os.path.join(base_dir, d))
    if not os.path.isdir(cell):
        return f'input dir does not exist: {cell}'
    s = inp.get('sha256', {})
    for key, fname in (('obs', 'obs.txt'), ('central', 'central.txt'), ('periph', 'periph.txt')):
        if file_sha256(os.path.join(cell, fname)) != s.get(key):
            return f'{fname} rehash != artifact digest (evidence missing/changed)'
    mpath = os.path.join(cell, 'manifest.json')
    if file_sha256(mpath) != inp.get('manifest_sha256'):
        return 'manifest.json rehash != artifact digest'
    try:
        m = json.load(open(mpath))
    except (OSError, ValueError):
        return 'referenced manifest.json unreadable'
    if m.get('verdict') != 'ACCEPT':
        return f'referenced manifest verdict={m.get("verdict")} (not ACCEPT)'
    if not m.get('observer_image_verified'):
        return 'referenced manifest observer_image_verified=false'
    for role in ('central_build', 'periph_build'):
        bd = m.get(role, {})
        for k in ('zephyr.hex', '.config'):
            if not valid_digest(bd.get(k)):
                return f'referenced manifest {role}.{k} not a valid 64-hex digest'
    if not valid_digest(m.get('sha256', {}).get('observer.hex')):
        return 'referenced manifest observer.hex not a valid 64-hex digest'
    return None

def validate_calib(path):
    """RECOMPUTE and verify a calibration artifact's whole contract before trusting
    its frozen value (not just status). Returns (frozen_int, None) or (None, reason).
    Rejects a forged/naked {status,frozen} blob: it must be a genuine, self
    -consistent, current-lineage combine_calib.py product."""
    import json
    try:
        cj = json.load(open(path))
    except (OSError, ValueError) as e:
        return None, f'unreadable calibration: {e}'
    if cj.get('status') != 'ESTABLISHED':
        return None, f'status={cj.get("status")} (not ESTABLISHED)'
    con = cj.get('contract', {})
    if (con.get('min_controls') != CALIB_MIN_CONTROLS or
            con.get('spread_max_ticks') != CALIB_SPREAD_MAX_TICKS or
            con.get('rounding') != CALIB_ROUNDING or con.get('proxy') != CALIB_PROXY):
        return None, f'contract fields do not match rev-12 ({con})'
    inputs = cj.get('inputs', [])
    if len(inputs) < CALIB_MIN_CONTROLS:
        return None, f'only {len(inputs)} inputs (need >= {CALIB_MIN_CONTROLS})'
    for i in inputs:
        if i.get('verdict') != 'ACCEPT':
            return None, 'an input verdict != ACCEPT'
        if 'median' not in i:
            return None, 'an input is missing its median'
        s = i.get('sha256', {})
        if not all(valid_digest(s.get(k)) for k in ('obs', 'central', 'periph')):
            return None, 'an input lacks the three valid 64-hex log digests (obs/central/periph)'
        if not valid_digest(i.get('manifest_sha256')):
            return None, 'an input lacks a valid 64-hex manifest digest'
    medians = cj.get('medians', [])
    if medians != [i['median'] for i in inputs]:
        return None, 'medians[] does not match the per-input medians'
    frozen = cj.get('frozen_gap_proxy_dev')
    if frozen is None:
        return None, 'no frozen_gap_proxy_dev'
    if calib_round(st.median(medians)) != frozen:
        return None, 'frozen value is not reproduced by round-half-even(median of medians)'
    if any(abs(m - frozen) > CALIB_SPREAD_MAX_TICKS for m in medians):
        return None, f'a median is > {CALIB_SPREAD_MAX_TICKS} ticks from the frozen value'
    lin = cj.get('lineage', {})
    want = {'analyze_q2': file_sha256(os.path.join(HERE, 'analyze_q2.py')),
            'combine_calib': file_sha256(os.path.join(HERE, 'combine_calib.py')),
            'protocol': file_sha256(PROTO_PATH)}
    for k, v in want.items():
        if lin.get(k) != v:
            return None, f'lineage {k} hash != current tool (calibration made by different tools)'
    # RESOLVE, REHASH, and RE-ANALYZE each referenced input (paths relative to the
    # artifact). Digest rehash alone is not enough: the artifact's reported medians
    # must be REPRODUCED from the logs (an internally-consistent median/frozen edit
    # over untouched evidence would otherwise pass). The manifest's internal log
    # hashes and cross-control homogeneity are re-checked through the SAME code the
    # combiner used (combine_calib), so consumer and producer never diverge.
    base_dir = os.path.dirname(os.path.abspath(path))
    import combine_calib as CC
    mans = []
    for inp in inputs:
        reason = verify_input_evidence(base_dir, inp)
        if reason:
            return None, f'input evidence unverifiable: {reason}'
        cell = inp['dir'] if os.path.isabs(inp['dir']) else os.path.normpath(os.path.join(base_dir, inp['dir']))
        mok, mreason, m = CC.check_manifest(cell)   # manifest-internal log hashes + verdict + image
        if not mok:
            return None, f'input manifest invalid: {mreason}'
        r = {}
        rc = analyze(*(os.path.join(cell, n) for n in ('obs.txt', 'central.txt', 'periph.txt')),
                     'symmetric', 'central', None, quiet=True, result=r)
        if rc != 0 or r.get('med') is None:
            return None, f'a referenced control no longer ACCEPTs on re-analysis ({cell})'
        if r['med'] != inp.get('median'):
            return None, (f're-analyzed median {r["med"]} != artifact median '
                          f'{inp.get("median")} (reported medians not reproduced)')
        mans.append(m)
    hok, hreason = CC.check_homogeneity(mans)   # reapply matched-replication gate
    if not hok:
        return None, f'referenced controls are not matched replications: {hreason}'
    return int(frozen), None

def _strip(ln):
    m = re.match(r'HOSTMS (\d+) (.*)', ln)
    return (int(m.group(1)), m.group(2)) if m else (None, ln)

def parse_obs(path):
    recs, frozen, loss, malformed, cfg = [], {}, {}, 0, {}
    n_start = n_end = n_frozen = 0; Ts = Te = None; cs_tick = ce_tick = None
    for raw in open(path, errors='replace'):
        hm, ln = _strip(raw)
        if ln.startswith('REC '):
            d = dict(re.findall(r'(\w+)=(-?\w+)', ln))
            try:
                recs.append(dict(oseq=int(d['oseq']), crc=int(d['crc']), st=int(d['st'], 16),
                                 rssi=int(d['rssi']), addr=int(d['addr']), end=int(d['end'])))
            except (KeyError, ValueError):
                malformed += 1
        elif ln.startswith('mode=Q2'):
            m = re.search(r'phy=(\d)M AA=0x([0-9a-fA-F]+) crcinit=0x([0-9a-fA-F]+) ch=(\d+)', ln)
            if m:
                cfg = dict(phy=int(m.group(1)), aa=int(m.group(2), 16),
                           crcinit=int(m.group(3), 16), ch=int(m.group(4)))
                mc = re.search(r'cap=(\d+)ms', ln)   # the observer declares its own window
                if mc: cfg['cap_ms'] = int(mc.group(1))
        elif ln.startswith('CAPTURE-START'):
            n_start += 1; Ts = hm
            mt = re.search(r'tick=(\d+)', ln); cs_tick = int(mt.group(1)) if mt else None
        elif ln.startswith('CAPTURE-END'):
            n_end += 1; Te = hm
            mt = re.search(r'tick=(\d+)', ln); ce_tick = int(mt.group(1)) if mt else None
        elif ln.startswith('FROZEN:'):
            n_frozen += 1
            frozen = {k: int(v) for k, v in re.findall(r'(\w+)=(-?\d+)', ln)}
        elif ln.startswith('LOSS:'):
            loss = {k: int(v) for k, v in re.findall(r'(\w+)=(-?\d+)', ln)}
    return dict(recs=recs, frozen=frozen, loss=loss, malformed=malformed, cfg=cfg,
                n_start=n_start, n_end=n_end, n_frozen=n_frozen, Ts=Ts, Te=Te,
                cs_tick=cs_tick, ce_tick=ce_tick)

def parse_conn(path):
    hits = []
    for raw in open(path, errors='replace'):
        _, ln = _strip(raw)
        # search ANYWHERE, not startswith: the controller's Q2CONN printk can
        # interleave with the app's Q2EVT line on the shared console (two print
        # sources, one UART), producing e.g. "Q2EVT role=CQ2CONN AA=...". The AA is
        # cross-checked against both Q2SNAP aa's + the map/chan_count gates, so a
        # mangled Q2CONN can only REJECT, never false-ACCEPT.
        if 'Q2CONN' in ln:
            m = re.search(r'Q2CONN AA=([0-9a-fA-F]+) CRCINIT=([0-9a-fA-F]+).*chan_count=(\d+) map=([0-9a-fA-F]+)', ln)
            if m:
                hits.append(dict(aa=int(m.group(1), 16), crcinit=int(m.group(2), 16),
                                 chan_count=int(m.group(3)), map=m.group(4).lower()))
    if len(hits) != 1:
        return None, f'expected exactly 1 Q2CONN, got {len(hits)}'
    return hits[0], ''

def parse_ready(path, role):
    """Q2READY role=X dev=.. boottag=.. ; require the expected role present."""
    for raw in open(path, errors='replace'):
        _, ln = _strip(raw)
        m = re.search(rf'Q2READY role={role} dev=([0-9a-fA-F]+) boottag=0x([0-9a-fA-F]+)', ln)
        if m:
            return dict(dev=m.group(1).lower(), boottag=int(m.group(2), 16)), ''
    return None, f'no Q2READY role={role}'

def parse_snaps(path, Ts, Te, role, obs_ch, boottag):
    """atomic START(seq0)+END(seq1) snapshots, BOUND to role/channel/boottag."""
    snaps = {}
    for raw in open(path, errors='replace'):
        hm, ln = _strip(raw)
        m = re.search(r'Q2SNAP role=(\w) seq=(\d+) boottag=0x([0-9a-fA-F]+) aa=0x([0-9a-fA-F]+) '
                      r'sess=(\d+) ch=(\d+) sched=(\d+) tx=(\d+)', ln)
        if m:
            snaps[int(m.group(2))] = dict(host=hm, role=m.group(1), boottag=int(m.group(3), 16),
                                          aa=int(m.group(4), 16), sess=int(m.group(5)),
                                          ch=int(m.group(6)), sched=int(m.group(7)), tx=int(m.group(8)))
    if set(snaps) != {0, 1}:
        return None, f'need exactly START(seq0)+END(seq1) snaps, got seqs {sorted(snaps)}'
    s, e = snaps[0], snaps[1]
    for tag, sn in (('START', s), ('END', e)):
        if sn['role'] != role:
            return None, f'{tag} snap role={sn["role"]} != expected {role} (crossed logs?)'
        if sn['ch'] != obs_ch:
            return None, f'{tag} snap ch={sn["ch"]} != observer ch {obs_ch}'
        if sn['boottag'] != boottag:
            return None, f'{tag} snap boottag != Q2READY boottag -> reboot/mixup'
    if s['aa'] != e['aa'] or s['sess'] != e['sess']:
        return None, 'AA/session changed between snapshots -> reconnect'
    if e['sched'] < s['sched'] or e['tx'] < s['tx']:
        return None, 'counter went backwards between snapshots -> reset'
    if Ts is None or Te is None or s['host'] is None or e['host'] is None:
        return None, 'missing host times for ordering check'
    if not (s['host'] <= Ts):
        return None, f'START snap ({s["host"]}) not before CAPTURE-START ({Ts}) -> ordering violated'
    if not (e['host'] >= Te):
        return None, f'END snap ({e["host"]}) not after CAPTURE-END ({Te}) -> ordering violated'
    slop = (Ts - s['host'], e['host'] - Te)
    if slop[0] > MAX_SLOP_MS or slop[1] > MAX_SLOP_MS:
        return None, f'boundary slop {slop} ms exceeds {MAX_SLOP_MS} -> loose bracket'
    return dict(aa=s['aa'], sess=s['sess'], sched=e['sched']-s['sched'], tx=e['tx']-s['tx'], slop=slop), ''

def structural_gates(o):
    recs = o['recs']; n = len(recs); G = []
    G.append(('exactly one CAPTURE-START', o['n_start'] == 1, o['n_start']))
    G.append(('exactly one CAPTURE-END', o['n_end'] == 1, o['n_end']))
    G.append(('exactly one FROZEN', o['n_frozen'] == 1, o['n_frozen']))
    dur = (o['Te'] - o['Ts']) if (o['Ts'] is not None and o['Te'] is not None) else -1
    cap = o['cfg'].get('cap_ms')
    G.append(('capture duration ~ declared cap',
              cap is not None and abs(dur - cap) <= CAP_TOL, f'{dur}ms vs cap={cap}ms'))
    G.append(('no malformed REC', o['malformed'] == 0, o['malformed']))
    G.append(('FROZEN & LOSS present', bool(o['frozen']) and bool(o['loss']), ''))
    G.append(('parsed==FROZEN==LOSS records',
              o['frozen'].get('records') == n and o['loss'].get('records') == n,
              f"parsed={n} frozen={o['frozen'].get('records')} loss={o['loss'].get('records')}"))
    G.append(('ring_full_drops==0', o['frozen'].get('ring_full_drops', 1) == 0, ''))
    for k in ('addr_minus_end', 'end_minus_recorded', 'reversal', 'stale_addr'):
        G.append((f'LOSS.{k}==0', o['loss'].get(k, 1) == 0, o['loss'].get(k)))
    G.append(('oseq contiguous', sorted(r['oseq'] for r in recs) == list(range(n)), ''))
    return G

def find_pairs(good):
    pairs = []
    for a, b in zip(good, good[1:]):
        gap = (b['addr'] - a['end']) & 0xFFFFFFFF
        if PAIR_GAP_LO <= gap <= PAIR_GAP_HI:
            pairs.append(dict(span=gap, a=a, b=b))
    return pairs

# --- Q3 timing anchors: bridge the observer's TIMER0 acquisition domain (REC
# addr/end ticks) to the host clock (HOSTMS) of the FSU completions, so the
# transition can be excluded NON-circularly (not by first detecting the step). ---
TIMER0_WRAP = 1 << 32          # observer TIMER0 is 32-bit @16MHz (62.5ns/tick; wraps ~268s)
Q3_HOST_JITTER_MS = 50         # conservative reader arrival-jitter bound per anchor (HOSTMS is arrival, not generation)

def q3_reltick(abs_tick, cs_tick):
    """A REC/anchor absolute TIMER0 tick -> ticks SINCE CAPTURE-START (wrap-safe;
    the <=268s window never wraps twice)."""
    return (abs_tick - cs_tick) & (TIMER0_WRAP - 1)

def q3_host_to_reltick(anchors, host_ms):
    """Map a host time (ms) -> ticks-since-CAPTURE-START via two (host_ms, abs_tick)
    anchors [START, END]. Relative frame avoids the 32-bit wrap inside the window.
    Returns (reltick, unc_ticks); unc propagates Q3_HOST_JITTER_MS through the rate.
    Raises ValueError on degenerate anchors."""
    (h0, t0), (h1, t1) = anchors
    dh = h1 - h0
    if dh <= 0:
        raise ValueError('non-increasing host anchors')
    dt = (t1 - t0) & (TIMER0_WRAP - 1)          # window duration in ticks (wrap-safe)
    rate = dt / dh                               # ticks per ms
    reltick = (host_ms - h0) * rate              # ticks since START (may be <0 / >dt if outside)
    return reltick, Q3_HOST_JITTER_MS * rate

def q3_transition_window(anchors, req_host, done_hosts, guard_ticks, extra_start_hosts=()):
    """CONSERVATIVE excluded interval [lo, hi] in ticks-since-CAPTURE-START around the
    FSU transition -- from the EARLIEST start event (the request AND any F-time phase
    snapshots, each minus its mapping uncertainty) through the LATER completion (plus
    uncertainty), plus a frozen guard on each side. Defined from timestamps ONLY (never
    from a detected gap change). Returns (lo, hi, unc)."""
    starts = [q3_host_to_reltick(anchors, h) for h in (req_host, *extra_start_hosts)]
    lo = min(r - u for r, u in starts) - guard_ticks
    dts = [q3_host_to_reltick(anchors, h) for h in done_hosts]
    later = max(d[0] for d in dts); ud = max(d[1] for d in dts)
    hi = later + ud + guard_ticks
    return lo, hi, max(max(u for _, u in starts), ud)

def two_means_thr(good):
    rs = sorted(r['rssi'] for r in good)
    c0, c1 = rs[0], rs[-1]
    for _ in range(30):
        g0 = [x for x in rs if abs(x-c0) <= abs(x-c1)]
        g1 = [x for x in rs if abs(x-c0) > abs(x-c1)]
        if not g0 or not g1: break
        c0, c1 = sum(g0)/len(g0), sum(g1)/len(g1)
    far, near = sorted((c0, c1))
    return (near+far)/2.0, near, far

def validate_common(o, clog, plog, pr):
    """SHARED Q2 evidence gates -- structural (FROZEN/LOSS/anchors/oseq), Q2CONN +
    both Q2READY DEVICEIDs, bracketing snapshots (denominators + boundary slop), and
    the full config binding (AA/CRCInit/map/chan_count/PHY/channel). Q2 AND Q3 both
    run this BEFORE their own metric. Returns (ctx, '') on PASS or (None, reason) on
    the first failing leg. ctx keys: conn, cr, prd, cw, pw, cfg, cap_dur, cN, pN."""
    if not o['recs']:
        return None, 'empty observer dump'
    pr('=== STRUCTURAL GATES ===')
    ok = True
    for name, g, d in structural_gates(o):
        pr(f'  [{"PASS" if g else "REJECT"}] {name}  ({d})'); ok &= g
    if not ok:
        return None, 'structural'
    conn, connerr = parse_conn(clog)
    cfg = o['cfg']
    obs_ch = cfg.get('ch') if cfg else None
    cr, crerr = parse_ready(clog, 'C')
    prd, prerr = parse_ready(plog, 'P')
    if connerr or crerr or prerr or obs_ch is None:
        return None, (f'Q2CONN[{connerr}] central-READY[{crerr}] periph-READY[{prerr}] '
                      f'obs_ch[{"missing" if obs_ch is None else obs_ch}]')
    cw, cerr = parse_snaps(clog, o['Ts'], o['Te'], 'C', obs_ch, cr['boottag'])
    pw, perr = parse_snaps(plog, o['Ts'], o['Te'], 'P', obs_ch, prd['boottag'])
    if cerr or perr:
        return None, f'central-snap[{cerr}] periph-snap[{perr}]'
    cap_dur = o['Te'] - o['Ts']
    cslop = cw['slop'][0] + cw['slop'][1]; pslop = pw['slop'][0] + pw['slop'][1]
    slop_ceil = SLOP_FRAC * cap_dur
    pr(f'snapshot slop central={cw["slop"]}ms (sum {cslop}) periph={pw["slop"]}ms (sum {pslop}) '
       f'; combined ceil {SLOP_FRAC*100:.0f}% of {cap_dur}ms = {slop_ceil:.0f}ms')
    pr('=== CONFIG BINDING ===')
    B = [('combined slop <= 3% of capture (tight lower bound)',
          cslop <= slop_ceil and pslop <= slop_ceil, f'C={cslop} P={pslop} ceil={slop_ceil:.0f}'),
         ('chan_count==2', conn['chan_count'] == 2, conn['chan_count']),
         ('map=={10,11}', conn['map'] == EXPECT_MAP, conn['map']),
         ('observer cfg present', bool(cfg), cfg)]
    if cfg:
        B += [('observer AA==connection', cfg['aa'] == conn['aa'], f"0x{cfg['aa']:08x}/0x{conn['aa']:08x}"),
              ('observer CRCInit==connection', cfg['crcinit'] == conn['crcinit'], ''),
              ('observer channel in {10,11}', cfg['ch'] in EXPECT_CHANS, cfg['ch']),
              (f'observer PHY=={EXPECT_PHY}M', cfg['phy'] == EXPECT_PHY, cfg['phy']),
              ('endpoint AA==observer AA', cw['aa'] == cfg['aa'], '')]
    B += [('central AA==periph AA', cw['aa'] == pw['aa'], ''),
          ('distinct DEVICEIDs (C!=P)', cr['dev'] != prd['dev'], f"{cr['dev']}/{prd['dev']}"),
          ('central actual-TX delta > 0', cw['tx'] > 0, f"txC={cw['tx']}"),
          ('periph resp-opportunity delta > 0', pw['tx'] > 0, f"txP={pw['tx']}")]
    for name, g, d in B:
        pr(f'  [{"PASS" if g else "REJECT"}] {name}  ({d})'); ok &= g
    if not ok:
        return None, 'config binding'
    return dict(conn=conn, cr=cr, prd=prd, cw=cw, pw=pw, cfg=cfg,
                cap_dur=cap_dur, cN=cw['tx'], pN=pw['tx']), ''


def analyze(obs, clog, plog, mode, near, tifs_dev, quiet=False, result=None):
    pr = (lambda *a: None) if quiet else print
    if mode == 'nearfar' and not tifs_dev:
        pr('REJECT: near/far requires a FROZEN gap_proxy_dev from accepted symmetric '
           'controls (--calib gap-proxy-calibration.json, or --tifs-dev for selftest).'); return 3
    o = parse_obs(obs)
    ctx, reason = validate_common(o, clog, plog, pr)
    if ctx is None:
        pr(f'=== REJECT: {reason} ==='); return 3
    conn, cr, prd, cw, pw, cfg, cap_dur, cN, pN = (
        ctx['conn'], ctx['cr'], ctx['prd'], ctx['cw'], ctx['pw'],
        ctx['cfg'], ctx['cap_dur'], ctx['cN'], ctx['pN'])

    # DENOMINATORS (per review): NOT sched. Central = actual TX-completion delta;
    # peripheral = CRC-good RESPONSE-OPPORTUNITY delta (conservative -- may exceed
    # actual transmitted responses, e.g. multi-PDU events). `sched` is kept ONLY
    # as a health / packets-per-event diagnostic, never an equality bound (sched
    # and tx are NOT one-to-one on real runs). (cN/pN come from validate_common.)
    pr(f'  [diag] packets-per-event: central tx/sched={cw["tx"]}/{cw["sched"]} '
       f'periph tx/sched={pw["tx"]}/{pw["sched"]} (diagnostic only, not a gate)')
    good = [r for r in o['recs'] if r['crc'] == 1 and r['st'] == 0]
    pairs = find_pairs(good)
    spans = sorted(p['span'] for p in pairs)
    med = st.median(spans) if spans else None
    iqr = (spans[3*len(spans)//4] - spans[len(spans)//4]) if len(spans) >= 4 else 0
    pr(f'\n=== METRICS (mode={mode}) === clean={len(good)} pairs={len(pairs)}')
    passed = True
    if med is None:
        pr('  tIFS: no pairs -> FAIL'); passed = False
    elif mode == 'symmetric':
        # `med` is the gap_proxy (next.ADDRESS - prev.END = tIFS + preamble/AA).
        # It is NOT the physical tIFS. Calibrated tIFS estimate below.
        tifs_us = (med - PREAMBLE_AA_OFF) / TICKS_PER_US
        pr(f'  gap_proxy_dev = {med} ticks  IQR={iqr}  -> calibrated tIFS estimate '
           f'~= {tifs_us:.2f}us (NOT {med/TICKS_PER_US:.1f}us; that includes the '
           f'~40us preamble/AA offset)')
        pr('  (a symmetric CANDIDATE reference -- becomes an ESTABLISHED gap_proxy_dev '
           'only if THIS cell ACCEPTs; see final verdict)')
    else:
        fok = abs(med-tifs_dev) <= TIFS_TOL
        tifs_us = (med - PREAMBLE_AA_OFF) / TICKS_PER_US
        pr(f'  gap_proxy median={med} (tIFS~={tifs_us:.2f}us) vs frozen gap_proxy_dev='
           f'{tifs_dev} |d|={abs(med-tifs_dev)} (<= {TIFS_TOL}) '
           f'{"PASS" if fok else "FAIL"} IQR={iqr}'); passed &= fok

    def cap(num, den, lbl):
        if num > den:
            pr(f'  REJECT: {lbl} numerator {num} > denom {den} (overcount)'); return False
        return True

    if mode == 'symmetric':
        c_r = [p['a']['rssi'] for p in pairs]; p_r = [p['b']['rssi'] for p in pairs]
        if not cap(len(pairs), pN, 'paired'):
            return 3
        prr = len(pairs)/pN if pN else 0
        sep = abs(st.median(c_r) - st.median(p_r)) if (c_r and p_r) else 999
        rok = sep <= SYM_SEP_MAX
        pr(f'  paired-retention LOWER BOUND >= {len(pairs)}/{pN} = {prr*100:.1f}% '
           f'{"PASS" if prr>=RETENTION_MIN else "FAIL"} (denominator over-counts by the '
           f'slop, so this is a floor -- true retention is higher)')
        pr(f'  symmetric order-derived RSSI sep={sep:.1f}dB (<= {SYM_SEP_MAX}) {"PASS" if rok else "FAIL"}')
        passed &= (prr>=RETENTION_MIN) and rok
        # ONE accepted run is only an ACCEPTED CANDIDATE. The frozen ESTABLISHED
        # gap_proxy_dev is produced by combine_calib.py from >=2 accepted controls
        # (median of run medians; every control within +/-2 ticks). A single run
        # NEVER establishes the reference.
        pr(f'  -> gap_proxy {"ACCEPTED CANDIDATE" if passed else "NOT a candidate (cell rejected)"} = {med} ticks '
           f'(one run = CANDIDATE only; frozen ESTABLISHED reference needs >=2 '
           f'accepted controls via combine_calib.py)')
    else:
        if len(good) < 4:
            pr('  REJECT: too few clean records'); return 3
        thr, near_c, far_c = two_means_thr(good)
        rof = lambda r: (near if r['rssi'] >= thr else ('periph' if near=='central' else 'central'))
        obs_c = [r for r in good if rof(r) == 'central']; obs_p = [r for r in good if rof(r) == 'periph']
        if not (cap(len(obs_c), cN, 'central') and cap(len(obs_p), pN, 'periph') and cap(len(pairs), pN, 'paired')):
            return 3
        disagree = sum(1 for p in pairs if not (rof(p['a'])=='central' and rof(p['b'])=='periph'))
        c_ret, p_ret, prr = len(obs_c)/cN, len(obs_p)/pN, len(pairs)/pN
        sep = near_c - far_c
        rok = sep >= NF_SEP_MIN and NF_FAR_BAND[0] <= far_c <= NF_FAR_BAND[1]
        pr(f'  central retention LOWER BOUND >= {len(obs_c)}/{cN} = {c_ret*100:.1f}% {"PASS" if c_ret>=RETENTION_MIN else "FAIL"}')
        pr(f'  periph  retention LOWER BOUND >= {len(obs_p)}/{pN} = {p_ret*100:.1f}% {"PASS" if p_ret>=RETENTION_MIN else "FAIL"}')
        pr(f'  paired  retention LOWER BOUND >= {len(pairs)}/{pN} = {prr*100:.1f}% {"PASS" if prr>=RETENTION_MIN else "FAIL"}')
        pr(f'  attribution ordering-vs-RSSI disagreements={disagree} {"PASS" if disagree==0 else "FAIL"}')
        pr(f'  near/far sep={sep:.1f}dB (>= {NF_SEP_MIN}) far={far_c:.0f}dBm in[{NF_FAR_BAND}] {"PASS" if rok else "FAIL"}')
        passed &= (c_ret>=RETENTION_MIN and p_ret>=RETENTION_MIN and prr>=RETENTION_MIN and disagree==0 and rok)
    # machine-readable summary (combine_calib.py / archiving consume this)
    pr(f'Q2RESULT mode={mode} verdict={"ACCEPT" if passed else "REJECT"} '
       f'gap_proxy_med={med if med is not None else "NA"} pairs={len(pairs)}')
    if result is not None:
        result.update(mode=mode, verdict=('ACCEPT' if passed else 'REJECT'),
                      med=med, pairs=len(pairs))
    pr('\n' + ('=== ACCEPT: Q2 gates pass ===' if passed else '=== REJECT/STOP: a gate failed ==='))
    return 0 if passed else 3

def near_pos(path):
    o = parse_obs(path); good = [r for r in o['recs'] if r['crc']==1 and r['st']==0]
    pairs = find_pairs(good)
    if not pairs: return None
    return 'first' if st.median([p['a']['rssi'] for p in pairs]) > \
                      st.median([p['b']['rssi'] for p in pairs]) else 'second'

def roleswap(bA, bB, tifs_dev):
    """each bundle = (obs, central, periph, near). BOTH configs are analyzed
    against ONE shared frozen gap_proxy_dev (from a single ESTABLISHED calibration
    artifact), must be ACCEPTED near/far runs, THEN the near/strong population must
    flip. No per-config naked timing value is accepted."""
    for tag, b in (('A', bA), ('B', bB)):
        rc = analyze(b[0], b[1], b[2], 'nearfar', b[3], int(tifs_dev), quiet=True)
        print(f'roleswap: config {tag} analyze (frozen gap_proxy_dev={tifs_dev}) -> '
              f'{"ACCEPT" if rc==0 else "REJECT"}')
        if rc != 0:
            print('=== REJECT: a role-swap input was not an accepted near/far run ==='); return 3
    a, b = near_pos(bA[0]), near_pos(bB[0])
    print(f'roleswap: A near-position={a} ; B near-position={b}')
    ok = a and b and a != b
    print('=== ' + ('ACCEPT: populations swapped ===' if ok else 'REJECT: role swap did not flip ==='))
    return 0 if ok else 3

# ---------------- self-test ----------------
def _synth(n, near, far, tifs=TIFS_EXPECT, miss_p=0.0, miss_both=0.0, overcount=0,
           aa=0x71764129, obs_aa=None, conn_map=EXPECT_MAP, chan_count=2, obs_ch=10,
           tx_link=1.0, c_tx_bad=False, nonmono=False, aa_change=False, n_start=1,
           n_conn=1, snap_bad_order=False, big_slop=False, loose_slop=False, cap_bad=False, miss_end_snap=False,
           cdev='1111111111111111', pdev='2222222222222222', btag=0xB007,
           same_dev=False, snap_role_bad=False, snap_boottag_bad=False, snap_ch_bad=False,
           Ts=1000, Te=11000):
    import tempfile
    obs_aa = obs_aa if obs_aa is not None else aa
    if cap_bad: Te = Ts + 3000
    recs, t, oseq = [], 100000, 0
    for e in range(n):
        c0, c1 = t, t+200*16; p0, p1 = c1+tifs, c1+tifs+200*16
        h = lambda k: ((e*2654435761+k) % 1000)/1000.0
        both = h(1) < miss_both; pm = both or (h(2) < miss_p)
        if not both: recs.append((oseq, near, c0, c1)); oseq += 1
        if not pm:   recs.append((oseq, far, p0, p1)); oseq += 1
        t = p1 + 7500*16
    for _ in range(overcount):
        recs.append((oseq, near, t, t+200*16)); oseq += 1
        recs.append((oseq, far, t+200*16+tifs, t+400*16+tifs)); oseq += 1; t += 7500*16
    obs = tempfile.mktemp(suffix='.txt')
    with open(obs, 'w') as f:
        f.write('HOSTMS 300 *** Booting ***\n')
        f.write(f'HOSTMS 500 mode=Q2 phy=1M AA=0x{obs_aa:08x} crcinit=0x555555 ch={obs_ch} whiten=1 cap=10000ms\n')
        f.write('HOSTMS 600 ARMED Q2 ch10\n')
        for _ in range(n_start):
            f.write(f'HOSTMS {Ts} CAPTURE-START Q2 ch10 cap=10000ms\n')
        for o_, rssi, a_, en in recs:
            f.write(f'HOSTMS {Ts+10} REC oseq={o_} s0=0x01 len=0 crc=1 rssi={rssi} pairid=0 '
                    f'txtag=0 st=0x00 addr={a_} end={en} air_us=0 gap_us=0\n')
        f.write(f'HOSTMS {Te} CAPTURE-END Q2\n')
        f.write(f'HOSTMS {Te+5} FROZEN: records={len(recs)} ring_full_drops=0 max_isr_ticks=50\n')
        f.write(f'HOSTMS {Te+6} LOSS: addr_irq={len(recs)} end_irq={len(recs)} records={len(recs)} '
                f'ring_full_drops=0 addr_minus_end=0 end_minus_recorded=0 reversal=0 stale_addr=0\n')
    cl, pl = tempfile.mktemp(suffix='.txt'), tempfile.mktemp(suffix='.txt')
    ptx = int(n*tx_link)
    pdev_eff = cdev if same_dev else pdev
    def evt(role, txmax, path, dev):
        L = [f'HOSTMS 100 Q2READY role={role} dev={dev} boottag=0x{btag:08x}\n']
        for _ in range(n_conn if role == 'C' else 0):
            L.append(f'HOSTMS 200 Q2CONN AA={aa:08x} CRCINIT=555555 hop=7 chan_count={chan_count} map={conn_map}\n')
        aa2 = (aa ^ 0xFF) if aa_change else aa
        st_sched, st_tx, en_sched, en_tx = (n, txmax, 0, 0) if nonmono else (0, 0, n, txmax)
        if role == 'C' and c_tx_bad:
            st_tx, en_tx = 0, n+5
        srole = ('P' if role == 'C' else 'C') if snap_role_bad else role
        sbt = (btag ^ 0xFF) if snap_boottag_bad else btag
        sch = 11 if snap_ch_bad else 10
        # realistic tight snaps: the runner snaps immediately before GO and right
        # after CAPTURE-END, so each boundary is tens of ms (<< the 3% ceiling).
        start_host = (Te + 1000) if snap_bad_order else (Ts - 60)
        end_host = Te + 60
        if loose_slop:                 # each boundary < MAX_SLOP but combined > 3%
            start_host, end_host = Ts - 220, Te + 220
        if big_slop:                   # a single boundary exceeds MAX_SLOP (parse reject)
            start_host = Ts - 6000
        L.append(f'HOSTMS {start_host} Q2SNAP role={srole} seq=0 boottag=0x{sbt:08x} aa=0x{aa:08x} sess=5 ch={sch} sched={st_sched} tx={st_tx}\n')
        if not miss_end_snap:
            L.append(f'HOSTMS {end_host} Q2SNAP role={srole} seq=1 boottag=0x{sbt:08x} aa=0x{aa2:08x} sess=5 ch={sch} sched={en_sched} tx={en_tx}\n')
        open(path, 'w').write(''.join(L))
    evt('C', n, cl, cdev); evt('P', ptx, pl, pdev_eff)
    return obs, cl, pl

def selftest():
    import os
    R = []
    def run(name, kw, mode, exp, near='central', tifs=None):
        o, c, p = _synth(**kw); rc = analyze(o, c, p, mode, near, tifs, quiet=True)
        for x in (o, c, p): os.unlink(x)
        R.append((name, rc == exp))
    T = TIFS_EXPECT
    # --- Q3 timing anchors (host <-> observer TIMER0 domain) ---
    ANC = [(1000, 0), (31000, 480_000_000)]   # 30s window: 30000ms <-> 480e6 ticks @16MHz
    R.append(('q3_reltick relative + wrap-safe',
              q3_reltick(1000, 1000) == 0 and q3_reltick(50, TIMER0_WRAP - 100) == 150))
    _rt, _u = q3_host_to_reltick(ANC, 16000)
    R.append(('q3_host_to_reltick maps midpoint host -> mid ticks',
              abs(_rt - 240_000_000) < 1 and abs(_u - 50 * (480_000_000/30000)) < 1))
    def _raises_v(f):
        try: f(); return False
        except ValueError: return True
    R.append(('q3_host_to_reltick rejects degenerate anchors', _raises_v(lambda: q3_host_to_reltick([(5,0),(5,9)], 5))))
    R.append(('q3_host_to_reltick handles wrapped anchor ticks',
              abs(q3_host_to_reltick([(1000, TIMER0_WRAP-240_000_000), (31000, 240_000_000)], 16000)[0] - 240_000_000) < 1))
    _lo, _hi, _un = q3_transition_window(ANC, 16000, [16100, 16050], guard_ticks=48000)
    R.append(('q3_transition_window is conservative (req..later-done +/- unc + guard)',
              _lo < 240_000_000 and _hi > 241_600_000 and _un > 0))
    # regression: the real smoke produced a Q2CONN interleaved with a Q2EVT line on
    # the shared UART ("Q2EVT role=CQ2CONN AA=..."); parse_conn must still find it.
    import tempfile as _tf
    _ic = _tf.mktemp(suffix='.txt')
    open(_ic, 'w').write('HOSTMS 6943 Q2EVT role=CQ2CONN AA=e90643ac CRCINIT=642c8a '
                         'hop=14 chan_count=2 map=000c000000\n')
    _cc, _ce = parse_conn(_ic); os.unlink(_ic)
    R.append(('parse_conn tolerates interleaved Q2CONN line',
              _ce == '' and _cc and _cc['aa'] == 0xe90643ac and _cc['crcinit'] == 0x642c8a
              and _cc['chan_count'] == 2 and _cc['map'] == '000c000000'))
    run('clean-symmetric', dict(n=300, near=-50, far=-52), 'symmetric', 0)
    run('symmetric-noisy-overlap', dict(n=300, near=-55, far=-58), 'symmetric', 0)
    run('clean-nearfar', dict(n=300, near=-45, far=-78), 'nearfar', 0, tifs=T)
    run('nearfar-no-tifsdev', dict(n=300, near=-45, far=-78), 'nearfar', 3)
    run('farside-obs-loss', dict(n=300, near=-45, far=-78, miss_p=0.15), 'nearfar', 3, tifs=T)
    run('both-missed', dict(n=300, near=-45, far=-78, miss_both=0.10), 'nearfar', 3, tifs=T)
    run('overcount', dict(n=300, near=-45, far=-78, overcount=40), 'nearfar', 3, tifs=T)
    run('far-too-strong', dict(n=300, near=-45, far=-58), 'nearfar', 3, tifs=T)
    run('snap-order-violation', dict(n=300, near=-45, far=-78, snap_bad_order=True), 'nearfar', 3, tifs=T)
    run('big-slop-parse-reject', dict(n=300, near=-45, far=-78, big_slop=True), 'nearfar', 3, tifs=T)
    run('loose-slop-frac-reject', dict(n=300, near=-45, far=-78, loose_slop=True), 'nearfar', 3, tifs=T)
    run('missing-end-snap', dict(n=300, near=-45, far=-78, miss_end_snap=True), 'nearfar', 3, tifs=T)
    run('cap-duration-bad', dict(n=300, near=-45, far=-78, cap_bad=True), 'nearfar', 3, tifs=T)
    run('two-capture-start', dict(n=300, near=-45, far=-78, n_start=2), 'nearfar', 3, tifs=T)
    run('multi-Q2CONN', dict(n=300, near=-45, far=-78, n_conn=2), 'nearfar', 3, tifs=T)
    run('reconnect-aa-change', dict(n=300, near=-45, far=-78, aa_change=True), 'nearfar', 3, tifs=T)
    run('counter-reset', dict(n=300, near=-45, far=-78, nonmono=True), 'nearfar', 3, tifs=T)
    run('wrong-connection-aa', dict(n=300, near=-45, far=-78, obs_aa=0xdeadbeef), 'nearfar', 3, tifs=T)
    run('wrong-channel-map', dict(n=300, near=-45, far=-78, conn_map='0030000000'), 'nearfar', 3, tifs=T)
    # sched/tx are NOT one-to-one: a healthy run with central tx != sched (denom
    # = actual TX) still ACCEPTs; only retention (observed/tx) gates.
    run('tx!=sched-still-accept', dict(n=300, near=-45, far=-78, c_tx_bad=True), 'nearfar', 0, tifs=T)
    run('periph-low-retention', dict(n=300, near=-45, far=-78, tx_link=1.2), 'nearfar', 3, tifs=T)
    # snapshot role/boot/channel/DEVICEID binding
    run('same-DEVICEID-reject', dict(n=300, near=-45, far=-78, same_dev=True), 'nearfar', 3, tifs=T)
    run('snap-wrong-role-reject', dict(n=300, near=-45, far=-78, snap_role_bad=True), 'nearfar', 3, tifs=T)
    run('snap-boottag-mismatch-reject', dict(n=300, near=-45, far=-78, snap_boottag_bad=True), 'nearfar', 3, tifs=T)
    run('snap-wrong-channel-reject', dict(n=300, near=-45, far=-78, snap_ch_bad=True), 'nearfar', 3, tifs=T)
    # role-swap: full bundles
    bA = _synth(300, near=-45, far=-78); bB = _synth(300, near=-78, far=-45); bC = _synth(300, near=-45, far=-78)
    import sys as _s; _o = _s.stdout; _s.stdout = open(os.devnull, 'w')
    rs1 = roleswap((bA[0],bA[1],bA[2],'central'), (bB[0],bB[1],bB[2],'periph'), T)
    rs2 = roleswap((bA[0],bA[1],bA[2],'central'), (bC[0],bC[1],bC[2],'central'), T)
    _s.stdout = _o
    R.append(('roleswap-accept', rs1 == 0)); R.append(('roleswap-noswap-reject', rs2 == 3))
    # CLI ENFORCEMENT: the naked-timing bypasses must be closed at the CLI. Invoke
    # this script as a subprocess (functions above are the in-process fast path).
    import subprocess
    nf = _synth(300, near=-45, far=-78); self = os.path.abspath(__file__)
    def cli(args): return subprocess.run([sys.executable, self, *args],
                                         capture_output=True, text=True).returncode
    R.append(('cli nearfar without --calib rejects',
              cli([nf[0],nf[1],nf[2],'--mode','nearfar']) == 3))
    R.append(('cli nearfar naked --tifs-dev is not accepted',
              cli([nf[0],nf[1],nf[2],'--mode','nearfar','--tifs-dev',str(T)]) != 0))
    R.append(('cli roleswap without --calib rejects',
              cli(['--roleswap',nf[0],nf[1],nf[2],'central',nf[0],nf[1],nf[2],'periph']) == 2))
    R.append(('cli bad --mode rejected (no fall-through to near/far)',
              cli([nf[0],nf[1],nf[2],'--mode','symetric']) == 2))
    for x in nf: os.unlink(x)
    # CALIBRATION-ARTIFACT VALIDATION: validate_calib recomputes the whole
    # contract, and a genuine combiner artifact drives a POSITIVE role-swap.
    import combine_calib as CC, json as _json, tempfile as _tf, shutil as _sh
    ctmp = _tf.mkdtemp(); cal = os.path.join(ctmp, 'cal.json'); forged = os.path.join(ctmp, 'f.json')
    _s.stdout = open(os.devnull, 'w')
    built = CC.combine([CC._mkcell(ctmp,'sa',T), CC._mkcell(ctmp,'sb',T)], cal)
    _s.stdout = _o
    R.append(('calib artifact built for validation tests', built == 0))
    R.append(('validate_calib accepts a genuine artifact', validate_calib(cal) == (T, None)))
    def forge(mut):
        cj = _json.load(open(cal)); mut(cj); _json.dump(cj, open(forged, 'w')); return validate_calib(forged)[1] is not None
    _json.dump({'status':'ESTABLISHED','frozen_gap_proxy_dev':T}, open(forged,'w'))
    R.append(('validate_calib rejects a forged naked {status,frozen} blob', validate_calib(forged)[1] is not None))
    R.append(('validate_calib rejects tampered frozen value', forge(lambda c: c.update(frozen_gap_proxy_dev=T+50))))
    R.append(('validate_calib rejects median>2 ticks from frozen', forge(lambda c: c['medians'].__setitem__(0, T+10))))
    R.append(('validate_calib rejects lineage-hash mismatch', forge(lambda c: c['lineage'].update(analyze_q2='deadbeef'))))
    R.append(('validate_calib rejects <2 inputs', forge(lambda c: (c.__setitem__('inputs', c['inputs'][:1]), c.__setitem__('medians', c['medians'][:1])))))
    R.append(('validate_calib rejects contract-field drift', forge(lambda c: c['contract'].update(spread_max_ticks=99))))
    R.append(('validate_calib rejects an input with a non-64hex log digest',
              forge(lambda c: c['inputs'][0]['sha256'].__setitem__('obs', 'MISSING'))))
    R.append(('validate_calib rejects an input with a bad manifest digest',
              forge(lambda c: c['inputs'][0].__setitem__('manifest_sha256', 'nope'))))
    R.append(('validate_calib rejects fabricated evidence (nonexistent dirs + aaaa digests)',
              forge(lambda c: [i.update(dir='nope_'+str(k),
                                        sha256=dict(obs='a'*64, central='a'*64, periph='a'*64),
                                        manifest_sha256='a'*64) for k, i in enumerate(c['inputs'])])))
    # internally-consistent median+frozen edit over UNTOUCHED evidence: digest
    # rehash passes, but re-analysis reproduces the true median and rejects.
    R.append(('validate_calib rejects internally-consistent median+frozen edit',
              forge(lambda c: (c.update(frozen_gap_proxy_dev=T+1),
                               c.__setitem__('medians', [T+1]*len(c['medians'])),
                               [i.__setitem__('median', T+1) for i in c['inputs']]))))
    # homogeneity substitution: repoint input[1] to a board-mismatched but
    # internally-consistent cell (digests patched to match); re-checked homogeneity
    # rejects it even though every per-input rehash passes.
    sc = CC._mkcell(ctmp, 'sc', T, board='nrf52840dk')
    def _sub(c):
        c['inputs'][1].update(dir=os.path.relpath(sc, ctmp),
                              sha256=dict(obs=CC.sha256(os.path.join(sc,'obs.txt')),
                                          central=CC.sha256(os.path.join(sc,'central.txt')),
                                          periph=CC.sha256(os.path.join(sc,'periph.txt'))),
                              manifest_sha256=CC.sha256(os.path.join(sc,'manifest.json')))
    R.append(('validate_calib rejects a homogeneity substitution', forge(_sub)))
    # positive role-swap CLI: loads and USES a valid artifact
    nfa = _synth(300, near=-45, far=-78); nfb = _synth(300, near=-78, far=-45)
    R.append(('cli roleswap with a valid artifact accepts',
              cli(['--roleswap','--calib',cal, nfa[0],nfa[1],nfa[2],'central', nfb[0],nfb[1],nfb[2],'periph']) == 0))
    for x in nfa + nfb: os.unlink(x)
    # editing a referenced log AFTER combine -> consumer rehash catches it
    with open(os.path.join(ctmp, 'sa', 'obs.txt'), 'a') as f: f.write('TAMPER\n')
    R.append(('validate_calib rejects a post-combine evidence edit', validate_calib(cal)[1] is not None))
    _sh.rmtree(ctmp, ignore_errors=True)
    for b in (bA, bB, bC):
        for x in b: os.unlink(x)
    print('=== SELFTEST ===')
    allok = all(v for _, v in R)
    for name, v in R: print(f'  [{"PASS" if v else "FAIL"}] {name}')
    print(f'SELFTEST: {"PASS" if allok else "FAIL"}')
    return 0 if allok else 1

def load_calib(path):
    """PROVENANCE-BOUND frozen reference: near/far + role-swap consume an
    ESTABLISHED calibration artifact whose WHOLE contract re-verifies (>=2 accepted
    inputs, medians reproduce the frozen value, spread within +-2, contract fields
    match rev-12, tool/protocol lineage matches). Never a naked number."""
    tifs, err = validate_calib(path)
    if err:
        print(f'REJECT: calibration {path} invalid: {err}'); sys.exit(3)
    print(f'[calib] frozen gap_proxy_dev={tifs} ticks (contract re-verified); artifact={path}')
    return tifs

def _take(a, flag):
    if flag in a:
        i = a.index(flag); v = a[i+1]; del a[i:i+2]; return v
    return None

if __name__ == '__main__':
    a = sys.argv[1:]
    if a and a[0] == '--selftest':
        sys.exit(selftest())
    if a and a[0] == '--roleswap':
        r = a[1:]
        calib = _take(r, '--calib')
        if calib is None or len(r) != 8 or r[3] not in ('central','periph') or r[7] not in ('central','periph'):
            print('usage: --roleswap --calib ART <obsA> <cA> <pA> <nearA> <obsB> <cB> <pB> <nearB>'
                  '  (nearA/nearB in {central,periph})')
            sys.exit(2)
        tifs_dev = load_calib(calib)
        sys.exit(roleswap((r[0],r[1],r[2],r[3]), (r[4],r[5],r[6],r[7]), tifs_dev))
    mode = _take(a, '--mode') or 'nearfar'
    near = _take(a, '--near') or 'central'
    calib = _take(a, '--calib')
    if mode not in ('symmetric', 'nearfar'):
        print(f'REJECT: --mode must be symmetric|nearfar (got {mode})'); sys.exit(2)
    if near not in ('central', 'periph'):
        print(f'REJECT: --near must be central|periph (got {near})'); sys.exit(2)
    tifs_dev = load_calib(calib) if calib is not None else None
    # --tifs-dev is intentionally NOT a CLI flag: near/far MUST use --calib (a
    # provenance-bound ESTABLISHED artifact). The raw integer is reachable only by
    # the in-process self-test calling analyze()/roleswap() directly.
    if len(a) != 3:
        print(__doc__); sys.exit(2)
    sys.exit(analyze(a[0], a[1], a[2], mode, near, tifs_dev))
