#!/usr/bin/env python3
"""Q2 capture runner -- command-gated startup + RUNTIME observer config +
ARMED/GO/atomic-snapshot handshake.

Flow:
  1. Flash the observer ONCE (generic Q2; runtime-configured -- no per-connection
     rebuild, no stale AA). It boots to CONFIG-READY and waits.
  2. Command-gated connection: reset both endpoints -> wait Q2READY -> periph 'A'
     (advertise) -> ADV-READY -> central 'C' (connect) -> wait connected +
     exactly one Q2CONN.
  3. Runtime config: send the observer `CFG aa=.. crc=.. ch=10 phy=1` -> require a
     matching CONFIG-ECHO -> wait ARMED.
  4. Handshake: 'S' to both endpoints (START snaps, seq0) -- WAIT both acks --
     'G' to observer -> CAPTURE-START..CAPTURE-END -> 'S' both (END snaps, seq1)
     -> wait Q2-DONE.
  5. analyze_q2.py.

Every captured line is host-timestamped (HOSTMS) by one reader per port; commands
are written to the same Serial under a lock.

Usage: q2_run.py --obs-port P --central-port P --periph-port P --obs-devid S
       --central-devid S --periph-devid S --outdir D --mode symmetric|nearfar
       [--near central|periph]
       [--calib gap-proxy-calibration.json]   # near/far: REQUIRED provenance-bound frozen ref
       --central-build DIR --periph-build DIR  # endpoint build dirs (MANDATORY for an accepted cell)
       [--obs-prebuilt-build DIR]             # preserved observer build dir, flashed --no-rebuild
       [--smoke | --force-quarantine]         # run fully but force QUARANTINED (integration smoke)
       [--q3 [--baseline-dwell-s S] [--transition-excl-ms MS]]  # Q3 mid-capture FSU trigger
       [--obs-app PATH] [--board B] [--skip-obs-flash]

Q3 mode (--q3; Q2 path unchanged when off): after 'G', WAIT for the observer's
CAPTURE-START (not a timer off 'G'), dwell --baseline-dwell-s, then send EXACTLY ONE
'F' to the central, which requests the reduced frame space MID-CAPTURE and emits
Q3FSU-REQ (aa/session/range/phys/types/rc/seq) + Q3FSU-DONE (role=C). The RESPONDER
(peripheral) logs its OWN Q3FSU-DONE role=P (peer-participation proof, bound to the
same AA/session). On-chip cross-val leg (peripheral): 'K' clears the histogram at
CAPTURE-START, 'D' FREEZES it AT CAPTURE-END (on-chip window ~= observer window
within the reported+gated boundary slops), and 'R' drains the frozen bins after the END snaps -> TIFSBIN (separate
150/100 @1M bins, drop=0). The clear/freeze/drain records are bound to
role=P/AA/session/life (exactly one lifecycle). Two SEPARATE incompleteness legs are
tracked: q3-control-plane-incomplete (REQ/DONE/peer) and q3-onchip-incomplete
(clear/freeze/drain/bins); both QUARANTINE, capture always preserved. A passing --q3 run is ALWAYS QUARANTINED (q3-preaccept-smoke) until
the Q3 step analyzer + accepted-cell protocol exist.
--baseline-dwell-s / --transition-excl-ms are parameterized here; their ACCEPTED
values are frozen in the smoke protocol, not in code. (The Q3 step analyzer, the
peripheral callback, and the on-chip drain are later steps.)

--smoke runs every flash + capture + analysis normally but forces a NON-ACCEPT
verdict (nonzero exit): QUARANTINED (quarantine_reason=integration-smoke) when the
analyzer PASSES, or REJECT if the analyzer fails. Either way it can never become
the first accepted cell.

A cell is ACCEPT only with a passing analyzer AND a verified observer image AND
bound endpoint build provenance. The runner FLASHES the supplied endpoint build
dirs (via `west flash -d DIR --no-rebuild`) so the hashed image == the RUNNING
image -- an unrelated dir can no longer be hashed into an ACCEPT. Provenance is
RESOLVED, not assumed: each of --central-build/--periph-build must contain a
readable zephyr/zephyr.hex and zephyr/.config that hash to real 64-hex digests (a
nonexistent dir -> "MISSING" does NOT count). --skip-obs-flash (unverified image),
a missing/invalid endpoint artifact, or omitting the build dirs forces a
QUARANTINED verdict (nonzero exit): the cell can never be accepted or feed the
combiner. There is no --tifs-dev; near/far consumes an ESTABLISHED --calib only.

For a matched pair of controls, build the observer ONCE and pass that preserved
build dir to BOTH runs via --obs-prebuilt-build (a fresh `west build` is not
bit-reproducible, so its observer.hex would differ and fail the homogeneity gate).
`west flash` needs the build dir (runners.yaml), so a bare --hex-file alone fails.
"""
import argparse, os, re, sys, time, threading, subprocess
try:
    import serial
except ImportError:
    serial = None   # a live run needs it (checked when a Port opens); --selftest does not

T0 = time.monotonic()
def hostms(): return int((time.monotonic() - T0) * 1000)

class Port:
    def __init__(self, name, path):
        if serial is None:
            print('pip install pyserial'); sys.exit(2)
        self.s = serial.Serial(name, 115200, timeout=0.1)
        self.f = open(path, 'w'); self.path = path
        self.lock = threading.Lock(); self.stop = threading.Event()
        self.th = threading.Thread(target=self._read, daemon=True); self.th.start()
    def _read(self):
        while not self.stop.is_set():
            with self.lock:
                ln = self.s.readline()
            if ln:
                self.f.write(f'HOSTMS {hostms()} ' + ln.decode('utf-8', 'replace')); self.f.flush()
            else:
                time.sleep(0.005)
    def send(self, b):
        with self.lock:
            self.s.write(b)
    def send_slow(self, b):
        """byte-by-byte with gaps: the observer polls the un-buffered console
        UART every ~2ms and would drop a fast burst's leading bytes."""
        for x in b:
            with self.lock:
                self.s.write(bytes([x])); self.s.flush()
            time.sleep(0.004)
    def close(self):
        self.stop.set(); self.th.join(timeout=2); self.s.close(); self.f.close()

def wait_for(path, token, timeout, after=0, poll=0.02):
    # poll defaults to 20ms (was 100ms): the snapshot-boundary waits (seq0 acks
    # before GO, CAPTURE-END before the END snaps) sit on the critical path, so a
    # tighter poll lands each Q2SNAP closer to the capture edge -> smaller slop ->
    # a tighter retention lower bound. Files are tiny, so 50 reads/s is cheap.
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        try:
            for ln in open(path, errors='replace'):
                if token in ln:
                    m = re.match(r'HOSTMS (\d+) ', ln)
                    if not after or (m and int(m.group(1)) >= after):
                        return True
        except FileNotFoundError:
            pass
        time.sleep(poll)
    return False

def sh(cmd):
    print('[sh]', ' '.join(cmd[:6]), '...'); return subprocess.call(cmd)

def reset(devid):
    return subprocess.call(['nrfutil', 'device', 'reset', '--serial-number', devid],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def die(msg, ports):
    print('[abort]', msg); [p.close() for p in ports if p]; sys.exit(2)

Q3_MIN_PLATEAU_S = 3.0   # each plateau (pre-step, post-step-minus-transition) must last >= this
# Q3 analyzer acceptance knobs surfaced as runner args (recorded in the manifest).
# These MIRROR analyze_q3's frozen constants; the runner selftest asserts they match.
Q3_GUARD_TICKS_DEFAULT = 160_000
Q3_MIN_PLATEAU_DEFAULT = 30
Q3_STEADY_SETTLE_MS_DEFAULT = 2000   # f100-steady: settle after FSU completes, BEFORE the window
STEADY_SETTLE_PAD_S = 0.5            # extra pad over the frozen minimum so the analyzer gate clears
# rev-6: the peripheral's auto conn-param update resets the negotiated frame space
# (ull_conn_update_parameters). The steady ABBA arm EVENT-GATES on it: request FSU only
# AFTER it. The final negotiated interval is FROZEN at the block-b1 on-air observation
# (50ms = 40 x 1.25ms units); both endpoints must agree and match it, and combine_abba
# requires identical final params across the four cells.
Q3_ABBA_FINAL_INTERVAL_UNITS = 40    # 50 ms (1.25 ms units); frozen from failed block b1
Q3_PARAMUPD_TIMEOUT_S = 20           # max wait for the ~5 s auto update on both endpoints

def _paramupd_rows(text, role):
    return [l for l in text.splitlines() if f'Q3PARAMUPD role={role}' in l]

def _kvparse(line):
    d = {}
    for k, v in re.findall(r'(\w+)=(0x[0-9a-fA-F]+|\d+)', line):
        d[k] = int(v, 16) if v.startswith('0x') else int(v)
    return d

def wait_paramupd(cf, pf, aa):
    """rev-6 EVENT GATE: wait for the peripheral auto connection-parameter update on BOTH
    endpoints, then require -- exactly one record each, bound to the connection AA, agreeing
    on interval/latency/timeout, with interval == the frozen final interval (50 ms). Returns
    (ok, reason_or_None, {interval,latency,timeout})."""
    if not (wait_for(cf, 'Q3PARAMUPD role=C', Q3_PARAMUPD_TIMEOUT_S) and
            wait_for(pf, 'Q3PARAMUPD role=P', Q3_PARAMUPD_TIMEOUT_S)):
        return False, 'no Q3PARAMUPD on both endpoints (auto param-update not observed)', {}
    crows, prows = _paramupd_rows(open(cf).read(), 'C'), _paramupd_rows(open(pf).read(), 'P')
    if len(crows) != 1 or len(prows) != 1:
        return False, f'expected exactly one param-update each (C={len(crows)} P={len(prows)})', {}
    c, p = _kvparse(crows[0]), _kvparse(prows[0])
    if c.get('aa') != int(aa, 16) or p.get('aa') != int(aa, 16):
        return False, 'param-update AA != connection AA', {}
    for f in ('interval', 'latency', 'timeout'):
        if c.get(f) != p.get(f):
            return False, f'endpoints disagree on {f} (C={c.get(f)} P={p.get(f)})', {}
    if c.get('interval') != Q3_ABBA_FINAL_INTERVAL_UNITS:
        return False, (f'final interval {c.get("interval")} != frozen {Q3_ABBA_FINAL_INTERVAL_UNITS} '
                       f'({Q3_ABBA_FINAL_INTERVAL_UNITS*1.25:.0f}ms)'), {}
    return True, None, {'interval': c['interval'], 'latency': c['latency'], 'timeout': c['timeout']}

def q3_plan(cap_ms, baseline_dwell_s, transition_excl_ms, min_plateau_s=Q3_MIN_PLATEAU_S):
    """Validate the mid-capture F schedule's NOMINAL capture budget: raises ValueError
    unless a pre-step plateau AND a post-step budget (window - dwell - transition) of
    >= min_plateau_s each fit the window. NOTE post_s is computed from when F is SENT,
    not from FSU COMPLETION -- a slow completion shortens the real post plateau, so the
    Q3 analyzer (later) must recompute the post-plateau from the completion timestamp
    and reject if insufficient. This is a nominal budget check, not a guarantee.
    Returns {f_at_s, pre_s, post_s}."""
    cap_s = cap_ms / 1000.0
    pre_s = baseline_dwell_s
    post_s = cap_s - baseline_dwell_s - transition_excl_ms / 1000.0
    if pre_s < min_plateau_s:
        raise ValueError(f'baseline dwell {pre_s}s < min plateau {min_plateau_s}s (no pre-step data)')
    if post_s < min_plateau_s:
        raise ValueError(f'post-step plateau {post_s:.2f}s < min {min_plateau_s}s '
                         f'(dwell {baseline_dwell_s}s + transition {transition_excl_ms}ms too large for {cap_s}s)')
    return {'f_at_s': baseline_dwell_s, 'pre_s': pre_s, 'post_s': post_s}

def _q3_fields(line):
    """parse a Q3FSU-* record's HOSTMS + K=V pairs (hex/decimal/token)."""
    d = {}
    m = re.match(r'HOSTMS (\d+) ', line)
    d['_hostms'] = int(m.group(1)) if m else None
    for k, v in re.findall(r'(\w+)=(\S+)', line):
        if v.startswith('0x'):
            try: d[k] = int(v, 16)
            except ValueError: d[k] = v
        elif re.fullmatch(r'-?\d+', v):
            d[k] = int(v)
        else:
            d[k] = v
    return d

Q3_INITIATOR_PEER = 2   # BT_HCI_LE_FRAME_SPACE_UPDATE_INITIATOR_PEER (0=local host,1=local ctlr)

def q3_completeness(central_text):
    """Decide if the ONE mid-capture FSU is complete: exactly one accepted request +
    one completion whose STRUCTURED FIELDS all agree -- same AA & session, seq==1,
    selected spacing == the requested reduction target, matching PHY & spacing-types,
    and DONE chronologically AFTER REQ. Returns (verdict, reason)."""
    rejects = re.findall(r'Q3FSU-REJECT .*?reason=(\S+)', central_text)
    if rejects:
        return 'incomplete', f'request rejected ({rejects[0]})'
    reqs = [_q3_fields(l) for l in central_text.splitlines() if 'Q3FSU-REQ' in l]
    dones = [_q3_fields(l) for l in central_text.splitlines() if 'Q3FSU-DONE' in l]
    if len(reqs) != 1:
        return 'incomplete', f'{len(reqs)} Q3FSU-REQ (expected exactly one)'
    if len(dones) != 1:
        return 'incomplete', f'{len(dones)} Q3FSU-DONE (expected exactly one)'
    q, o = reqs[0], dones[0]
    # PRESENCE-STRICT: a compared field that is ABSENT must NOT satisfy the equality
    # via None==None. Require every field present on both records first.
    miss = ([f'REQ.{k}' for k in ('role','aa','sess','min','max','phys','types','rc','seq') if q.get(k) is None] +
            [f'DONE.{k}' for k in ('role','aa','sess','status','spacing','types','phys') if o.get(k) is None])
    if miss:                                return 'incomplete', f'missing FSU fields {miss}'
    if q.get('role') != 'C':                return 'incomplete', f"REQ role={q.get('role')} (expected C)"
    if o.get('role') != 'C':                return 'incomplete', f"DONE role={o.get('role')} (expected C)"
    if q.get('rc') != 0:                    return 'incomplete', f"REQ rc={q.get('rc')} (nonzero)"
    if q.get('seq') != 1:                   return 'incomplete', f"REQ seq={q.get('seq')} (expected 1)"
    if o.get('status') != 0:                return 'incomplete', f"DONE status={o.get('status')} (nonzero)"
    if q.get('aa') != o.get('aa'):          return 'incomplete', f"AA mismatch REQ {q.get('aa')} != DONE {o.get('aa')}"
    if q.get('sess') != o.get('sess'):      return 'incomplete', f"session mismatch {q.get('sess')} != {o.get('sess')}"
    if o.get('spacing') != q.get('min'):    return 'incomplete', f"selected spacing {o.get('spacing')} != requested {q.get('min')}"
    if q.get('phys') != o.get('phys'):      return 'incomplete', f"PHY mismatch {q.get('phys')} != {o.get('phys')}"
    if q.get('types') != o.get('types'):    return 'incomplete', f"spacing-types mismatch {q.get('types')} != {o.get('types')}"
    if q.get('_hostms') is not None and o.get('_hostms') is not None and o['_hostms'] <= q['_hostms']:
        return 'incomplete', 'DONE not chronologically after REQ'
    return 'complete', ''

def q3_peer_participation(central_text, periph_text):
    """PEER-PARTICIPATION proof: the RESPONDER (peripheral) must log its OWN FSU
    completion bound to the SAME AA/session as the central's request, with the same
    selected spacing. The central's HCI initiator field alone does NOT prove the peer
    engaged. Returns (verdict, reason)."""
    reqs = [_q3_fields(l) for l in central_text.splitlines() if 'Q3FSU-REQ' in l]
    pdones = [_q3_fields(l) for l in periph_text.splitlines() if 'Q3FSU-DONE role=P' in l]
    if len(reqs) != 1:
        return 'incomplete', f'{len(reqs)} central Q3FSU-REQ'
    if len(pdones) != 1:
        return 'incomplete', f'{len(pdones)} peripheral Q3FSU-DONE (expected exactly one)'
    q, o = reqs[0], pdones[0]
    miss = ([f'REQ.{k}' for k in ('aa','sess','min','phys','types') if q.get(k) is None] +
            [f'pDONE.{k}' for k in ('role','aa','sess','status','spacing','types','phys') if o.get(k) is None])
    if miss:                                return 'incomplete', f'missing peripheral FSU fields {miss}'
    if o.get('role') != 'P':                return 'incomplete', f"peripheral DONE role={o.get('role')} (expected P)"
    if o.get('status') != 0:                return 'incomplete', f"peripheral DONE status={o.get('status')} (nonzero)"
    if q.get('aa') != o.get('aa'):          return 'incomplete', f"peripheral AA {o.get('aa')} != connection {q.get('aa')}"
    if q.get('sess') != o.get('sess'):      return 'incomplete', f"peripheral session {o.get('sess')} != {q.get('sess')}"
    if o.get('spacing') != q.get('min'):    return 'incomplete', f"peripheral spacing {o.get('spacing')} != requested {q.get('min')}"
    if q.get('phys') != o.get('phys'):      return 'incomplete', f"peripheral PHY {o.get('phys')} != request {q.get('phys')}"
    if q.get('types') != o.get('types'):    return 'incomplete', f"peripheral spacing-types {o.get('types')} != request {q.get('types')}"
    if q.get('_hostms') is not None and o.get('_hostms') is not None and o['_hostms'] <= q['_hostms']:
        return 'incomplete', 'peripheral completion not after the request'
    # `initiator` semantics verified against hci_types.h: 0=local host, 1=local ctlr,
    # 2=PEER. The RESPONDER (peripheral) is driven by the central's request, so its
    # completion MUST report initiator=PEER -- this distinguishes a genuine peer-driven
    # adoption from a spurious/local completion.
    if o.get('initiator') != Q3_INITIATOR_PEER:
        return 'incomplete', f"peripheral initiator={o.get('initiator')} != PEER({Q3_INITIATOR_PEER})"
    return 'complete', ''

def q3_onchip_bins(periph_text):
    """Parse `TIFSBIN tifs=.. phy=.. n=.. nv=.. min=.. med=.. max=.. drop=..` drain
    lines into a LIST of per-bin dicts (a list, not a keyed map, so DUPLICATE
    (tifs,phy) bins remain detectable)."""
    out = []
    for l in periph_text.splitlines():
        if 'TIFSBIN' not in l:
            continue
        f = {k: int(v) for k, v in re.findall(r'(\w+)=(\d+)', l)}
        if 'tifs' in f and 'phy' in f:
            out.append(f)
    return out

def q3_onchip_check(periph_text, conn_aa, req_sess=None, expect_tifs=None, expect_phy=1):
    """Validate the FULL peripheral on-chip lifecycle + drain: EXACTLY ONE
    clear/freeze/drain (role=P), CHRONOLOGICAL clear<freeze<drain, all bound to
    conn_aa and the SAME life, with session == the Q3 REQUEST session (req_sess);
    NO duplicate (tifs,phy) bins; every bin at expect_phy (1M) with n>0, 0<nv<=n,
    drop==0 and min<=med<=max (all present); and -- if expect_tifs is given --
    EXACTLY those spacings. Returns (verdict, reason)."""
    rows = lambda tag: [_q3_fields(l) for l in periph_text.splitlines() if tag in l]
    cl, fz, dr = rows('TIFS-CLEARED role=P'), rows('TIFS-FROZEN role=P'), rows('TIFS-DRAIN-DONE role=P')
    for name, r in (('TIFS-CLEARED', cl), ('TIFS-FROZEN', fz), ('TIFS-DRAIN-DONE', dr)):
        if len(r) != 1:
            return 'incomplete', f'{len(r)} {name} (expected exactly one lifecycle)'
    c, f, d = cl[0], fz[0], dr[0]
    for name, r in (('CLEARED', c), ('FROZEN', f), ('DRAIN-DONE', d)):
        if r.get('aa') != conn_aa:
            return 'incomplete', f'on-chip {name} aa {r.get("aa")} != connection {conn_aa}'
    if not (c.get('life') == f.get('life') == d.get('life')):
        return 'incomplete', 'on-chip life-seq mismatch across clear/freeze/drain'
    if not (c.get('sess') == f.get('sess') == d.get('sess')):
        return 'incomplete', 'on-chip session mismatch across clear/freeze/drain'
    if req_sess is not None and c.get('sess') != req_sess:
        return 'incomplete', f"on-chip session {c.get('sess')} != Q3 request session {req_sess}"
    if not (c.get('_hostms') < f.get('_hostms') < d.get('_hostms')):
        return 'incomplete', 'on-chip lifecycle not chronological (clear<freeze<drain)'
    bins, seen = q3_onchip_bins(periph_text), set()
    for b in bins:
        key = (b['tifs'], b['phy'])
        if key in seen:
            return 'incomplete', f'duplicate on-chip bin (tifs={b["tifs"]}, phy={b["phy"]})'
        seen.add(key)
        if b['phy'] != expect_phy:
            return 'incomplete', f'on-chip bin tifs={b["tifs"]} phy={b["phy"]} != {expect_phy} (expected 1M)'
        if b.get('n', 0) <= 0:
            return 'incomplete', f'on-chip bin tifs={b["tifs"]} n={b.get("n")} (expected >0)'
        if not (0 < b.get('nv', 0) <= b['n']):
            return 'incomplete', f'on-chip bin tifs={b["tifs"]} nv={b.get("nv")} not in (0, n]'
        if b.get('drop', 1) != 0:
            return 'incomplete', f'on-chip bin tifs={b["tifs"]} drop={b.get("drop")} (acceptance requires 0)'
        if not all(k in b for k in ('min', 'med', 'max')) or not (b['min'] <= b['med'] <= b['max']):
            return 'incomplete', f"on-chip bin tifs={b['tifs']} bad schema/order (min<=med<=max): {b.get('min')},{b.get('med')},{b.get('max')}"
    if expect_tifs is not None and sorted(b['tifs'] for b in bins) != sorted(expect_tifs):
        return 'incomplete', f"on-chip spacings {sorted(b['tifs'] for b in bins)} != expected {sorted(expect_tifs)}"
    return 'complete', ''

Q3_BOUNDARY_SLOP_MS = 1000   # clear-after-START / freeze-after-END host-arrival allowance

def q3_boundary_slops(obs_text, periph_text, max_slop_ms=Q3_BOUNDARY_SLOP_MS):
    """The on-chip window is [clear, freeze], which BRACKETS the observer window
    [CAPTURE-START, CAPTURE-END] only APPROXIMATELY: clear lands shortly AFTER
    CAPTURE-START and freeze shortly AFTER CAPTURE-END. Report both boundary slops
    (host ms) and gate 0 <= slop <= max_slop_ms so the two windows track. Returns
    (verdict, reason, slops)."""
    def hm(text, tok):
        for l in text.splitlines():
            if tok in l:
                m = re.match(r'HOSTMS (\d+) ', l)
                if m: return int(m.group(1))
        return None
    cs, ce = hm(obs_text, 'CAPTURE-START'), hm(obs_text, 'CAPTURE-END')
    cl, fz = hm(periph_text, 'TIFS-CLEARED'), hm(periph_text, 'TIFS-FROZEN')
    if None in (cs, ce, cl, fz):
        return 'incomplete', 'missing boundary anchor (CAPTURE-START/END or CLEARED/FROZEN)', {}
    slops = {'clear_slop_ms': cl - cs, 'freeze_slop_ms': fz - ce}
    if not (0 <= slops['clear_slop_ms'] <= max_slop_ms):
        return 'incomplete', f"clear slop {slops['clear_slop_ms']}ms not in [0,{max_slop_ms}]", slops
    if not (0 <= slops['freeze_slop_ms'] <= max_slop_ms):
        return 'incomplete', f"freeze slop {slops['freeze_slop_ms']}ms not in [0,{max_slop_ms}]", slops
    return 'complete', '', slops

def main():
    ap = argparse.ArgumentParser()
    for x in ('obs-port','central-port','periph-port','obs-devid','central-devid','periph-devid','outdir'):
        ap.add_argument('--'+x, required=True)
    ap.add_argument('--mode', required=True, choices=['symmetric','nearfar'])
    ap.add_argument('--near', default='central', choices=['central','periph'])
    ap.add_argument('--calib', default='')       # near/far: provenance-bound frozen ref (gap-proxy-calibration.json)
    # NOTE: no --tifs-dev. near/far MUST use a --calib artifact (ESTABLISHED). The
    # raw integer path exists only inside analyze_q2's self-test.
    ap.add_argument('--obs-app', default=os.path.expanduser('~/Desktop/develop/zenoh-pico-ble-test/pca10040-radio-observer'))
    ap.add_argument('--board', default='nrf52dk/nrf52832'); ap.add_argument('--skip-obs-flash', action='store_true')
    ap.add_argument('--obs-prebuilt-build', default='')  # preserved observer BUILD DIR -> flash --no-rebuild (stable hex)
    ap.add_argument('--central-build', default='')   # endpoint build dirs; FLASHED this run so the hashed
    ap.add_argument('--periph-build', default='')    # image is the running image. Need zephyr/zephyr.hex + zephyr/.config.
    ap.add_argument('--smoke', '--force-quarantine', dest='smoke', action='store_true',
                    help='run everything but never ACCEPT: QUARANTINED if the analyzer passes, else REJECT (integration smoke)')
    # Q3 mode (explicit flag; Q2 default path unchanged). Baseline dwell + transition
    # exclusion are PARAMETERIZED now; their ACCEPTED values are frozen in the smoke
    # protocol, not here.
    ap.add_argument('--q3', action='store_true',
                    help='Q3 mid-capture FSU: after CAPTURE-START, dwell, send one F to the central')
    ap.add_argument('--baseline-dwell-s', type=float, default=8.0)
    # PLANNING budget only: --transition-excl-ms sizes q3_plan so BOTH plateaus fit the
    # window. It does NOT drive the analyzer's exclusion -- analyze_q3 excludes the
    # TIMESTAMP-derived [request..later-DONE +/- jitter + guard] envelope. It must
    # exceed that envelope (guard + host jitter, both sides) or the plan under-budgets.
    ap.add_argument('--transition-excl-ms', type=float, default=500.0)
    # Q3 analyzer acceptance knobs, bound through the runner + recorded in the manifest
    # (defaults MIRROR analyze_q3's frozen constants; the selftest asserts no drift).
    ap.add_argument('--q3-guard-ticks', type=int, default=Q3_GUARD_TICKS_DEFAULT)
    ap.add_argument('--q3-min-plateau', type=int, default=Q3_MIN_PLATEAU_DEFAULT)
    ap.add_argument('--q3-contract-deviation', default=None,
                    help='REQUIRED reason if a Q3 knob differs from frozen; forces a non-accept '
                         '(the analyzer can never return METRICS-OK under a deviation)')
    ap.add_argument('--q3-arm', choices=('f100', 'f150'), default='f100',
                    help='central request arm for the FSU config assertion (f100=100/150, f150=150/0)')
    # Q3 STEADY-STATE (ABBA confirmation arm): ONE plateau per cell, no mid-step trigger.
    # f150 = no-request 150us control; f100 = FSU completed + settled BEFORE the window.
    ap.add_argument('--q3-steady', action='store_true',
                    help='steady-state single-plateau capture (invokes q3_steady_analyze; f150 or f100)')
    ap.add_argument('--abba-campaign-id', default='',
                    help='ABBA campaign binding recorded in the manifest (same id across the 4-cell block)')
    ap.add_argument('--abba-seq', type=int, default=None,
                    help='position 0..3 of this cell in the frozen f150/f100/f100/f150 ABBA block')
    a = {k.replace('-', '_'): v for k, v in vars(ap.parse_args()).items()}
    if a['q3'] and not a['calib']:
        print('[abort] --q3 requires --calib (the analyzer gates the plateau against '
              'the frozen 150us baseline)'); sys.exit(2)
    if a['q3_steady'] and not a['q3']:
        print('[abort] --q3-steady requires --q3'); sys.exit(2)
    if steady_knob_override(a['q3_steady'], a['q3_guard_ticks'], a['q3_min_plateau'],
                            a['q3_contract_deviation']):
        # the steady analyzer IGNORES guard/min-plateau, but the overridden values would
        # still enter the accepted manifest contract -> a silent contract mutation. Force
        # the operator to either drop the override or declare it (which forces quarantine).
        print('[abort] --q3-steady ignores --q3-guard-ticks/--q3-min-plateau; overriding them '
              'without --q3-contract-deviation would silently mutate the accepted contract. '
              'Drop the override, or pass --q3-contract-deviation "<reason>" (forces quarantine).'); sys.exit(2)
    if a['q3'] and not a['q3_steady']:   # mid-step: fail fast if BOTH plateaus can't fit the window
        try:
            q3_plan(30000, a['baseline_dwell_s'], a['transition_excl_ms'])
        except ValueError as e:
            print(f'[abort] --q3 timing invalid: {e}'); sys.exit(2)
    # ABBA cell binding, MANDATORY on the accepting steady path (enforced before any
    # hardware mutation): every non-smoke --q3-steady run MUST carry a nonempty campaign
    # id, a seq in range, and the arm the frozen A/B/B/A sequence demands at that seq --
    # so a stray steady run can never become an individual ACCEPT outside a campaign.
    # A standalone steady diagnostic uses --smoke (which can never accept).
    if a['q3_steady'] and not a['smoke']:
        bok, breason = abba_binding_ok(a['abba_campaign_id'], a['abba_seq'], a['q3_arm'])
        if not bok:
            print(f'[abort] --q3-steady (accepting path): {breason}; use --smoke for a standalone '
                  'steady diagnostic (cannot accept)'); sys.exit(2)
    # ABSOLUTE monotonic clock (system-wide, comparable ACROSS runs on this boot) so the
    # ABBA combiner can PROVE strictly-ordered non-overlapping collection.
    a['run_start_ns'] = time.monotonic_ns()
    # capture source-tree cleanliness BEFORE creating any output under the repo,
    # else writing logs would make tree_dirty spuriously true.
    a['pre_run_dirty'] = bool(subprocess.run(['git','status','--porcelain'], capture_output=True,
                                             text=True, cwd=os.path.dirname(__file__)).stdout.strip())
    a['q3_cp_reason'] = None      # control-plane (REQ/DONE/peer) incompleteness
    a['q3_paramupd_reason'] = None  # rev-6 steady: conn-param-update EVENT-GATE incompleteness
    a['q3_final_params'] = {}     # rev-6 steady: frozen final {interval,latency,timeout}
    a['q3_onchip_reason'] = None  # on-chip (clear/freeze/drain/bins) incompleteness -- SEPARATE leg
    a['q3_boundary_slops'] = None # clear-after-START / freeze-after-END host slops (reported)
    a['q3_fsu_reason'] = None     # endpoint FSU-config assertion failure (wrong firmware) -- SEPARATE leg
    a['q3_fsu_assert'] = None     # the assert_fsu_config.py output (both endpoints)
    a['q3_assert_sha'] = None     # sha256 of assert_fsu_config.py used this run
    os.makedirs(a['outdir'], exist_ok=True)
    of, cf, pf = (os.path.join(a['outdir'], n) for n in ('obs.txt', 'central.txt', 'periph.txt'))

    # (0-q3) HARD FSU-config GATE, BEFORE ANY hardware mutation (observer OR endpoint
    # flashing): a --q3 run must not flash+interpret a stale/wrong build (e.g. the 150us
    # floor) as a physical result. Validate BOTH endpoint .config files (central at its
    # arm, periph as responder); on either failure, record the assertion + script hash
    # to a manifest and ABORT before flashing anything (QUARANTINED, q3-fsu-config-invalid).
    if a['q3']:
        if a['q3_arm'] == 'f150' and not a['q3_steady']:
            # f150 (APP_FSU_MAX_US=0) is a no-request control: the central rejects 'F'
            # (no-request-build) and the MID-STEP analyzer is registered for 100->150.
            # f150 is only executable on the dedicated STEADY-STATE path (--q3-steady).
            print('[abort] --q3 --q3-arm f150 is not an executable mid-step path '
                  '(f150 is the no-request control; use --q3-steady for the f150 arm, '
                  'and f100 for the mid-step smoke)'); sys.exit(2)
        cc = os.path.join(a['central_build'] or '', 'zephyr', '.config')
        pc = os.path.join(a['periph_build'] or '', 'zephyr', '.config')
        ok, a['q3_fsu_reason'], a['q3_fsu_assert'], a['q3_assert_sha'] = \
            q3_fsu_config_gate(cc, pc, a['q3_arm'])
        print(a['q3_fsu_assert'])
        if not ok:
            import json
            stub = {'verdict': 'QUARANTINED', 'cell_exit': 3,
                    'quarantine_reason': a['q3_fsu_reason'],
                    'note': 'aborted BEFORE any flashing: endpoint FSU config invalid (wrong firmware)',
                    'q3_arm': a['q3_arm'], 'q3_fsu_assert': a['q3_fsu_assert'],
                    'args': a,
                    'sha256': {'assert_fsu_config.py': a['q3_assert_sha'],
                               'central.config': _sha256(cc), 'periph.config': _sha256(pc)}}
            with open(os.path.join(a['outdir'], 'manifest.json'), 'w') as f:
                json.dump(stub, f, indent=2); f.write('\n')
            print(f'[abort] --q3 endpoint FSU config invalid -> NOTHING flashed. '
                  f'reason={a["q3_fsu_reason"]} (manifest.json written)'); sys.exit(3)

    # (1) resolve (and, if fresh, BUILD) the observer image BEFORE snapshotting -- so the
    # snapshot can capture the actual hex/elf/.config that will be flashed.
    obs_bd = a['obs_prebuilt_build'] or '/tmp/q2obs'
    if not a['skip_obs_flash']:
        if a['obs_prebuilt_build']:
            obs_hex = os.path.join(obs_bd, 'zephyr', 'zephyr.hex')
            if not (os.path.exists(obs_hex) and os.path.exists(os.path.join(obs_bd,'zephyr','.config'))):
                print(f'--obs-prebuilt-build {obs_bd} lacks zephyr/zephyr.hex + zephyr/.config'); sys.exit(2)
        else:
            if sh(['west','build','-b',a['board'],'-d',obs_bd,a['obs_app'],'-p','always','--','-DQ2=1']) != 0:
                print('observer build failed'); sys.exit(2)
    a['obs_hex_path'] = os.path.join(obs_bd, 'zephyr', 'zephyr.hex')

    # (1a-q3) SNAPSHOT + VALIDATE firmware INTO the cell BEFORE flashing (time-of-check ==
    # time-of-use). Copies all nine artifacts, requires completeness + valid digests, and
    # reruns assert_fsu_config on the ARCHIVED endpoint configs. The flash then reads these
    # SAME build dirs; archive() re-verifies they are byte-identical post-run (TOCTOU). A
    # cell can ACCEPT (--q3) only if this snapshot is valid AND unchanged (decide_verdict).
    a['fw_snapshot_ok'], a['fw_snapshot_reason'], a['fw_snapshot'] = True, '', {}
    if a['q3']:
        a['fw_snapshot_ok'], a['fw_snapshot_reason'], a['fw_snapshot'] = snapshot_firmware(
            a['outdir'], obs_bd, a['central_build'], a['periph_build'], a['skip_obs_flash'], a['q3_arm'])
        print(f'[fw-snapshot] {"OK" if a["fw_snapshot_ok"] else "INVALID: " + a["fw_snapshot_reason"]} '
              f'({len(a["fw_snapshot"])} artifacts archived pre-flash)')

    # (1b) FLASH the observer + endpoints from those SAME (now-snapshotted) build dirs.
    if not a['skip_obs_flash']:
        obs_hex = os.path.join(obs_bd, 'zephyr', 'zephyr.hex')
        if sh(['west','flash','-d',obs_bd,'--no-rebuild','--hex-file',obs_hex,'--dev-id',a['obs_devid']]) != 0:
            print('observer flash failed'); sys.exit(2)
    for role, bd, devid in (('central', a['central_build'], a['central_devid']),
                            ('periph',  a['periph_build'],  a['periph_devid'])):
        if bd:
            hexf = os.path.join(bd, 'zephyr', 'zephyr.hex')
            if not (os.path.exists(hexf) and os.path.exists(os.path.join(bd,'zephyr','.config'))):
                print(f'--{role}-build {bd} lacks zephyr/zephyr.hex + zephyr/.config'); sys.exit(2)
            if sh(['west','flash','-d',bd,'--no-rebuild','--hex-file',hexf,'--dev-id',devid]) != 0:
                print(f'{role} endpoint flash failed'); sys.exit(2)

    C = Port(a['central_port'], cf); P = Port(a['periph_port'], pf); O = Port(a['obs_port'], of)

    # (2) command-gated connection. FRESH-BOOT gate: record a pre-reset host
    # timestamp and require BOTH endpoints to emit Q2READY AFTER it, so a stale
    # buffered Q2READY from a prior boot can never satisfy the gate. Reset return
    # codes are checked (a failed reset is not silently accepted).
    print('[setup] deterministic command-gated startup')
    t_boot = hostms()
    if reset(a['periph_devid']) != 0 or reset(a['central_devid']) != 0:
        die('endpoint reset command failed', [C,P,O])
    if not (wait_for(cf,'Q2READY role=C',15, after=t_boot) and
            wait_for(pf,'Q2READY role=P',15, after=t_boot)):
        die('endpoints did not reach fresh Q2READY (post-reset)', [C,P,O])
    P.send(b'A')
    if not wait_for(pf,'ADV-READY',10): die('peripheral did not ADV-READY', [C,P,O])
    C.send(b'C')
    if not (wait_for(cf,'CENTRAL connected',15) and wait_for(cf,'Q2CONN',5)):
        die('central did not connect / no Q2CONN', [C,P,O])
    conns = re.findall(r'Q2CONN AA=([0-9a-fA-F]+) CRCINIT=([0-9a-fA-F]+).*map=([0-9a-fA-F]+)', open(cf).read())
    if len(conns) != 1: die(f'expected 1 Q2CONN, got {len(conns)}', [C,P,O])
    aa, crc, mp = conns[0]; ch = 10
    print(f'[setup] connection AA=0x{aa} CRCInit=0x{crc} map={mp} ch{ch}')

    # (3) runtime observer config: reset the observer so it boots FRESH to
    # CONFIG-READY (it may have finished a prior run); wait for the CONFIG-READY
    # whose host-timestamp is AFTER the reset (ignore any stale one).
    t_or = hostms()
    if reset(a['obs_devid']) != 0: die('observer reset command failed', [C,P,O])
    if not wait_for(of,'CONFIG-READY',15, after=t_or): die('observer not CONFIG-READY', [C,P,O])
    O.send_slow(f'CFG aa=0x{aa} crc=0x{crc} ch={ch} phy=1\n'.encode())
    if not wait_for(of,'CONFIG-ECHO',8, after=t_or): die('observer did not echo config', [C,P,O])
    echo = [l for l in open(of) if 'CONFIG-ECHO' in l][-1]
    if f'aa=0x{int(aa,16):08x}' not in echo.lower() or f'ch={ch}' not in echo:
        die(f'observer CONFIG-ECHO mismatch: {echo.strip()}', [C,P,O])
    if not wait_for(of,'ARMED',8, after=t_or): die('observer did not ARM', [C,P,O])
    print('[run] observer armed; START snaps -> GO -> capture -> END snaps')

    steady = a['q3'] and a['q3_steady']
    # (4) handshake. rev-6 STEADY: the peripheral's ~5 s auto conn-param update resets the
    # negotiated frame space, so the whole pre-window sequence is EVENT-GATED on it:
    #   update -> (f100: FSU + both completions) -> frozen settle -> START snaps -> GO.
    # START snaps move AFTER all pre-window activity (they used to precede the FSU/settle,
    # which blew the snapshot-slop bracket in failed block b1). Mid-step/Q2 keep the old
    # order (START snaps -> GO; mid-step F fires after CAPTURE-START).
    if steady:
        pu_ok, pu_reason, pu = wait_paramupd(cf, pf, aa)
        a['q3_paramupd_reason'] = pu_reason
        a['q3_final_params'] = pu
        if not pu_ok:
            print(f'[q3-steady] param-update gate INCOMPLETE ({pu_reason}) -> capture finishes, QUARANTINED')
        else:
            print(f'[q3-steady] conn-param update confirmed on both endpoints: {pu} (final interval frozen)')
        if pu_ok and a['q3_arm'] == 'f100':
            t_f = hostms(); C.send(b'F')
            cp = None
            if not wait_for(cf,'Q3FSU-REQ',5, after=t_f):
                cp = 'F not acknowledged (no Q3FSU-REQ)'
            elif not wait_for(cf,'Q3FSU-DONE',10, after=t_f):
                cp = 'no central Q3FSU-DONE'
            else:
                v, r = q3_completeness(open(cf).read())
                if v != 'complete': cp = f'central: {r}'
            if cp is None and not wait_for(pf,'Q3FSU-DONE role=P',10, after=t_f):
                cp = 'no peripheral Q3FSU-DONE (peer participation unproven)'
            if cp is None:
                v, r = q3_peer_participation(open(cf).read(), open(pf).read())
                if v != 'complete': cp = f'peer: {r}'
            a['q3_cp_reason'] = cp
        settle_s = Q3_STEADY_SETTLE_MS_DEFAULT / 1000.0 + STEADY_SETTLE_PAD_S
        kind = 'post-FSU' if a['q3_arm'] == 'f100' else 'sham (symmetry)'
        print(f'[q3-steady] {a["q3_arm"]}: settling {settle_s:.1f}s ({kind}) then START snaps -> GO')
        time.sleep(settle_s)
    # START snaps -- for steady, only NOW (after update/FSU/settle); for mid-step/Q2, here.
    C.send(b'S'); P.send(b'S')
    if not (wait_for(cf,'Q2SNAP role=C seq=0',5) and wait_for(pf,'Q2SNAP role=P seq=0',5)):
        die('START snapshots not acknowledged', [C,P,O])
    O.send(b'G')
    # (4b) Q3 mid-capture FSU trigger. Dwell from the observer's CAPTURE-START (NOT a
    # timer off G), send EXACTLY ONE 'F', require REQ + central & peripheral DONE.
    # SCIENTIFIC INCOMPLETENESS (missing/mismatched control-plane records) is RECORDED
    # and the capture is ALLOWED TO FINISH -> archived QUARANTINED
    # q3-control-plane-incomplete (preserving the observer data). die() is reserved
    # for capture/instrument failures that make continued capture impossible (no
    # CAPTURE-START). Q2 (a['q3'] false) does none of this.
    if a['q3']:
        if not wait_for(of,'CAPTURE-START',20, after=t_or):
            die('no CAPTURE-START (instrument failure -- capture never started)', [C,P,O])
        t_cs = hostms()
        P.send(b'K')   # clear the periph on-chip histogram right after CAPTURE-START
        if not wait_for(pf,'TIFS-CLEARED role=P',5, after=t_cs):   # MANDATORY for instrument completeness
            a['q3_onchip_reason'] = 'no TIFS-CLEARED (clear not acknowledged)'
        if steady:
            # SINGLE plateau: no baseline dwell, no phase snapshots, no mid-step F.
            # f150 must show NO FSU activity of any kind (the analyzer re-asserts this).
            if a['q3_arm'] == 'f150':
                txt = open(cf).read() + open(pf).read()
                if any(tok in txt for tok in ('Q3FSU-REQ', 'Q3FSU-REJECT', 'Q3FSU-DONE')):
                    a['q3_cp_reason'] = 'f150 steady: unexpected FSU activity'
            print(f'[q3-steady] {a["q3_arm"]} single-plateau capture (no mid-step trigger)')
        else:
            print(f"[q3] CAPTURE-START seen; on-chip cleared; baseline dwell {a['baseline_dwell_s']}s then one F")
            time.sleep(a['baseline_dwell_s'])
            # F-time PHASE snapshot on BOTH endpoints (splits the ch10 TX denominator into
            # pre/post for phase-SPECIFIC retention), THEN the single FSU trigger.
            t_m = hostms(); C.send(b'M'); P.send(b'M')
            if not (wait_for(cf,'Q3PHASESNAP role=C',5, after=t_m) and
                    wait_for(pf,'Q3PHASESNAP role=P',5, after=t_m)):
                a['q3_onchip_reason'] = a['q3_onchip_reason'] or 'no Q3PHASESNAP (phase snapshot missing)'
            t_f = hostms(); C.send(b'F')
            cp = None   # first control-plane incompleteness reason (None => complete)
            if not wait_for(cf,'Q3FSU-REQ',5, after=t_f):
                cp = 'F not acknowledged (no Q3FSU-REQ)'
            elif not wait_for(cf,'Q3FSU-DONE',10, after=t_f):
                cp = 'no central Q3FSU-DONE'
            else:
                v, r = q3_completeness(open(cf).read())
                if v != 'complete': cp = f'central: {r}'
            if cp is None and not wait_for(pf,'Q3FSU-DONE role=P',10, after=t_f):
                cp = 'no peripheral Q3FSU-DONE (peer participation unproven)'
            if cp is None:
                v, r = q3_peer_participation(open(cf).read(), open(pf).read())
                if v != 'complete': cp = f'peer: {r}'
            a['q3_cp_reason'] = cp
            print(f'[q3] control plane INCOMPLETE ({cp}) -> capture finishes, cell QUARANTINED'
                  if cp else '[q3] central + peripheral FSU completions recorded (same AA/session)')
    if not wait_for(of,'CAPTURE-END',45, after=t_or): die('no CAPTURE-END', [C,P,O])  # >30s capture window
    if a['q3']:
        # FREEZE the on-chip histogram IMMEDIATELY at CAPTURE-END so the on-chip
        # window ~= the observer window within the reported/gated boundary slops
        # (clear lands shortly after START, freeze shortly after END; drain, which is
        # slow, happens after the snaps).
        t_fz = hostms(); P.send(b'D')
        if not wait_for(pf,'TIFS-FROZEN role=P',5, after=t_fz):
            a['q3_onchip_reason'] = a['q3_onchip_reason'] or 'no TIFS-FROZEN (freeze not acknowledged)'
    # END snaps immediately after CAPTURE-END (no artificial delay). The observer
    # window is already closed (Te); the analyzer independently verifies END_snap
    # host-ts >= Te and gates the resulting slop, so tightness -- not a fixed pad
    # -- is what keeps the denominator a tight conservative bracket.
    C.send(b'S'); P.send(b'S')
    if not (wait_for(cf,'Q2SNAP role=C seq=1',5) and wait_for(pf,'Q2SNAP role=P seq=1',5)):
        die('END snapshots not acknowledged', [C,P,O])
    if a['q3']:
        # DRAIN the frozen bins AFTER the END snaps; validate the full lifecycle +
        # bin structure. On-chip failures are a SEPARATE leg (q3-onchip-incomplete).
        t_dr = hostms(); P.send(b'R')
        if not wait_for(pf,'TIFS-DRAIN-DONE role=P',8, after=t_dr):
            a['q3_onchip_reason'] = a['q3_onchip_reason'] or 'no TIFS-DRAIN-DONE (drain missing)'
        else:
            ctext, ptext, otext = open(cf).read(), open(pf).read(), open(of).read()
            if steady:
                # SINGLE on-chip bin: f150 -> [150] bound to the connection snapshot
                # session; f100 -> [100] bound to the FSU REQUEST session.
                if a['q3_arm'] == 'f150':
                    m = re.search(r'Q2SNAP role=C seq=0 .*?sess=(\d+)', ctext)
                    req_sess = int(m.group(1)) if m else None
                    expect = [150]
                else:
                    m = re.search(r'Q3FSU-REQ .*?sess=(\d+)', ctext)
                    req_sess = int(m.group(1)) if m else None
                    expect = [100]
            else:
                m = re.search(r'Q3FSU-REQ .*?sess=(\d+).*?min=(\d+)', ctext)
                req_sess = int(m.group(1)) if m else None
                expect = [150, int(m.group(2))] if m else None   # mid-step: pre (150) + post (requested)
            v, r = q3_onchip_check(ptext, int(aa, 16), req_sess, expect)
            if v != 'complete': a['q3_onchip_reason'] = a['q3_onchip_reason'] or r
            bv, br, slops = q3_boundary_slops(otext, ptext)
            a['q3_boundary_slops'] = slops
            if bv != 'complete': a['q3_onchip_reason'] = a['q3_onchip_reason'] or br
            if a['q3_onchip_reason'] is None:
                bins_desc = ({'f150': '150', 'f100': '100'}[a['q3_arm']] if steady else '150/100')
                print(f'[q3] on-chip OK (one lifecycle; drop=0; {bins_desc}@1M bin(s); boundary slops {slops})')
    ok = wait_for(of,'Q2-DONE',50, after=t_or)  # ~1230 records dump over UART takes ~12s
    time.sleep(1); [x.close() for x in (C,P,O)]
    if not ok: print('no Q2-DONE'); sys.exit(2)
    # rev-6 steady: NO LATER param update may occur before/during the window -- require
    # STILL exactly one Q3PARAMUPD per endpoint (a second would silently reset FSU mid-run).
    if steady and a['q3_paramupd_reason'] is None:
        nc, npp = len(_paramupd_rows(open(cf).read(),'C')), len(_paramupd_rows(open(pf).read(),'P'))
        if nc != 1 or npp != 1:
            a['q3_paramupd_reason'] = f'a later param-update occurred (C={nc} P={npp} total) -- FSU may have reset mid-run'
            print(f'[q3-steady] {a["q3_paramupd_reason"]}')

    # (5) analyze + ARCHIVE the cell's evidence (so an accepted cell is
    # reproducible: analyzer stdout+verdict, runner args, resolved connection
    # config, source commit, and firmware/log hashes).
    here = os.path.dirname(__file__)
    if a['q3'] and a['q3_steady']:
        # STEADY-STATE (ABBA arm): the single-plateau analyzer, arm-registered. No
        # guard/min-plateau/deviation knobs (one plateau, no transition window).
        cmd = [sys.executable, os.path.join(here,'analyze_q3.py'), of, cf, pf, '--steady', a['q3_arm']]
        if a['calib']:
            cmd += ['--calib', a['calib']]
    elif a['q3']:
        # Q3 uses the DISTINCT step analyzer (obs+central+periph -> on-air 800t step).
        # It REUSES every Q2 gate + a validated --calib baseline + the strict control-
        # plane/on-chip validators. Post-promotion its exit IS authoritative: a
        # registered mode with every leg passing on a clean tree ACCEPTS (decide_verdict);
        # a fail REJECTs. guard/min-plateau are passed + recorded (manifest args).
        cmd = [sys.executable, os.path.join(here,'analyze_q3.py'), of, cf, pf,
               '--guard-ticks', str(a['q3_guard_ticks']), '--min-plateau', str(a['q3_min_plateau'])]
        if a['calib']:
            cmd += ['--calib', a['calib']]
        if a['q3_contract_deviation']:
            cmd += ['--contract-deviation', a['q3_contract_deviation']]
    else:
        cmd = [sys.executable, os.path.join(here,'analyze_q2.py'),
               of, cf, pf, '--mode', a['mode'], '--near', a['near']]
        if a['calib']:                  # provenance-bound frozen ref (near/far REQUIRES this)
            cmd += ['--calib', a['calib']]
    print('[analyze]', ' '.join(cmd[2:]))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    analysis = proc.stdout + (('\n[stderr]\n'+proc.stderr) if proc.stderr else '')
    print(analysis, end='')
    # RESOLVE + VALIDATE provenance BEFORE deciding the verdict: the observer image
    # (HEX + ELF + .config) and both endpoint builds (all three each) must hash to real
    # 64-hex digests (not "MISSING"). The ELF pins the executable; .config pins the build.
    obs_bd_dir = os.path.dirname(os.path.dirname(a['obs_hex_path']))
    obs_hash = _sha256(a['obs_hex_path']) if not a['skip_obs_flash'] else 'UNVERIFIED(--skip-obs-flash)'
    obs_elf_hash = _sha256(os.path.join(obs_bd_dir, 'zephyr', 'zephyr.elf')) if not a['skip_obs_flash'] else 'UNVERIFIED'
    obs_cfg_hash = _sha256(os.path.join(obs_bd_dir, 'zephyr', '.config')) if not a['skip_obs_flash'] else 'UNVERIFIED'
    obs_verified = ((not a['skip_obs_flash']) and valid_digest(obs_hash)
                    and valid_digest(obs_elf_hash) and valid_digest(obs_cfg_hash))
    cbh = build_hashes(a['central_build']); pbh = build_hashes(a['periph_build'])
    builds_valid = build_ok(cbh) and build_ok(pbh)
    # firmware archival is an ACCEPTANCE PREREQUISITE (--q3): the pre-flash snapshot was
    # complete + valid + assert-passing, and the flashed build dirs are byte-identical
    # to it post-run (no mid-run source mutation). Q2 has no snapshot -> vacuously true.
    if a['q3']:
        fw_valid, a['fw_toctou_reason'] = firmware_unchanged(a)
    else:
        fw_valid, a['fw_toctou_reason'] = True, ''
    verdict, final_rc, qreason = decide_verdict(proc.returncode, obs_verified, builds_valid,
                                                a['smoke'], a['q3'], a['q3_cp_reason'],
                                                a['q3_onchip_reason'], a['q3_fsu_reason'],
                                                pre_run_dirty=a['pre_run_dirty'],
                                                q3_contract_deviation=a.get('q3_contract_deviation'),
                                                q3_steady=a.get('q3_steady', False),
                                                q3_arm=a.get('q3_arm', 'f100'),
                                                firmware_archive_valid=fw_valid,
                                                q3_paramupd_reason=a.get('q3_paramupd_reason'))
    q3_detail = '; '.join(f'{k}={v}' for k, v in
                          (('q3_cp', a['q3_cp_reason']), ('q3_onchip', a['q3_onchip_reason'])) if v)
    with open(os.path.join(a['outdir'],'analysis.txt'),'w') as fo:
        fo.write(analysis + f'\n=== analyzer exit {proc.returncode}; cell verdict {verdict} '
                 f'(obs_image_verified={obs_verified} endpoint_builds_valid={builds_valid} '
                 f'quarantine_reason={qreason or "-"}'
                 f"{'; '+q3_detail if q3_detail else ''}) ===\n")
    archive(a, of, cf, pf, aa, crc, mp, ch, verdict, proc.returncode, final_rc, qreason,
            obs_hash, cbh, pbh)
    sys.exit(final_rc)

def q3_fsu_config_gate(central_cfg, periph_cfg, central_arm):
    """Run assert_fsu_config.py on BOTH endpoint .config files for a --q3 run: the
    central at its request arm (f100/f150), the peripheral as the responder. Returns
    (ok, reason, text, script_sha256). Any failure -> 'q3-fsu-config-invalid'. A
    missing .config fails the assertion (exit != 0), so it is caught here too."""
    script = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                           '..', 'zephyr-patches', 'fsu-m0-series', 'assert_fsu_config.py'))
    sha = _sha256(script)
    lines, ok = [], True
    for cfg, arm in ((central_cfg, central_arm), (periph_cfg, 'periph')):
        p = subprocess.run([sys.executable, script, cfg or '<none>', '--arm', arm],
                           capture_output=True, text=True)
        lines.append((p.stdout + p.stderr).strip())
        if p.returncode != 0:
            ok = False
    return ok, (None if ok else 'q3-fsu-config-invalid'), '\n'.join(lines), sha

# the ONLY Q3 modes the promotion accepts: mid-step is f100-only; steady is f150/f100.
def _q3_registered(arm, steady):
    return (arm in ('f150', 'f100')) if steady else (arm == 'f100')

def abba_binding_ok(campaign_id, seq, arm):
    """the accepting steady path MUST be bound to the ABBA campaign: nonempty campaign
    id, a seq in range, and the arm the frozen A/B/B/A sequence demands at that seq.
    Returns (ok, reason)."""
    import analyze_q3 as _q3seq
    seqmax = len(_q3seq.Q3_ABBA_ARM_SEQ)
    if not campaign_id or seq is None:
        return False, 'requires BOTH --abba-campaign-id and --abba-seq'
    if not (0 <= seq < seqmax):
        return False, f'--abba-seq must be in 0..{seqmax - 1}'
    want = _q3seq.Q3_ABBA_ARM_SEQ[seq]
    if arm != want:
        return False, f'--abba-seq {seq} requires --q3-arm {want} (got {arm})'
    return True, ''

def steady_knob_override(steady, guard_ticks, min_plateau, deviation):
    """True iff a steady run overrides guard/min-plateau (which the steady analyzer
    IGNORES) WITHOUT declaring a deviation -- a silent accepted-contract mutation the
    runner must refuse before any hardware mutation."""
    return bool(steady) and not deviation and (
        guard_ticks != Q3_GUARD_TICKS_DEFAULT or min_plateau != Q3_MIN_PLATEAU_DEFAULT)

def decide_verdict(analyzer_rc, obs_verified, builds_present, smoke=False, q3=False,
                   q3_cp_reason=None, q3_onchip_reason=None, q3_fsu_reason=None,
                   pre_run_dirty=False, q3_contract_deviation=None, q3_steady=False,
                   q3_arm='f100', firmware_archive_valid=True, q3_paramupd_reason=None):
    """Returns (verdict, exit_code, quarantine_reason). A cell is ACCEPT only if the
    analyzer passed AND the observer image is verified (flashed this run) AND exact
    endpoint firmware/config provenance is bound AND the source tree was CLEAN pre-run.

    PROMOTED: a Q3 run in a REGISTERED mode (mid-step f100 or steady f150/f100) now
    ACCEPTS when EVERY leg passes -- analyzer METRICS-OK/STEADY-METRICS-OK, both the
    control-plane and on-chip legs complete, verified images + endpoint provenance,
    no contract deviation, a CLEAN pre-run tree (HARD gate), not --smoke, and a
    registered arm. Anything short of that stays QUARANTINED (naming the failed leg)
    or REJECTs (analyzer fail). --smoke ALWAYS forced-quarantines; a dirty tree ALWAYS
    quarantines; a contract deviation or an unregistered/wrong arm can never ACCEPT."""
    if q3:
        # named-leg incompleteness / forced quarantine first (these preempt an ACCEPT)
        if q3_fsu_reason:                       # wrong firmware is the most fundamental leg
            return 'QUARANTINED', 3, q3_fsu_reason
        if smoke:
            return 'QUARANTINED', 3, 'integration-smoke'
        if q3_paramupd_reason:                  # rev-6 steady: conn-param-update event-gate
            return 'QUARANTINED', 3, 'q3-paramupd-incomplete'
        if q3_cp_reason:
            return 'QUARANTINED', 3, 'q3-control-plane-incomplete'
        if q3_onchip_reason:
            return 'QUARANTINED', 3, 'q3-onchip-incomplete'
        if q3_contract_deviation:
            return 'QUARANTINED', 3, 'q3-contract-deviation'
        if not _q3_registered(q3_arm, q3_steady):
            return 'QUARANTINED', 3, 'q3-unregistered-arm'
        if analyzer_rc != 0:
            return 'REJECT', analyzer_rc, 'analyzer-fail'
        if not obs_verified:
            return 'QUARANTINED', 3, 'observer-image-unverified'
        if not builds_present:
            return 'QUARANTINED', 3, 'endpoint-build-provenance-missing'
        if not firmware_archive_valid:          # snapshot incomplete/invalid or mutated mid-run
            return 'QUARANTINED', 3, 'firmware-archive-invalid'
        if pre_run_dirty:                       # HARD gate: never accept a dirty-tree cell
            return 'QUARANTINED', 3, 'pre-run-dirty-tree'
        return 'ACCEPT', 0, ''                  # all legs pass -> accepted registered Q3 cell
    if analyzer_rc != 0:
        return 'REJECT', analyzer_rc, 'analyzer-fail'
    if not obs_verified:
        return 'QUARANTINED', 3, 'observer-image-unverified'
    if not builds_present:
        return 'QUARANTINED', 3, 'endpoint-build-provenance-missing'
    if smoke:
        return 'QUARANTINED', 3, 'integration-smoke'
    return 'ACCEPT', 0, ''

def _sha256(path):
    import hashlib
    try:
        h = hashlib.sha256()
        with open(path,'rb') as f:
            for b in iter(lambda: f.read(65536), b''): h.update(b)
        return h.hexdigest()
    except OSError:
        return 'MISSING'

def valid_digest(h):
    return bool(re.fullmatch(r'[0-9a-f]{64}', h or ''))

def build_hashes(bd):
    """resolve an endpoint build dir to real hashes of its zephyr.hex + zephyr.elf +
    .config (each 'MISSING' if the file is absent/unreadable). The ELF identifies the
    changed executable -- an unchanged .config does NOT prove executable identity."""
    if not bd:
        return {}
    return {'zephyr.hex': _sha256(os.path.join(bd,'zephyr','zephyr.hex')),
            'zephyr.elf': _sha256(os.path.join(bd,'zephyr','zephyr.elf')),
            '.config':    _sha256(os.path.join(bd,'zephyr','.config'))}

def build_ok(bh):
    """endpoint provenance is bound only if ALL THREE artifacts hash to real 64-hex
    digests -- HEX, ELF (identifies the changed executable), and .config. A nonexistent
    dir or missing file (-> 'MISSING') does NOT count."""
    return (valid_digest(bh.get('zephyr.hex')) and valid_digest(bh.get('zephyr.elf'))
            and valid_digest(bh.get('.config')))

def _fw_plan(central_bd, periph_bd, obs_bd, skip_obs):
    plan = [('central', central_bd), ('periph', periph_bd)]
    if not skip_obs:
        plan.append(('observer', obs_bd))
    return plan

def snapshot_firmware(outdir, obs_bd, central_bd, periph_bd, skip_obs, arm):
    """BEFORE flashing: copy HEX/ELF/.config for all three devices INTO the cell
    (firmware/<dev>.<ext>) and VALIDATE the snapshot -- require completeness, valid
    digests, and a PASSING assert_fsu_config over the archived central (@arm) + periph
    .config. This is time-of-check; the flash then reads the SAME build dirs and archive()
    re-verifies they are byte-identical (time-of-use). Returns (ok, reason, {name: hash})."""
    import shutil
    fwd = os.path.join(outdir, 'firmware'); os.makedirs(fwd, exist_ok=True)
    snap = {}
    for dev, bd in _fw_plan(central_bd, periph_bd, obs_bd, skip_obs):
        if not bd:
            return False, f'{dev} build dir not provided (no firmware to archive)', snap
        for ext, fn in (('hex', 'zephyr.hex'), ('elf', 'zephyr.elf'), ('config', '.config')):
            src = os.path.join(bd, 'zephyr', fn)
            if not os.path.exists(src):
                return False, f'{dev} build dir missing {fn} (incomplete firmware)', snap
            dst = os.path.join(fwd, f'{dev}.{ext}')
            shutil.copy2(src, dst)
            h = _sha256(dst)
            if not valid_digest(h):
                return False, f'{dev}.{ext} did not hash to a valid digest', snap
            snap[f'{dev}.{ext}'] = h
    script = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                           '..', 'zephyr-patches', 'fsu-m0-series', 'assert_fsu_config.py'))
    for cfg, aarm in (('central.config', arm), ('periph.config', 'periph')):
        r = subprocess.run([sys.executable, script, os.path.join(fwd, cfg), '--arm', aarm],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return False, f'assert_fsu_config FAILED on archived {cfg} (arm={aarm})', snap
    return True, '', snap

def firmware_unchanged(a):
    """POST-run: the flashed build dirs must STILL hash to the pre-flash snapshot -- no
    source mutation during the hardware run (TOCTOU). Returns (ok, reason)."""
    if not a.get('fw_snapshot_ok'):
        return False, a.get('fw_snapshot_reason') or 'no valid pre-flash firmware snapshot'
    snap = a.get('fw_snapshot') or {}
    obs_bd = os.path.dirname(os.path.dirname(a['obs_hex_path'])) if a.get('obs_hex_path') else ''
    for dev, bd in _fw_plan(a['central_build'], a['periph_build'], obs_bd, a['skip_obs_flash']):
        for ext, fn in (('hex', 'zephyr.hex'), ('elf', 'zephyr.elf'), ('config', '.config')):
            cur = _sha256(os.path.join(bd, 'zephyr', fn)) if bd else 'MISSING'
            if snap.get(f'{dev}.{ext}') != cur:
                return False, f'{dev}.{ext} changed between flash and archive (source mutated mid-run)'
    return True, ''

def archive(a, of, cf, pf, aa, crc, mp, ch, verdict, analyzer_rc, cell_rc, quarantine_reason, obs_hash, cbh, pbh):
    import json
    here = os.path.dirname(__file__)
    commit = subprocess.run(['git','rev-parse','HEAD'], capture_output=True, text=True,
                            cwd=here).stdout.strip() or 'UNKNOWN'
    proto = os.path.join(here, '..', 'debug-evidence', 'observer-q1-20260810', 'Q2-ACCEPTANCE-PROTOCOL.md')
    man = {
        'verdict': verdict, 'analyzer_exit': analyzer_rc, 'cell_exit': cell_rc,
        'quarantine_reason': quarantine_reason,
        'args': a,
        'connection': {'AA': f'0x{aa}', 'CRCInit': f'0x{crc}', 'map': mp, 'ch': ch, 'phy': '1M'},
        'source_commit': commit, 'tree_dirty': a['pre_run_dirty'], 'board': a['board'],
        'devids': {'obs': a['obs_devid'], 'central': a['central_devid'], 'periph': a['periph_devid']},
        'observer_image_verified': (not a['skip_obs_flash']) and valid_digest(obs_hash),
        'sha256': {
            'observer.hex': obs_hash,
            'observer.elf': (_sha256(os.path.join(os.path.dirname(os.path.dirname(a['obs_hex_path'])),'zephyr','zephyr.elf'))
                             if not a['skip_obs_flash'] else 'UNVERIFIED(--skip-obs-flash)'),
            'observer.config': (_sha256(os.path.join(os.path.dirname(os.path.dirname(a['obs_hex_path'])),'zephyr','.config'))
                                if not a['skip_obs_flash'] else 'UNVERIFIED(--skip-obs-flash)'),
            'obs.txt': _sha256(of), 'central.txt': _sha256(cf), 'periph.txt': _sha256(pf),
            'analysis.txt': _sha256(os.path.join(a['outdir'],'analysis.txt')),
            'analyze_q2.py': _sha256(os.path.join(here,'analyze_q2.py')),
            'analyze_q3.py': _sha256(os.path.join(here,'analyze_q3.py')),
            'q2_run.py': _sha256(os.path.join(here,'q2_run.py')),
            'combine_calib.py': _sha256(os.path.join(here,'combine_calib.py')),
            'Q2-ACCEPTANCE-PROTOCOL.md': _sha256(proto),
        },
        'central_build': cbh,
        'periph_build': pbh,
        'calibration_artifact': _sha256(a['calib']) if a['calib'] else '',
    }
    if a.get('q3'):
        # firmware was already archived + validated PRE-FLASH (firmware/<dev>.<ext>);
        # record the snapshot + whether it survived the TOCTOU re-verification. The ABBA
        # combiner independently rehashes those archived binaries (defense-in-depth).
        man['firmware_archived'] = sorted((a.get('fw_snapshot') or {}).keys())
        man['firmware_archive_valid'] = bool(a.get('fw_snapshot_ok')) and not a.get('fw_toctou_reason')
        man['firmware_snapshot'] = a.get('fw_snapshot') or {}
        # bind the Q3 acceptance protocol + the EXACT structured contract this run was
        # analyzed under (analyzer/runner/calib hashes are already in sha256 above).
        import analyze_q3 as _Q3
        q3proto = os.path.join(here, '..', 'debug-evidence', 'observer-q3-20260811',
                               'Q3-ACCEPTANCE-PROTOCOL.md')
        man['sha256']['Q3-ACCEPTANCE-PROTOCOL.md'] = _sha256(q3proto)
        man['q3_contract'] = _Q3.q3_contract(a.get('q3_guard_ticks', Q3_GUARD_TICKS_DEFAULT),
                                             a.get('q3_min_plateau', Q3_MIN_PLATEAU_DEFAULT))
        man['q3_contract_deviation'] = a.get('q3_contract_deviation') or ''
        # endpoint FSU-config assertion result + the script that produced it
        man['q3_arm'] = a.get('q3_arm')
        man['q3_fsu_config_assert'] = a.get('q3_fsu_assert')
        man['sha256']['assert_fsu_config.py'] = a.get('q3_assert_sha')
        # STEADY-STATE (ABBA) binding: the combiner requires abba_campaign_id + abba_seq
        # to prove the four cells belong to ONE block in the frozen A/B/B/A order.
        man['q3_steady'] = bool(a.get('q3_steady'))
        if a.get('q3_steady'):
            man['sha256']['combine_abba.py'] = _sha256(os.path.join(here, 'combine_abba.py'))
            # rev-6: the frozen final connection parameters (post auto param-update) that the
            # steady arm gated on; combine_abba requires these identical across all 4 cells.
            man['q3_final_params'] = a.get('q3_final_params') or {}
            man['q3_paramupd_reason'] = a.get('q3_paramupd_reason') or ''
            # absolute monotonic collection interval (proves strict, non-overlapping order)
            man['abba_start_ns'] = a.get('run_start_ns')
            man['abba_end_ns'] = time.monotonic_ns()
            if a.get('abba_campaign_id'):
                man['abba_campaign_id'] = a['abba_campaign_id']
            if a.get('abba_seq') is not None:
                man['abba_seq'] = a['abba_seq']
    with open(os.path.join(a['outdir'],'manifest.json'),'w') as f:
        json.dump(man, f, indent=2); f.write('\n')
    warn = '' if verdict == 'ACCEPT' else f'  [WARN] verdict {verdict} -> the combiner will reject this cell'
    print(f'[archive] verdict={verdict}; wrote analysis.txt + manifest.json to {a["outdir"]}{chr(10)+warn if warn else ""}')

def selftest():
    """offline checks of the enforcement + auditability logic (no hardware)."""
    import tempfile, json
    R = []
    # verdict decision table (ACCEPT needs analyzer-pass + verified image + builds)
    R.append(('accept when pass + verified + builds', decide_verdict(0, True, True) == ('ACCEPT', 0, '')))
    R.append(('QUARANTINE when pass but unverified image',
              decide_verdict(0, False, True) == ('QUARANTINED', 3, 'observer-image-unverified')))
    R.append(('QUARANTINE when pass but endpoint builds missing',
              decide_verdict(0, True, False) == ('QUARANTINED', 3, 'endpoint-build-provenance-missing')))
    R.append(('reject propagates analyzer failure (even w/ provenance)',
              decide_verdict(3, True, True) == ('REJECT', 3, 'analyzer-fail')))
    # --smoke QUARANTINES even with a passing analyzer + COMPLETE provenance
    R.append(('smoke QUARANTINES despite pass + full provenance',
              decide_verdict(0, True, True, smoke=True) == ('QUARANTINED', 3, 'integration-smoke')))
    # --- endpoint build provenance is RESOLVED, not just present-as-a-string ---
    import shutil
    R.append(('nonexistent build dir -> not valid', build_ok(build_hashes('/does/not/exist')) is False))
    R.append(('empty build path -> not valid', build_ok(build_hashes('')) is False))
    bd = tempfile.mkdtemp(); os.makedirs(os.path.join(bd, 'zephyr'))
    open(os.path.join(bd, 'zephyr', 'zephyr.hex'), 'w').write('HEXDATA\n')
    R.append(('build dir missing .config -> not valid (only one artifact)',
              build_ok(build_hashes(bd)) is False))
    open(os.path.join(bd, 'zephyr', '.config'), 'w').write('CONFIG_X=y\n')
    open(os.path.join(bd, 'zephyr', 'zephyr.elf'), 'w').write('ELFDATA\n')
    R.append(('build dir with hex + .config -> valid 64-hex digests',
              build_ok(build_hashes(bd)) and valid_digest(build_hashes(bd)['zephyr.hex'])))
    R.append(('build_hashes records the ELF (identity of the changed executable)',
              valid_digest(build_hashes(bd).get('zephyr.elf'))))
    R.append(("'MISSING' is never a valid digest", valid_digest('MISSING') is False))
    shutil.rmtree(bd, ignore_errors=True)
    # archive() records PRE-RUN dirty + verdict + BOTH exits, and the resolved
    # hashes it was handed (never re-deriving them, so validity can't drift).
    d = tempfile.mkdtemp()
    for n in ('obs.txt', 'central.txt', 'periph.txt', 'analysis.txt'):
        open(os.path.join(d, n), 'w').write('x\n')
    a = dict(outdir=d, board='b', obs_devid='o', central_devid='c', periph_devid='p',
             skip_obs_flash=True, central_build='', periph_build='', calib='',
             pre_run_dirty=False)   # sentinel: tree was CLEAN before the run
    # analyzer PASSED (0) but cell QUARANTINED (3): both must be recorded distinctly
    archive(a, *(os.path.join(d, n) for n in ('obs.txt','central.txt','periph.txt')),
            'aa', 'crc', 'map', 10, 'QUARANTINED', 0, 3, 'integration-smoke',
            'UNVERIFIED(--skip-obs-flash)', {}, {})
    m = json.load(open(os.path.join(d, 'manifest.json')))
    R.append(('manifest tree_dirty echoes PRE-RUN state (not post-log-write)',
              m['tree_dirty'] is False))
    R.append(('manifest verdict echoes decided verdict', m['verdict'] == 'QUARANTINED'))
    R.append(('manifest records quarantine_reason', m['quarantine_reason'] == 'integration-smoke'))
    R.append(('manifest records analyzer_exit (0) and cell_exit (3) distinctly',
              m['analyzer_exit'] == 0 and m['cell_exit'] == 3))
    R.append(('manifest observer hash UNVERIFIED under --skip-obs-flash',
              'UNVERIFIED' in m['sha256']['observer.hex']))
    R.append(('manifest records observer.elf + observer.config keys (provenance of the executable)',
              'observer.elf' in m['sha256'] and 'observer.config' in m['sha256']))
    R.append(('manifest records observer_image_verified=false', m['observer_image_verified'] is False))
    shutil.rmtree(d, ignore_errors=True)
    # a Q3 run's manifest binds the Q3 protocol + the EXACT structured contract.
    d2 = tempfile.mkdtemp()
    for n in ('obs.txt', 'central.txt', 'periph.txt', 'analysis.txt'):
        open(os.path.join(d2, n), 'w').write('x\n')
    a2 = dict(outdir=d2, board='b', obs_devid='o', central_devid='c', periph_devid='p',
              skip_obs_flash=True, central_build='', periph_build='', calib='',
              pre_run_dirty=False, q3=True, q3_guard_ticks=Q3_GUARD_TICKS_DEFAULT,
              q3_min_plateau=Q3_MIN_PLATEAU_DEFAULT, q3_contract_deviation=None,
              q3_arm='f100', q3_fsu_assert='FSU CONFIG ASSERT: PASS (arm=f100)\nPASS (periph)',
              q3_assert_sha='ab'*32)
    archive(a2, *(os.path.join(d2, n) for n in ('obs.txt','central.txt','periph.txt')),
            'aa', 'crc', 'map', 10, 'QUARANTINED', 0, 3, 'q3-preaccept-smoke',
            'UNVERIFIED(--skip-obs-flash)', {}, {})
    m2 = json.load(open(os.path.join(d2, 'manifest.json')))
    R.append(('Q3 manifest binds the structured q3_contract (immutable step_tol=4)',
              m2.get('q3_contract', {}).get('step_tol') == 4 and
              m2['q3_contract']['guard_ticks'] == Q3_GUARD_TICKS_DEFAULT))
    R.append(('Q3 manifest hashes the Q3 acceptance protocol',
              valid_digest(m2['sha256'].get('Q3-ACCEPTANCE-PROTOCOL.md'))))
    R.append(('Q3 manifest records the FSU-config assertion + script hash + arm',
              m2.get('q3_arm') == 'f100' and 'PASS' in (m2.get('q3_fsu_config_assert') or '')
              and m2['sha256'].get('assert_fsu_config.py') == 'ab'*32))
    shutil.rmtree(d2, ignore_errors=True)
    # runner-enforced FSU config gate (arm-aware) against the ARCHIVED provenance configs
    prov = os.path.join(os.path.dirname(__file__), '..', 'zephyr-patches', 'fsu-m0-series', 'provenance')
    f100c = os.path.join(prov, 'central-fsu-f100.config'); f150c = os.path.join(prov, 'central-fsu-f150.config')
    perc = os.path.join(prov, 'periph-fsu.config')
    if os.path.exists(f100c) and os.path.exists(perc):
        ok, rsn, _t, sha = q3_fsu_config_gate(f100c, perc, 'f100')
        R.append(('FSU config gate PASSES the archived f100 central + periph', ok and rsn is None and valid_digest(sha)))
        okf, rsnf, _t2, _s = q3_fsu_config_gate(f150c, perc, 'f100')   # f150 build asserted as f100 -> wrong params
        R.append(('FSU config gate REJECTS a wrong-arm central build', (not okf) and rsnf == 'q3-fsu-config-invalid'))
        okm, rsnm, _t3, _s2 = q3_fsu_config_gate('/no/such/.config', perc, 'f100')   # missing config
        R.append(('FSU config gate REJECTS a missing endpoint .config', (not okm) and rsnm == 'q3-fsu-config-invalid'))
    R.append(('Q3 + wrong FSU firmware -> QUARANTINED q3-fsu-config-invalid (highest q3 precedence)',
              decide_verdict(0, True, True, q3=True, q3_cp_reason='x', q3_onchip_reason='y',
                             q3_fsu_reason='q3-fsu-config-invalid')
              == ('QUARANTINED', 3, 'q3-fsu-config-invalid')))
    # --- PROMOTED Q3 acceptance: a registered mode with every leg passing ACCEPTS ---
    R.append(('Q3 mid-step f100 + all legs pass + clean tree -> ACCEPT',
              decide_verdict(0, True, True, q3=True, q3_arm='f100') == ('ACCEPT', 0, '')))
    R.append(('Q3 steady f150 + all legs pass + clean tree -> ACCEPT',
              decide_verdict(0, True, True, q3=True, q3_steady=True, q3_arm='f150') == ('ACCEPT', 0, '')))
    R.append(('Q3 steady f100 + all legs pass + clean tree -> ACCEPT',
              decide_verdict(0, True, True, q3=True, q3_steady=True, q3_arm='f100') == ('ACCEPT', 0, '')))
    # --- every promotion BYPASS stays quarantined / rejected ---
    R.append(('Q3 + analyzer-FAIL -> REJECT analyzer-fail',
              decide_verdict(3, True, True, q3=True, q3_arm='f100') == ('REJECT', 3, 'analyzer-fail')))
    R.append(('Q3 + dirty pre-run tree -> QUARANTINED pre-run-dirty-tree (HARD gate)',
              decide_verdict(0, True, True, q3=True, q3_arm='f100', pre_run_dirty=True)
              == ('QUARANTINED', 3, 'pre-run-dirty-tree')))
    R.append(('Q3 + --smoke -> QUARANTINED integration-smoke (never accepts)',
              decide_verdict(0, True, True, smoke=True, q3=True, q3_arm='f100')
              == ('QUARANTINED', 3, 'integration-smoke')))
    R.append(('Q3 + contract deviation -> QUARANTINED q3-contract-deviation',
              decide_verdict(0, True, True, q3=True, q3_arm='f100', q3_contract_deviation='guard-override')
              == ('QUARANTINED', 3, 'q3-contract-deviation')))
    R.append(('Q3 mid-step f150 (unregistered arm) -> QUARANTINED q3-unregistered-arm',
              decide_verdict(0, True, True, q3=True, q3_steady=False, q3_arm='f150')
              == ('QUARANTINED', 3, 'q3-unregistered-arm')))
    R.append(('Q3 + unverified observer image -> QUARANTINED observer-image-unverified',
              decide_verdict(0, False, True, q3=True, q3_arm='f100')
              == ('QUARANTINED', 3, 'observer-image-unverified')))
    R.append(('Q3 + missing endpoint provenance -> QUARANTINED endpoint-build-provenance-missing',
              decide_verdict(0, True, False, q3=True, q3_arm='f100')
              == ('QUARANTINED', 3, 'endpoint-build-provenance-missing')))
    # --- rev-6: steady conn-param-update EVENT-GATE ---
    R.append(('Q3 steady + param-update gate incomplete -> QUARANTINED q3-paramupd-incomplete',
              decide_verdict(0, True, True, q3=True, q3_steady=True, q3_arm='f150',
                             q3_paramupd_reason='no Q3PARAMUPD on both endpoints')
              == ('QUARANTINED', 3, 'q3-paramupd-incomplete')))
    import tempfile as _tfp
    IV = Q3_ABBA_FINAL_INTERVAL_UNITS
    def _pu_probe(c_iv, p_iv, aa='dead'):
        cf, pf = _tfp.mktemp(), _tfp.mktemp()
        open(cf, 'w').write(f'HOSTMS 12000 Q3PARAMUPD role=C aa=0x0000dead sess=1 interval={c_iv} latency=0 timeout=42 seq=1\n')
        open(pf, 'w').write(f'HOSTMS 12010 Q3PARAMUPD role=P aa=0x0000dead sess=1 interval={p_iv} latency=0 timeout=42 seq=1\n')
        return wait_paramupd(cf, pf, aa)
    R.append(('wait_paramupd parses matching records + frozen interval',
              _pu_probe(IV, IV) == (True, None, {'interval': IV, 'latency': 0, 'timeout': 42})))
    R.append(('wait_paramupd rejects endpoint interval disagreement', _pu_probe(IV, 24)[0] is False))
    R.append(('wait_paramupd rejects wrong frozen interval', _pu_probe(24, 24)[0] is False))
    R.append(('Q3 + control-plane incomplete -> QUARANTINED q3-control-plane-incomplete',
              decide_verdict(0, True, True, q3=True, q3_cp_reason='no Q3FSU-REQ')
              == ('QUARANTINED', 3, 'q3-control-plane-incomplete')))
    R.append(('Q3 + on-chip incomplete (control plane OK) -> QUARANTINED q3-onchip-incomplete',
              decide_verdict(0, True, True, q3=True, q3_onchip_reason='no TIFS-DRAIN-DONE')
              == ('QUARANTINED', 3, 'q3-onchip-incomplete')))
    # --- rev-4 F3: a steady guard/min-plateau override cannot silently reach acceptance ---
    R.append(('steady guard override w/o reason -> runner aborts (pre-capture)',
              steady_knob_override(True, Q3_GUARD_TICKS_DEFAULT + 1, Q3_MIN_PLATEAU_DEFAULT, None) is True))
    R.append(('steady min-plateau override w/o reason -> runner aborts (pre-capture)',
              steady_knob_override(True, Q3_GUARD_TICKS_DEFAULT, Q3_MIN_PLATEAU_DEFAULT + 1, None) is True))
    R.append(('steady override WITH a deviation reason -> allowed to run but QUARANTINES',
              steady_knob_override(True, Q3_GUARD_TICKS_DEFAULT + 1, Q3_MIN_PLATEAU_DEFAULT, 'why') is False
              and decide_verdict(0, True, True, q3=True, q3_steady=True, q3_arm='f100',
                                 q3_contract_deviation='why') == ('QUARANTINED', 3, 'q3-contract-deviation')))
    R.append(('steady with NO knob override -> not flagged',
              steady_knob_override(True, Q3_GUARD_TICKS_DEFAULT, Q3_MIN_PLATEAU_DEFAULT, None) is False))
    R.append(('mid-step (non-steady) knob override -> not a steady concern',
              steady_knob_override(False, Q3_GUARD_TICKS_DEFAULT + 1, Q3_MIN_PLATEAU_DEFAULT, None) is False))
    # --- rev-5: every accepting --q3-steady run must be bound to the ABBA campaign ---
    R.append(('steady w/o campaign+seq -> binding rejected (pre-hardware)',
              abba_binding_ok('', None, 'f150')[0] is False
              and abba_binding_ok('CID', None, 'f150')[0] is False
              and abba_binding_ok('', 0, 'f150')[0] is False))
    R.append(('steady seq/arm consistent with A/B/B/A -> binding OK',
              abba_binding_ok('CID', 0, 'f150')[0] is True
              and abba_binding_ok('CID', 1, 'f100')[0] is True
              and abba_binding_ok('CID', 3, 'f150')[0] is True))
    R.append(('steady seq/arm INCONSISTENT (seq1 wants f100, got f150) -> rejected',
              abba_binding_ok('CID', 1, 'f150')[0] is False))
    R.append(('steady seq out of range -> rejected',
              abba_binding_ok('CID', 9, 'f150')[0] is False))
    # --- rev-4 F3: steady_settle_min_ms now IN the frozen contract ---
    import analyze_q3 as _Q3s
    R.append(('q3_contract() emits steady_settle_min_ms (frozen)',
              _Q3s.q3_contract().get('steady_settle_min_ms') == _Q3s.Q3_STEADY_SETTLE_MIN_MS))
    # --- rev-4 F4: build_ok requires HEX + ELF + .config (ELF no longer optional) ---
    R.append(('build_ok requires a valid ELF digest too',
              build_ok({'zephyr.hex': 'a'*64, 'zephyr.elf': 'b'*64, '.config': 'c'*64}) is True
              and build_ok({'zephyr.hex': 'a'*64, '.config': 'c'*64}) is False
              and build_ok({'zephyr.hex': 'a'*64, 'zephyr.elf': 'MISSING', '.config': 'c'*64}) is False))
    R.append(('Q2 (q3=False) with full provenance still ACCEPTs',
              decide_verdict(0, True, True) == ('ACCEPT', 0, '')))
    # --- rev-6: firmware archival is a PRE-FLASH, validated ACCEPTANCE PREREQUISITE ---
    R.append(('q3 + firmware-archive-invalid -> QUARANTINED (prerequisite to accept)',
              decide_verdict(0, True, True, q3=True, q3_arm='f100', firmware_archive_valid=False)
              == ('QUARANTINED', 3, 'firmware-archive-invalid')))
    import tempfile as _tf, shutil as _sh
    _prov = os.path.join(os.path.dirname(__file__), '..', 'zephyr-patches', 'fsu-m0-series', 'provenance')
    def _mkbd(root, dev, cfg):
        z = os.path.join(root, dev, 'zephyr'); os.makedirs(z, exist_ok=True)
        open(os.path.join(z, 'zephyr.hex'), 'w').write(f'HEX-{dev}')
        open(os.path.join(z, 'zephyr.elf'), 'w').write(f'ELF-{dev}')
        _sh.copy(cfg, os.path.join(z, '.config'))
        return os.path.join(root, dev)
    if os.path.exists(os.path.join(_prov, 'central-fsu-f100.config')):
        def _fwenv(root, central_cfg):
            out = os.path.join(root, 'cell'); os.makedirs(out, exist_ok=True)
            c = _mkbd(root, 'central', os.path.join(_prov, central_cfg))
            p = _mkbd(root, 'periph', os.path.join(_prov, 'periph-fsu.config'))
            o = _mkbd(root, 'observer', os.path.join(_prov, 'periph-fsu.config'))
            return out, c, p, o
        # (1) valid pre-flash snapshot -> ok + 9 hashes; unchanged right after -> ok
        t1 = _tf.mkdtemp(); out1, c1, p1, o1 = _fwenv(t1, 'central-fsu-f100.config')
        ok1, r1, snap1 = snapshot_firmware(out1, o1, c1, p1, False, 'f100')
        R.append(('snapshot_firmware valid build -> ok + 9 archived hashes', ok1 and len(snap1) == 9))
        a1 = dict(fw_snapshot_ok=ok1, fw_snapshot=snap1, fw_snapshot_reason=r1, skip_obs_flash=False,
                  obs_hex_path=os.path.join(o1, 'zephyr', 'zephyr.hex'), central_build=c1, periph_build=p1)
        R.append(('firmware_unchanged immediately after snapshot -> ok', firmware_unchanged(a1)[0] is True))
        # (2) pre/post SOURCE MUTATION between flash and archive cannot ACCEPT
        open(os.path.join(c1, 'zephyr', 'zephyr.hex'), 'w').write('MUTATED-MID-RUN')
        R.append(('source mutated after snapshot -> firmware_unchanged False', firmware_unchanged(a1)[0] is False))
        R.append(('mid-run mutation cannot ACCEPT',
                  decide_verdict(0, True, True, q3=True, q3_arm='f100',
                                 firmware_archive_valid=firmware_unchanged(a1)[0])
                  == ('QUARANTINED', 3, 'firmware-archive-invalid')))
        # (3) a MISSING build file (copy source absent) -> snapshot invalid
        t2 = _tf.mkdtemp(); out2, c2, p2, o2 = _fwenv(t2, 'central-fsu-f100.config')
        os.remove(os.path.join(c2, 'zephyr', 'zephyr.elf'))
        ok2, r2, _ = snapshot_firmware(out2, o2, c2, p2, False, 'f100')
        R.append(('snapshot missing build file -> invalid (cannot accept)',
                  ok2 is False and decide_verdict(0, True, True, q3=True, q3_arm='f100',
                                                  firmware_archive_valid=ok2)[0] == 'QUARANTINED'))
        # (4) a WRONG-ARM archived central config -> assert_fsu_config fails the snapshot
        t3 = _tf.mkdtemp(); out3, c3, p3, o3 = _fwenv(t3, 'central-fsu-f150.config')   # f150 cfg, arm f100
        ok3, r3, _ = snapshot_firmware(out3, o3, c3, p3, False, 'f100')
        R.append(('snapshot wrong-arm central config -> assert fails', ok3 is False))
        for t in (t1, t2, t3): _sh.rmtree(t, ignore_errors=True)
    # --- Q3 F timing: NOMINAL capture budget (post is from F-send, not completion;
    # the real post-plateau is measured from the completion ts by the Q3 analyzer) ---
    R.append(('q3_plan nominal budget leaves pre+post plateaus',
              q3_plan(30000, 8, 500) == {'f_at_s': 8, 'pre_s': 8, 'post_s': 30 - 8 - 0.5}))
    def _raises(f):
        try: f(); return False
        except ValueError: return True
    R.append(('q3_plan rejects too-small baseline dwell (no pre-step data)',
              _raises(lambda: q3_plan(30000, 2, 500))))
    R.append(('q3_plan rejects too-large dwell (no nominal post-step budget)',
              _raises(lambda: q3_plan(30000, 28, 500))))
    # --- q3_completeness enforces the full REQ<->DONE relationships ---
    REQ  = 'HOSTMS 1 Q3FSU-REQ role=C aa=0x00000001 sess=1 min=100 max=150 phys=0x1 types=0x3 rc=0 seq=1\n'
    DONE = 'HOSTMS 2 Q3FSU-DONE role=C aa=0x00000001 sess=1 status=0x00 spacing=100 types=0x3 phys=0x1 initiator=0\n'
    R.append(('q3 complete = one REQ + matching DONE (aa/sess/seq/spacing/phys/types/order)',
              q3_completeness(REQ + DONE) == ('complete', '')))
    R.append(('q3 Q2 log (no F) -> incomplete', q3_completeness('HOSTMS 1 Q2SNAP role=C seq=0\n')[0] == 'incomplete'))
    R.append(('q3 missing DONE -> incomplete', q3_completeness(REQ)[0] == 'incomplete'))
    R.append(('q3 reject -> incomplete',
              q3_completeness('HOSTMS 1 Q3FSU-REJECT role=C reason=no-conn\n')[0] == 'incomplete'))
    R.append(('q3 two REQ -> incomplete (exactly one)', q3_completeness(REQ + REQ + DONE)[0] == 'incomplete'))
    R.append(('q3 REQ rc!=0 -> incomplete', q3_completeness(REQ.replace('rc=0','rc=-22') + DONE)[0] == 'incomplete'))
    R.append(('q3 REQ seq!=1 -> incomplete', q3_completeness(REQ.replace('seq=1','seq=2') + DONE)[0] == 'incomplete'))
    R.append(('q3 DONE status!=0 -> incomplete', q3_completeness(REQ + DONE.replace('status=0x00','status=0x0c'))[0] == 'incomplete'))
    R.append(('q3 AA mismatch -> incomplete', q3_completeness(REQ + DONE.replace('aa=0x00000001','aa=0x00000002'))[0] == 'incomplete'))
    R.append(('q3 session mismatch -> incomplete', q3_completeness(REQ + DONE.replace('sess=1','sess=2'))[0] == 'incomplete'))
    R.append(('q3 selected spacing != requested -> incomplete', q3_completeness(REQ + DONE.replace('spacing=100','spacing=52'))[0] == 'incomplete'))
    R.append(('q3 PHY mismatch -> incomplete', q3_completeness(REQ + DONE.replace('phys=0x1','phys=0x2'))[0] == 'incomplete'))
    R.append(('q3 spacing-types mismatch -> incomplete', q3_completeness(REQ + DONE.replace('types=0x3','types=0x1'))[0] == 'incomplete'))
    R.append(('q3 DONE-before-REQ ordering -> incomplete',
              q3_completeness(REQ.replace('HOSTMS 1','HOSTMS 9') + DONE.replace('HOSTMS 2','HOSTMS 5'))[0] == 'incomplete'))
    # --- peer participation: responder completion bound to the same AA/session ---
    PDONE = 'HOSTMS 3 Q3FSU-DONE role=P aa=0x00000001 sess=1 status=0x00 spacing=100 types=0x3 phys=0x1 initiator=2\n'
    R.append(('q3 peer participation complete (periph DONE, same AA/sess/spacing)',
              q3_peer_participation(REQ, PDONE) == ('complete', '')))
    R.append(('q3 peer MISSING periph DONE -> incomplete',
              q3_peer_participation(REQ, 'HOSTMS 3 PERIPH connected\n')[0] == 'incomplete'))
    R.append(('q3 peer AA mismatch -> incomplete',
              q3_peer_participation(REQ, PDONE.replace('aa=0x00000001','aa=0x00000009'))[0] == 'incomplete'))
    R.append(('q3 peer session mismatch -> incomplete',
              q3_peer_participation(REQ, PDONE.replace('sess=1','sess=2'))[0] == 'incomplete'))
    R.append(('q3 peer spacing mismatch -> incomplete',
              q3_peer_participation(REQ, PDONE.replace('spacing=100','spacing=52'))[0] == 'incomplete'))
    R.append(('q3 peer status!=0 -> incomplete',
              q3_peer_participation(REQ, PDONE.replace('status=0x00','status=0x0c'))[0] == 'incomplete'))
    R.append(('q3 peer PHY mismatch -> incomplete',
              q3_peer_participation(REQ, PDONE.replace('phys=0x1','phys=0x2'))[0] == 'incomplete'))
    R.append(('q3 peer spacing-types mismatch -> incomplete',
              q3_peer_participation(REQ, PDONE.replace('types=0x3','types=0x1'))[0] == 'incomplete'))
    R.append(('q3 peer completion-before-request -> incomplete',
              q3_peer_participation(REQ.replace('HOSTMS 1','HOSTMS 9'), PDONE.replace('HOSTMS 3','HOSTMS 4'))[0] == 'incomplete'))
    # --- on-chip lifecycle + drain (peripheral RX-PHYEND->TX-READY, separate 150/100 bins) ---
    LIFE = ('HOSTMS 4 TIFS-CLEARED role=P aa=0x00000001 sess=1 life=1\n'
            'HOSTMS 7 TIFS-FROZEN role=P aa=0x00000001 sess=1 life=1\n'
            'HOSTMS 8 TIFSBIN tifs=150 phy=1 n=400 nv=395 min=148 med=150 max=153 drop=0\n'
            'HOSTMS 8 TIFSBIN tifs=100 phy=1 n=420 nv=418 min=98 med=100 max=103 drop=0\n'
            'HOSTMS 9 TIFS-DRAIN-DONE role=P aa=0x00000001 sess=1 life=1\n')
    AA, SS = 1, 1   # connection AA, Q3 request session
    R.append(('q3_onchip_bins parses both spacings (list) with medians',
              [b['med'] for b in q3_onchip_bins(LIFE)] == [150, 100]))
    R.append(('q3_onchip_check complete (one chronological lifecycle, drop=0, 150/100 @1M)',
              q3_onchip_check(LIFE, AA, SS, [150,100]) == ('complete', '')))
    R.append(('q3_onchip missing DRAIN-DONE -> incomplete',
              q3_onchip_check(LIFE.replace('TIFS-DRAIN-DONE','x'), AA, SS, [150,100])[0] == 'incomplete'))
    R.append(('q3_onchip two CLEARED (not one lifecycle) -> incomplete',
              q3_onchip_check(LIFE + 'HOSTMS 3 TIFS-CLEARED role=P aa=0x00000001 sess=1 life=2\n', AA, SS, [150,100])[0] == 'incomplete'))
    R.append(('q3_onchip AA != connection -> incomplete', q3_onchip_check(LIFE, 2, SS, [150,100])[0] == 'incomplete'))
    R.append(('q3_onchip life-seq mismatch -> incomplete',
              q3_onchip_check(LIFE.replace('TIFS-FROZEN role=P aa=0x00000001 sess=1 life=1','TIFS-FROZEN role=P aa=0x00000001 sess=1 life=2'), AA, SS, [150,100])[0] == 'incomplete'))
    R.append(('q3_onchip session != Q3 request session -> incomplete',
              q3_onchip_check(LIFE, AA, 2, [150,100])[0] == 'incomplete'))
    R.append(('q3_onchip non-chronological (freeze before clear) -> incomplete',
              q3_onchip_check(LIFE.replace('HOSTMS 7 TIFS-FROZEN','HOSTMS 2 TIFS-FROZEN'), AA, SS, [150,100])[0] == 'incomplete'))
    R.append(('q3_onchip drop!=0 -> incomplete', q3_onchip_check(LIFE.replace('drop=0','drop=2',1), AA, SS, [150,100])[0] == 'incomplete'))
    R.append(('q3_onchip duplicate (tifs,phy) bin -> incomplete',
              q3_onchip_check(LIFE.replace('tifs=100 phy=1','tifs=150 phy=1',1), AA, SS, None)[0] == 'incomplete'))
    R.append(('q3_onchip phy!=1M -> incomplete', q3_onchip_check(LIFE.replace('tifs=100 phy=1','tifs=100 phy=2'), AA, SS, [150,100])[0] == 'incomplete'))
    R.append(('q3_onchip bin min>med (bad order) -> incomplete',
              q3_onchip_check(LIFE.replace('min=148 med=150','min=151 med=150'), AA, SS, [150,100])[0] == 'incomplete'))
    R.append(('q3_onchip unexpected spacing -> incomplete',
              q3_onchip_check(LIFE + 'HOSTMS 8 TIFSBIN tifs=52 phy=1 n=10 nv=10 min=50 med=52 max=54 drop=0\n', AA, SS, [150,100])[0] == 'incomplete'))
    R.append(('q3_onchip missing expected spacing -> incomplete', q3_onchip_check(LIFE, AA, SS, [150,100,52])[0] == 'incomplete'))
    R.append(('q3_onchip nv=0 -> incomplete', q3_onchip_check(LIFE.replace('nv=418','nv=0'), AA, SS, [150,100])[0] == 'incomplete'))
    R.append(('q3_onchip nv>n -> incomplete', q3_onchip_check(LIFE.replace('nv=418','nv=999'), AA, SS, [150,100])[0] == 'incomplete'))
    # boundary slops: clear-after-START and freeze-after-END
    OBS = 'HOSTMS 100 CAPTURE-START Q2 ch10 cap=30000ms tick=0\nHOSTMS 200 CAPTURE-END Q2 tick=480000000\n'
    PER = 'HOSTMS 150 TIFS-CLEARED role=P aa=0x1 sess=1 life=1\nHOSTMS 260 TIFS-FROZEN role=P aa=0x1 sess=1 life=1\n'
    R.append(('q3_boundary_slops OK (small positive slops)',
              q3_boundary_slops(OBS, PER)[0] == 'complete' and q3_boundary_slops(OBS, PER)[2] == {'clear_slop_ms':50,'freeze_slop_ms':60}))
    R.append(('q3_boundary_slops clear before START (negative) -> incomplete',
              q3_boundary_slops(OBS.replace('HOSTMS 100 CAPTURE-START','HOSTMS 160 CAPTURE-START'), PER)[0] == 'incomplete'))
    R.append(('q3_boundary_slops freeze slop too large -> incomplete',
              q3_boundary_slops(OBS, PER.replace('HOSTMS 260 TIFS-FROZEN','HOSTMS 5000 TIFS-FROZEN'))[0] == 'incomplete'))
    import analyze_q3 as _Q3
    R.append(('runner Q3 knobs mirror analyze_q3 frozen constants (no drift)',
              Q3_GUARD_TICKS_DEFAULT == _Q3.Q3_GUARD_TICKS and
              Q3_MIN_PLATEAU_DEFAULT == _Q3.Q3_MIN_PLATEAU_PAIRS and
              Q3_BOUNDARY_SLOP_MS == _Q3.Q3_BOUNDARY_SLOP_MS))
    R.append(('runner steady settle mirrors analyze_q3 frozen minimum (no drift)',
              Q3_STEADY_SETTLE_MS_DEFAULT == _Q3.Q3_STEADY_SETTLE_MIN_MS and STEADY_SETTLE_PAD_S > 0))
    print('=== RUNNER SELFTEST ===')
    for n, v in R: print(f'  [{"PASS" if v else "FAIL"}] {n}')
    ok = all(v for _, v in R); print(f'RUNNER SELFTEST: {"PASS" if ok else "FAIL"}')
    return 0 if ok else 1

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--selftest':
        sys.exit(selftest())
    main()
