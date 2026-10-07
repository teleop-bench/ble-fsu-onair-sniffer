#!/usr/bin/env python3
"""Q3 step analyzer -- the FSU on-air verdict. DISTINCT from the Q2 analyzer, but it
REUSES every Q2 validity gate before computing anything Q3-specific.

Pipeline (each leg can only downgrade to INCOMPLETE, never silently pass):
  1. Q2 COMMON  : analyze_q2.validate_common -- structural (FROZEN/LOSS/anchors/oseq),
                  Q2CONN, both DEVICEIDs, bracketing snapshots (denominators+slop),
                  full AA/CRCInit/map/chan/PHY config binding.
  2. ANCHORS    : exactly one CAPTURE-START/END, both tick=; host & timer monotonic;
                  tick- AND host-duration each consistent with the declared cap.
  3. CALIB      : a VALIDATED --calib (validate_calib re-derives the whole artifact);
                  median(pre plateau) must equal the frozen 150us gap_proxy +/- 2t.
  4. CONTROL    : q3_completeness (strict one REQ+one DONE, all fields agree) +
                  q3_peer_participation (responder DONE bound to the same AA/session);
                  REQ + both DONE must lie INSIDE the anchored capture.
  5. ON-CHIP    : q3_onchip_check (one chronological clear<freeze<drain, session ==
                  request session, exactly 150/100 @1M, drop=0, min<=med<=max) +
                  q3_boundary_slops (clear-after-START / freeze-after-END gated).
  6. PARTITION  : conservative WHOLE-INTERVAL split by REC addr/end TIMER0 ticks
                  (never dump HOSTMS) vs a TIMESTAMP-derived window [lo,hi]: PRE iff
                  next.address<lo, POST iff previous.end>hi, else EXCLUDED.
  7. STEP       : median(pre)-median(post) (statistics.median; compared as a REAL to
                  800 +/- 4, IMMUTABLE). Cross-validated in us against the on-chip
                  bin-median delta. Change point re-estimated INDEPENDENTLY
                  (max-abs-mean-split over ALL pairs) as a secondary consistency check
                  that cannot move the partition.

Outcomes: METRICS-OK (800+/-4 AND on-chip agree) | REDUCTION-OFF-TARGET (clear
reduction, off target) | NO-REDUCTION (null) | WRONG-DIRECTION | XVAL-DISAGREE |
INCOMPLETE (a gate could not be computed). Only METRICS-OK exits 0.

This analyzer reports METRICS ONLY; the RUNNER (q2_run.decide_verdict) owns the cell
verdict. Post-promotion a registered mode with every leg passing on a clean, image
-verified tree ACCEPTS; --smoke, a dirty tree, an unregistered/wrong arm, a contract
deviation, or missing provenance still QUARANTINE. This module never asserts a cell is
accepted -- it only asserts the metrics passed.
"""
import sys, os, re, statistics as st
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze_q2 as A
import q2_run as Q

WRAP = A.TIMER0_WRAP

# ---- FROZEN Q3 ACCEPTANCE CONTRACT (every acceptance constant, in one place) ----
Q3_STEP_TOL             = 4          # |step-800| <= this. IMMUTABLE primary gate.
Q3_EXPECT_STEP_TICKS    = (150 - 100) * A.TICKS_PER_US   # 800
# ABBA steady-state CONFIRMATION arm (combine_abba.py). Four steady cells collected
# in the frozen f150/f100/f100/f150 order; the drift-cancelling estimator
# ((A1-B1)+(A2-B2))/2 removes any linear environmental drift over the campaign and
# must land on the same 800t step within the SAME immutable +/-4 primary tolerance.
Q3_ABBA_ARM_SEQ         = ('f150', 'f100', 'f100', 'f150')   # A B B A, frozen order
Q3_ABBA_EXPECT_STEP     = Q3_EXPECT_STEP_TICKS   # 800 t, drift-cancelled target
Q3_ABBA_TOL_TICKS       = Q3_STEP_TOL            # |estimate-800| <= this (same primary gate)
Q3_DUR_TOL_MS           = 1500       # tick- AND host-duration vs declared cap
Q3_GUARD_TICKS          = 160_000    # extra exclusion margin around the completions
Q3_MIN_PLATEAU_PAIRS    = 30         # min clean pairs per plateau AFTER exclusion
Q3_BASELINE_TOL_TICKS   = 2          # median(pre) vs frozen 150us gap_proxy
Q3_XVAL_TOL_US          = 8          # |on_air_step/16 - (onchip150-onchip100)| us
Q3_NULL_THRESHOLD_TICKS = 64         # |step|<this => NO-REDUCTION; <=-this => WRONG-DIR
Q3_CHANGEPOINT_METHOD   = 'max-abs-mean-split'
# STABLE-PLATEAU SHAPE gate (per plateau). CALIBRATED from the SIX pre-existing
# ACCEPTED Q2 live-link captures (symctl1/2 + nearfarA1/A2/B1/B2), NOT the synthetic
# generator: their real gap_proxy has IQR 8-10 t, block-dev <=2 t, half-drift <=2 t,
# and >=0.98 of samples within +/-8 t of the median. Thresholds sit above those with
# margin (see the replay regression test). This is a CONCENTRATION check (single broad
# mode), NOT a formal modality test.
Q3_PLATEAU_IQR_MAX_TICKS   = 16     # within-plateau inter-quartile spread (max observed 10)
Q3_PLATEAU_DRIFT_MAX_TICKS = 5      # |median(first half) - median(second half)| (max observed 2)
Q3_PLATEAU_BLOCKS          = 4      # time-ordered blocks for the stability check
Q3_PLATEAU_BLOCK_MAX_TICKS = 5      # each block median within this of the plateau median (max observed 2)
Q3_PLATEAU_CONC_EPS_TICKS  = 10     # concentration window (+/- around median); real bulk within +/-8
Q3_PLATEAU_CONC_FRAC       = 0.95   # >= this fraction within the window (real cells >= 0.99)
Q3_PHASE_RETENTION_MIN     = A.RETENTION_MIN   # per-phase pairs/tx floor; FROZEN, inherited from the established Q2 RETENTION_MIN (0.95). If the smoke misses it -> instrument incompleteness + redesign, NOT retune.
# PHASE-BALANCE is a SECONDARY DIAGNOSTIC ONLY (reported, never a gate): the six
# accepted Q2 cells show a strong EARLY pair-rate transient (57-61% imbalance across
# an 8 s split with NO FSU), so "pair rate is FSU-invariant" is empirically FALSE for
# this instrument geometry. Phase-SPECIFIC retention uses F-time counter snapshots
# instead (see q3_phase_retention).
# REGISTERED request parameters (the ONE arm this protocol verifies): 150->100us,
# both ACL spacing types (bit0|bit1 = 0x3), 1M (0x1). A request that differs is not
# this experiment.
Q3_REG_MIN, Q3_REG_MAX, Q3_REG_PHYS, Q3_REG_TYPES = 100, 150, 0x1, 0x3
# referenced from their owning modules (single source of truth):
Q3_HOST_JITTER_MS       = A.Q3_HOST_JITTER_MS     # cross-port RELATIVE uncertainty (50)
Q3_BOUNDARY_SLOP_MS     = Q.Q3_BOUNDARY_SLOP_MS   # clear/freeze boundary allowance (1000)
EXPECT_PRE_US, EXPECT_POST_US = 150, 100


def q3_contract(guard_ticks=Q3_GUARD_TICKS, min_plateau=Q3_MIN_PLATEAU_PAIRS):
    """The frozen acceptance contract as a dict (also emitted as one machine-readable
    Q3-CONTRACT line). guard/min-plateau are echoed as the values ACTUALLY used, so a
    run's manifest pins what it was analyzed under."""
    return dict(step_tol=Q3_STEP_TOL, expect_step=Q3_EXPECT_STEP_TICKS,
                dur_tol_ms=Q3_DUR_TOL_MS, guard_ticks=guard_ticks, min_plateau=min_plateau,
                baseline_tol_ticks=Q3_BASELINE_TOL_TICKS, retention_min=A.RETENTION_MIN,
                xval_tol_us=Q3_XVAL_TOL_US, null_thr_ticks=Q3_NULL_THRESHOLD_TICKS,
                host_jitter_ms=Q3_HOST_JITTER_MS, boundary_slop_ms=Q3_BOUNDARY_SLOP_MS,
                changepoint=Q3_CHANGEPOINT_METHOD,
                reg_min=Q3_REG_MIN, reg_max=Q3_REG_MAX, reg_phys=Q3_REG_PHYS, reg_types=Q3_REG_TYPES,
                expect_pre_us=EXPECT_PRE_US, expect_post_us=EXPECT_POST_US,
                plateau_iqr_max=Q3_PLATEAU_IQR_MAX_TICKS, plateau_drift_max=Q3_PLATEAU_DRIFT_MAX_TICKS,
                plateau_blocks=Q3_PLATEAU_BLOCKS, plateau_block_max=Q3_PLATEAU_BLOCK_MAX_TICKS,
                plateau_conc_eps=Q3_PLATEAU_CONC_EPS_TICKS, plateau_conc_frac=Q3_PLATEAU_CONC_FRAC,
                phase_retention_min=Q3_PHASE_RETENTION_MIN, steady_tol_ticks=Q3_STEADY_TOL_TICKS,
                steady_settle_min_ms=Q3_STEADY_SETTLE_MIN_MS,
                abba_arm_seq=list(Q3_ABBA_ARM_SEQ), abba_expect_step=Q3_ABBA_EXPECT_STEP,
                abba_tol_ticks=Q3_ABBA_TOL_TICKS)


def classify_step(step):
    """Frozen null/direction taxonomy over the real-valued step."""
    if step <= -Q3_NULL_THRESHOLD_TICKS:            return 'WRONG-DIRECTION'
    if abs(step) < Q3_NULL_THRESHOLD_TICKS:         return 'NO-REDUCTION'
    if abs(step - Q3_EXPECT_STEP_TICKS) <= Q3_STEP_TOL: return 'AGREEMENT'
    return 'REDUCTION-OFF-TARGET'


def _changepoint(seq, min_side):
    """Preregistered INDEPENDENT secondary estimator (max-abs-mean-split), DIAGNOSTIC
    ONLY: over ALL pairs ordered by acquisition tick, the split k maximizing
    |mean(left)-mean(right)|. Uses every pair + its span (does NOT consult the
    timestamp partition). Both sides are constrained to >= min_side so a single edge
    outlier at k=1/n-1 cannot win. Returns (cp_tick, k) or (None, None) if too few
    pairs for a constrained split."""
    n = len(seq)
    if n < 2 * min_side:
        return None, None
    ticks = [t for t, _ in seq]; spans = [s for _, s in seq]
    total = sum(spans); pre = sum(spans[:min_side]); best = -1.0; bk = None
    for k in range(min_side, n - min_side + 1):
        d = abs(pre / k - (total - pre) / (n - k))
        if d > best:
            best = d; bk = k
        if k < n - min_side:
            pre += spans[k]
    return (ticks[bk - 1] + ticks[bk]) / 2.0, bk


def _plateau_shape(spans):
    """STABLE-PLATEAU gate: `spans` in acquisition-TIME order. Rejects a plateau that
    is not a single stable tIFS via four preregistered sub-checks -- within-plateau
    IQR, first/second-half DRIFT, per-block median stability, and CONCENTRATION (a
    broad-single-mode check, NOT a formal modality test). Thresholds calibrated from
    the six accepted Q2 cells. Returns (ok, reason, stats)."""
    n = len(spans)
    ss = sorted(spans)
    iqr = ss[(3 * n) // 4] - ss[n // 4]
    med = st.median(spans)
    frac = sum(1 for s in spans if abs(s - med) <= Q3_PLATEAU_CONC_EPS_TICKS) / n
    h = n // 2
    drift = abs(st.median(spans[:h]) - st.median(spans[h:]))
    stats = {'iqr': iqr, 'drift': drift, 'conc_frac': round(frac, 3)}
    if iqr > Q3_PLATEAU_IQR_MAX_TICKS:
        return False, f'IQR {iqr} > {Q3_PLATEAU_IQR_MAX_TICKS}', stats
    if drift > Q3_PLATEAU_DRIFT_MAX_TICKS:
        return False, f'half-to-half drift {drift} > {Q3_PLATEAU_DRIFT_MAX_TICKS}', stats
    b = max(1, n // Q3_PLATEAU_BLOCKS)
    for i in range(Q3_PLATEAU_BLOCKS):
        blk = spans[i * b:] if i == Q3_PLATEAU_BLOCKS - 1 else spans[i * b:(i + 1) * b]
        if blk and abs(st.median(blk) - med) > Q3_PLATEAU_BLOCK_MAX_TICKS:
            return False, f'block {i} median off plateau by > {Q3_PLATEAU_BLOCK_MAX_TICKS}', stats
    if frac < Q3_PLATEAU_CONC_FRAC:
        return False, f'concentration {frac:.2f} within +/-{Q3_PLATEAU_CONC_EPS_TICKS}t < {Q3_PLATEAU_CONC_FRAC} (too spread)', stats
    return True, '', stats


def _phase_rate(ticks):
    """LOCAL pair rate = (n-1) / observed tick-span. DIAGNOSTIC ONLY (the instrument
    has a strong early-connection rate transient -> not a gate). None if degenerate."""
    if len(ticks) < 2:
        return None
    span = max(ticks) - min(ticks)
    return (len(ticks) - 1) / span if span > 0 else None


def _snap_tx(text, role, seq):
    """raw tx at a Q2SNAP role=.. seq=.. (START=0 / END=1). None if absent."""
    for l in text.splitlines():
        if 'Q2SNAP' in l and f'role={role}' in l and f'seq={seq}' in l:
            m = re.search(r'\btx=(\d+)', l)
            if m:
                return int(m.group(1))
    return None


def _q2snap_host(text, role, seq):
    for l in text.splitlines():
        m = re.search(rf'HOSTMS (\d+) .*Q2SNAP role={role} seq={seq}\b', l)
        if m:
            return int(m.group(1))
    return None


def _phase_snaps(text, role):
    """all Q3PHASESNAP role=X records -> list of {host,tx,aa,sess,boottag,ch}."""
    out = []
    for l in text.splitlines():
        if 'Q3PHASESNAP' not in l or f'role={role}' not in l:
            continue
        d, mh = {}, re.match(r'HOSTMS (\d+) ', l)
        d['host'] = int(mh.group(1)) if mh else None
        for k in ('aa', 'sess', 'ch', 'tx', 'boottag'):
            m = re.search(rf'\b{k}=(0x[0-9a-fA-F]+|\d+)', l)
            d[k] = (int(m.group(1), 16) if m.group(1).startswith('0x') else int(m.group(1))) if m else None
        out.append(d)
    return out


def q3_bind_mid(text, role, conn_aa, req_sess, boottag, obs_ch, start_host, end_host, anchors, lo, hi):
    """Identity- and timing-bind the F-time MID snapshot: EXACTLY ONE per role, all
    fields present, aa/session/boottag/channel matching the connection + READY, host
    strictly between the START and END snaps (post-provenance), and its reltick +/-
    mapping-uncertainty INSIDE the transition window. Returns (mid_tx, '') or
    (None, reason)."""
    snaps = _phase_snaps(text, role)
    if len(snaps) != 1:
        return None, f'{role}: {len(snaps)} Q3PHASESNAP (expected exactly one)'
    s = snaps[0]
    if any(s.get(k) is None for k in ('host', 'tx', 'aa', 'sess', 'boottag', 'ch')):
        return None, f'{role} MID snapshot missing fields'
    if s['aa'] != conn_aa:      return None, f'{role} MID aa {s["aa"]} != connection {conn_aa}'
    if s['sess'] != req_sess:   return None, f'{role} MID session {s["sess"]} != request {req_sess}'
    if s['boottag'] != boottag: return None, f'{role} MID boottag != READY (reboot/mixup)'
    if s['ch'] != obs_ch:       return None, f'{role} MID ch {s["ch"]} != observer ch {obs_ch}'
    if start_host is None or end_host is None or not (start_host < s['host'] < end_host):
        return None, f'{role} MID host {s["host"]} not strictly within [START {start_host}, END {end_host}] snaps'
    r, u = A.q3_host_to_reltick(anchors, s['host'])
    if not (lo <= r - u and r + u <= hi):
        return None, f'{role} MID reltick {r:.0f}+/-{u:.0f} outside transition window [{lo:.0f},{hi:.0f}]'
    return s['tx'], ''


def q3_phase_retention(txs, pre_pairs, post_pairs, ret_min=Q3_PHASE_RETENTION_MIN):
    """PHASE-SPECIFIC retention from BOUND snapshot triples txs={role:(start,mid,end)}:
    split the ch10 TX denominator into pre (START->MID) and post (MID->END) and require
    observer pairs / phase-tx >= ret_min for BOTH phases on BOTH roles (conservative --
    MID sits inside the excluded transition, so the phase denominators over-count).
    Catches a concentrated per-phase loss that whole-cell retention dilutes."""
    stats = {}
    for role, (s, m, e) in txs.items():
        pre_tx, post_tx = m - s, e - m
        if pre_tx <= 0 or post_tx <= 0:
            return 'incomplete', f'{role} phase counter non-monotonic (pre {pre_tx}, post {post_tx})', stats
        rp, ro = pre_pairs / pre_tx, post_pairs / post_tx
        stats[role] = {'pre_tx': pre_tx, 'post_tx': post_tx, 'pre_ret': round(rp, 3), 'post_ret': round(ro, 3)}
        if pre_pairs > pre_tx or post_pairs > post_tx:
            return 'incomplete', f'{role} phase overcount (pairs > phase tx)', stats
        if rp < ret_min or ro < ret_min:
            return 'incomplete', f'{role} phase retention below {ret_min}: pre {rp*100:.1f}% post {ro*100:.1f}%', stats
    return 'complete', '', stats


def _inc(pr, reason):
    pr(f'INCOMPLETE: {reason}')
    return dict(verdict='INCOMPLETE', reason=reason), 3


Q3_STEADY_TOL_TICKS = 4        # steady single-plateau median vs its expected value (frozen)
Q3_STEADY_SETTLE_MIN_MS = 2000 # f100-steady: min interval from BOTH FSU completions to capture-start (frozen)


def q3_steady_analyze(obs, cf, pf, arm, calib_frozen=None, calib_path=None, quiet=False):
    """STEADY-STATE single-plateau analyzer (the ABBA confirmation arm; the within-
    connection f100 STEP analyzer stays the PRIMARY experiment). arm='f150' = no FSU
    request, one stable 150us plateau, exactly a 150 on-chip bin; arm='f100' = FSU
    COMPLETED BEFORE the measurement window, one stable 100us plateau, exactly a 100
    on-chip bin. Reuses the frozen Q2 gates + shape gate + whole-cell retention; gates
    the single-plateau median against the calibrated expectation (150us=frozen baseline,
    100us=frozen-800) and the arm-specific control plane. Returns (result, rc)."""
    pr = (lambda *a: None) if quiet else print
    if arm not in ('f150', 'f100', 'f52'):
        return _inc(pr, f"steady arm must be f150|f100|f52 (got {arm})")
    o = A.parse_obs(obs)
    ctx, creason = A.validate_common(o, cf, pf, pr)
    if ctx is None:
        return _inc(pr, f'q2-common: {creason}')
    conn_aa = ctx['conn']['aa']
    cs, Ts, Te = o['cs_tick'], o['Ts'], o['Te']   # steady needs no transition window
    if cs is None or Ts is None or Te is None or Ts >= Te:
        return _inc(pr, 'anchors: missing tick= or non-monotonic host')
    dur_ticks = (o['ce_tick'] - cs) & (WRAP - 1)
    cap_ms = o['cfg'].get('cap_ms')
    if cap_ms is None or abs(dur_ticks / (A.TICKS_PER_US * 1000.0) - cap_ms) > Q3_DUR_TOL_MS:
        return _inc(pr, 'anchors: tick duration inconsistent with declared cap')
    if calib_frozen is None:
        if not calib_path:
            return _inc(pr, 'steady requires a validated --calib baseline')
        calib_frozen, kr = A.validate_calib(calib_path)
        if calib_frozen is None:
            return _inc(pr, f'calibration invalid: {kr}')
    expect = calib_frozen if arm == 'f150' else (calib_frozen - Q3_EXPECT_STEP_TICKS)
    tol = Q3_BASELINE_TOL_TICKS if arm == 'f150' else Q3_STEADY_TOL_TICKS
    expect_us = EXPECT_PRE_US if arm == 'f150' else EXPECT_POST_US

    ctext, ptext, otext = (open(p, errors='replace').read() for p in (cf, pf, obs))
    snap_sess = ctx['cw']['sess']                    # connection session (bracketing snaps)
    # arm-specific CONTROL PLANE. onchip_sess = the session the on-chip lifecycle must bind to.
    if arm == 'f150':
        # a clean no-request run: NO FSU activity of ANY kind (REQ / REJECT / DONE).
        for tok in ('Q3FSU-REQ', 'Q3FSU-REJECT', 'Q3FSU-DONE'):
            if tok in ctext or tok in ptext:
                return _inc(pr, f'f150 steady: unexpected FSU activity ({tok}) -- must be no-request')
        onchip_sess = snap_sess
    else:  # f100 steady: FSU COMPLETED (peer too), bound + settled BEFORE the capture window
        cpv, cpr = Q.q3_completeness(ctext)
        if cpv != 'complete':
            return _inc(pr, f'control-plane: {cpr}')
        ppv, ppr = Q.q3_peer_participation(ctext, ptext)
        if ppv != 'complete':
            return _inc(pr, f'peer-participation: {ppr}')
        req = Q._q3_fields([l for l in ctext.splitlines() if 'Q3FSU-REQ' in l][0])
        pdone = Q._q3_fields([l for l in ptext.splitlines() if 'Q3FSU-DONE role=P' in l][0])
        cdone = Q._q3_fields([l for l in ctext.splitlines() if 'Q3FSU-DONE role=C' in l][0])
        if not (req.get('sess') == snap_sess == ctx['pw']['sess']):
            return _inc(pr, f"f100 steady: request session {req.get('sess')} != snapshot sessions "
                            f"C={snap_sess} P={ctx['pw']['sess']}")
        if pdone.get('aa') != conn_aa or pdone.get('initiator') != Q.Q3_INITIATOR_PEER:
            return _inc(pr, 'f100 steady: peripheral completion not peer-bound (aa/initiator)')
        ch, ph = cdone.get('_hostms'), pdone.get('_hostms')
        if ch is None or ph is None:
            return _inc(pr, 'f100 steady: missing completion HOSTMS')
        if ch >= Ts or ph >= Ts:
            return _inc(pr, 'f100 steady: FSU did not complete BEFORE the measurement window')
        settle = Ts - max(ch, ph)
        if settle < Q3_STEADY_SETTLE_MIN_MS:
            return _inc(pr, f'f100 steady: settle {settle}ms < {Q3_STEADY_SETTLE_MIN_MS}ms '
                            f'(both completions -> capture-start)')
        onchip_sess = req.get('sess')

    # single plateau: whole-cell retention + shape + median vs expected
    good = [r for r in o['recs'] if r['crc'] == 1 and r['st'] == 0]
    pairs = A.find_pairs(good)
    npairs = len(pairs)
    cN, pN = ctx['cN'], ctx['pN']
    if npairs > cN or npairs > pN:
        return _inc(pr, f'overcount: {npairs} > denom (C={cN} P={pN})')
    ret_c, ret_p = npairs / cN, npairs / pN
    if ret_c < A.RETENTION_MIN or ret_p < A.RETENTION_MIN:
        return _inc(pr, f'whole-cell retention below {A.RETENTION_MIN}: C {ret_c*100:.1f}% P {ret_p*100:.1f}%')
    if npairs < Q3_MIN_PLATEAU_PAIRS:
        return _inc(pr, f'plateau pairs {npairs} < min {Q3_MIN_PLATEAU_PAIRS}')
    spans = [p['span'] for p in pairs]   # acquisition order
    okp, rp, shape = _plateau_shape(spans)
    if not okp:
        return _inc(pr, f'plateau not stable: {rp}')
    med = st.median(spans)
    if abs(med - expect) > tol:
        return _inc(pr, f'{arm} plateau median {med} != expected {expect} +/-{tol}')

    # boundary slops: clear-after-CAPTURE-START / freeze-after-CAPTURE-END (same as mid-step)
    bsv, bsr, slops = Q.q3_boundary_slops(otext, ptext)
    if bsv != 'complete':
        return _inc(pr, f'boundary-slop: {bsr}')
    # on-chip: EXACTLY ONE bin at the expected spacing; lifecycle SESSION-bound to the
    # connection (f150) / request (f100) session -- not merely internally consistent.
    ocv, ocr = Q.q3_onchip_check(ptext, conn_aa, onchip_sess, [expect_us], expect_phy=A.EXPECT_PHY)
    if ocv != 'complete':
        return _inc(pr, f'on-chip: {ocr}')
    onb = Q.q3_onchip_bins(ptext)
    onchip_med = onb[0]['med'] if onb else None
    tifs_us = (med - A.PREAMBLE_AA_OFF) / A.TICKS_PER_US

    pr(f'=== Q3 STEADY-STATE ({arm}) (METRICS ONLY; the runner owns the cell verdict) ===')
    pr(f'single plateau: n={npairs} median={med}t (tIFS~={tifs_us:.2f}us) vs expected {expect} +/-{tol} OK')
    pr(f'  shape {shape}; whole-cell retention C {ret_c*100:.1f}% P {ret_p*100:.1f}%')
    pr(f'  on-chip: exactly one {expect_us}us bin, med={onchip_med}us OK')
    pr(f'=== STEADY-METRICS-OK ({arm}: {expect_us}us plateau) ===')
    return dict(verdict='STEADY-METRICS-OK', arm=arm, median=med, expect=expect,
                onchip_med=onchip_med, n=npairs, ret_c=round(ret_c, 3), ret_p=round(ret_p, 3),
                shape=shape), 0


def q3_analyze(obs, cf, pf, calib_frozen=None, calib_path=None,
               guard_ticks=Q3_GUARD_TICKS, min_plateau=Q3_MIN_PLATEAU_PAIRS,
               deviation_reason=None, quiet=False):
    pr = (lambda *a: None) if quiet else print
    # CONTRACT INTEGRITY: guard/min-plateau MUST equal the frozen values on any
    # accepted path. Zero/negative are rejected outright; any deviation is allowed
    # ONLY with an explicit reason and can NEVER be METRICS-OK (forced quarantine).
    if guard_ticks <= 0 or min_plateau <= 0:
        return _inc(pr, f'guard_ticks={guard_ticks}/min_plateau={min_plateau} must be > 0')
    dev = []
    if guard_ticks != Q3_GUARD_TICKS:
        dev.append(f'guard_ticks {guard_ticks}!={Q3_GUARD_TICKS}')
    if min_plateau != Q3_MIN_PLATEAU_PAIRS:
        dev.append(f'min_plateau {min_plateau}!={Q3_MIN_PLATEAU_PAIRS}')
    if dev and not deviation_reason:
        return _inc(pr, f'contract deviation {dev} requires an explicit contract-deviation reason')
    o = A.parse_obs(obs)

    # (1) Q2 COMMON gates -- reused verbatim from the Q2 analyzer.
    ctx, creason = A.validate_common(o, cf, pf, pr)
    if ctx is None:
        return _inc(pr, f'q2-common: {creason}')
    conn_aa = ctx['conn']['aa']

    # (2) ANCHORS (tick + monotonicity + duration; structural already checked counts).
    cs, ce, Ts, Te = o['cs_tick'], o['ce_tick'], o['Ts'], o['Te']
    if cs is None or ce is None:
        return _inc(pr, 'CAPTURE-START/END missing tick= anchor')
    if Ts is None or Te is None or Ts >= Te:
        return _inc(pr, f'host anchors not monotonic (Ts={Ts} Te={Te})')
    dur_ticks = (ce - cs) & (WRAP - 1)
    if dur_ticks <= 0:
        return _inc(pr, 'timer anchors not monotonic')
    cap_ms = o['cfg'].get('cap_ms')
    tick_dur_ms = dur_ticks / (A.TICKS_PER_US * 1000.0)
    host_dur_ms = Te - Ts
    if cap_ms is None:
        return _inc(pr, 'observer declared no cap=')
    if abs(tick_dur_ms - cap_ms) > Q3_DUR_TOL_MS:
        return _inc(pr, f'tick duration {tick_dur_ms:.0f}ms != declared cap {cap_ms}ms')
    if abs(host_dur_ms - cap_ms) > Q3_DUR_TOL_MS:
        return _inc(pr, f'host duration {host_dur_ms}ms != cap {cap_ms}ms (short/underestimated)')

    # (3) CALIB -- a VALIDATED frozen 150us baseline is mandatory.
    if calib_frozen is None:
        if not calib_path:
            return _inc(pr, 'Q3 requires a validated --calib frozen baseline')
        calib_frozen, kreason = A.validate_calib(calib_path)
        if calib_frozen is None:
            return _inc(pr, f'calibration invalid: {kreason}')

    # (4) CONTROL-PLANE -- strict transaction + peer participation (rerun, not counted).
    ctext, ptext, otext = (open(p, errors='replace').read() for p in (cf, pf, obs))
    cpv, cpr = Q.q3_completeness(ctext)
    if cpv != 'complete':
        return _inc(pr, f'control-plane: {cpr}')
    ppv, ppr = Q.q3_peer_participation(ctext, ptext)
    if ppv != 'complete':
        return _inc(pr, f'peer-participation: {ppr}')
    req = Q._q3_fields([l for l in ctext.splitlines() if 'Q3FSU-REQ' in l][0])
    cdone = Q._q3_fields([l for l in ctext.splitlines() if 'Q3FSU-DONE role=C' in l][0])
    pdone = Q._q3_fields([l for l in ptext.splitlines() if 'Q3FSU-DONE role=P' in l][0])
    # PRESENCE-STRICT schema (an absent field must not slip through as None).
    for nm, d, keys in (('REQ', req, ('role', 'aa', 'sess', 'min', 'max', 'phys', 'types', 'rc', 'seq')),
                        ('central DONE', cdone, ('role', 'aa', 'sess', 'status', 'spacing', 'types', 'phys')),
                        ('periph DONE', pdone, ('role', 'aa', 'sess', 'status', 'spacing', 'types', 'phys'))):
        missing = [k for k in keys if d.get(k) is None]
        if missing:
            return _inc(pr, f'{nm} missing fields {missing}')
    # REGISTERED request parameters (this exact 150->100us / 1M / both-ACL arm).
    if req['min'] != Q3_REG_MIN or req['max'] != Q3_REG_MAX:
        return _inc(pr, f"request {req['min']}->{req['max']}us != registered {Q3_REG_MIN}->{Q3_REG_MAX}")
    if req['phys'] != Q3_REG_PHYS or req['types'] != Q3_REG_TYPES:
        return _inc(pr, f"request phys={req['phys']} types={req['types']} != registered "
                        f"{Q3_REG_PHYS}/{Q3_REG_TYPES}")
    # BIND to the observed connection: request AA == Q2 connection AA; request session
    # == BOTH bracketing snapshot sessions.
    if req['aa'] != conn_aa:
        return _inc(pr, f"request AA {req['aa']} != observed connection AA {conn_aa}")
    if not (req['sess'] == ctx['cw']['sess'] == ctx['pw']['sess']):
        return _inc(pr, f"request session {req['sess']} != snapshot sessions "
                        f"C={ctx['cw']['sess']} P={ctx['pw']['sess']}")
    req_h, cd_h, pd_h = req['_hostms'], cdone['_hostms'], pdone['_hostms']
    req_sess = req['sess']
    if None in (req_h, cd_h, pd_h):
        return _inc(pr, 'missing HOSTMS on REQ/central-DONE/periph-DONE')
    for name, h in (('REQ', req_h), ('central DONE', cd_h), ('periph DONE', pd_h)):
        if not (Ts <= h <= Te):
            return _inc(pr, f'{name} host {h} outside anchored capture [{Ts},{Te}]')

    # (5) ON-CHIP -- strict lifecycle + boundary slops (rerun, not counted).
    ocv, ocr = Q.q3_onchip_check(ptext, conn_aa, req_sess, [EXPECT_PRE_US, EXPECT_POST_US], expect_phy=A.EXPECT_PHY)
    if ocv != 'complete':
        return _inc(pr, f'on-chip: {ocr}')
    bsv, bsr, slops = Q.q3_boundary_slops(otext, ptext)
    if bsv != 'complete':
        return _inc(pr, f'boundary-slop: {bsr}')

    # (5b) F-time MID snapshots -- EXACTLY ONE per role (host needed to widen the
    # exclusion so MID is structurally inside it). Full identity/timing bind after the
    # window is built.
    anchors = [(Ts, cs), (Te, ce)]
    mids = {}
    for role, text in (('C', ctext), ('P', ptext)):
        ms = _phase_snaps(text, role)
        if len(ms) != 1 or ms[0].get('host') is None:
            return _inc(pr, f'phase-snapshot: {role} needs exactly one Q3PHASESNAP with HOSTMS (got {len(ms)})')
        mids[role] = ms[0]

    # (6) TRANSITION WINDOW -- from the EARLIEST of {REQ, MID_C, MID_P} through the later
    # completion (timestamps only), so both MID snapshots lie inside the exclusion.
    lo, hi, unc = A.q3_transition_window(anchors, req_h, [cd_h, pd_h], guard_ticks,
                                         extra_start_hosts=[mids['C']['host'], mids['P']['host']])
    if not (lo < hi):
        return _inc(pr, f'transition window not ordered (lo={lo:.0f} hi={hi:.0f})')
    if not (0 <= lo and hi <= dur_ticks):
        return _inc(pr, f'transition window [{lo:.0f},{hi:.0f}] outside capture [0,{dur_ticks}]')

    # (6a) BIND both MID snapshots: identity (aa/session/boottag/channel) + timing
    # (between START/END snaps + reltick+/-unc inside the window).
    obs_ch = ctx['cfg']['ch']
    mid_tx = {}
    for role, text, boottag in (('C', ctext, ctx['cr']['boottag']), ('P', ptext, ctx['prd']['boottag'])):
        s_host, e_host = _q2snap_host(text, role, 0), _q2snap_host(text, role, 1)
        mt, br = q3_bind_mid(text, role, conn_aa, req_sess, boottag, obs_ch, s_host, e_host, anchors, lo, hi)
        if mt is None:
            return _inc(pr, f'phase-snapshot bind: {br}')
        mid_tx[role] = mt

    good = [r for r in o['recs'] if r['crc'] == 1 and r['st'] == 0]
    pairs_list = A.find_pairs(good)
    # WHOLE-CELL retention (protocol gate 1): observer C->P pairs vs the controller-
    # owned denominators. Overcount (pairs>denom) is impossible on real evidence ->
    # REJECT; retention below RETENTION_MIN means the observer missed too much air to
    # trust the plateau medians. (Observer pair-RATE balance is only a diagnostic --
    # this instrument has a strong early-connection transient; phase-SPECIFIC retention
    # is done rigorously below from the bound F-time counter snapshots.)
    cN, pN = ctx['cN'], ctx['pN']
    npairs = len(pairs_list)
    if npairs > cN or npairs > pN:
        return _inc(pr, f'overcount: {npairs} pairs > denom (central tx={cN} periph tx={pN})')
    ret_c, ret_p = npairs / cN, npairs / pN
    if ret_c < A.RETENTION_MIN or ret_p < A.RETENTION_MIN:
        return _inc(pr, f'whole-cell retention below {A.RETENTION_MIN}: central {ret_c*100:.1f}% '
                        f'periph {ret_p*100:.1f}% ({npairs}/{cN}, {npairs}/{pN})')
    all_pairs, pre, post, excl = [], [], [], 0   # pre/post: (b_addr tick, span), time-ordered
    for p in pairs_list:
        a_end = A.q3_reltick(p['a']['end'], cs)
        b_addr = A.q3_reltick(p['b']['addr'], cs)
        all_pairs.append((b_addr, p['span']))
        if b_addr < lo:
            pre.append((b_addr, p['span']))
        elif a_end > hi:
            post.append((b_addr, p['span']))
        else:
            excl += 1
    if len(pre) < min_plateau:
        return _inc(pr, f'pre-plateau pairs {len(pre)} < min {min_plateau}')
    if len(post) < min_plateau:
        return _inc(pr, f'post-plateau pairs {len(post)} < min {min_plateau}')
    pre_spans = [s for _, s in pre]; post_spans = [s for _, s in post]

    # (6a) STABLE-PLATEAU SHAPE gate -- each plateau must be a single stable tIFS.
    shape = {}
    for name, spans in (('pre', pre_spans), ('post', post_spans)):
        okp, rp, shape[name] = _plateau_shape(spans)
        if not okp:
            return _inc(pr, f'{name}-plateau not stable: {rp}')
    # (6b) PHASE-BALANCE -- DIAGNOSTIC ONLY (reported, never gates): the instrument has
    # a strong early-connection pair-rate transient, so pre/post local rates differ even
    # with no loss. Phase-SPECIFIC retention is done from F-time counter snapshots below.
    rate_pre = _phase_rate([t for t, _ in pre]); rate_post = _phase_rate([t for t, _ in post])
    rate_imbal = (abs(rate_pre - rate_post) / max(rate_pre, rate_post)
                  if (rate_pre and rate_post) else None)

    # (6c) PHASE-SPECIFIC retention from the BOUND F-time counter snapshots (threshold
    # FROZEN = the established Q2 RETENTION_MIN).
    txs = {role: (_snap_tx(text, role, 0), mid_tx[role], _snap_tx(text, role, 1))
           for role, text in (('C', ctext), ('P', ptext))}
    prv, prr, phase_ret = q3_phase_retention(txs, len(pre), len(post))
    if prv != 'complete':
        return _inc(pr, f'phase-retention: {prr}')

    med_pre, med_post = st.median(pre_spans), st.median(post_spans)   # mean of two middles for even n
    # baseline anchor: the pre plateau MUST reproduce the frozen 150us gap_proxy.
    if abs(med_pre - calib_frozen) > Q3_BASELINE_TOL_TICKS:
        return _inc(pr, f'pre-plateau median {med_pre} != frozen baseline {calib_frozen} '
                        f'+/-{Q3_BASELINE_TOL_TICKS}')
    step = med_pre - med_post
    tifs_pre = (med_pre - A.PREAMBLE_AA_OFF) / A.TICKS_PER_US
    tifs_post = (med_post - A.PREAMBLE_AA_OFF) / A.TICKS_PER_US

    # cross-val (us): on-air step/16 vs on-chip PRE-POST bin-median delta (150-100 at 1M, 150-52 at 2M).
    onb = {b['tifs']: b for b in Q.q3_onchip_bins(ptext)}
    onchip_step = onb[EXPECT_PRE_US]['med'] - onb[EXPECT_POST_US]['med']
    xval_d = abs(step / A.TICKS_PER_US - onchip_step)
    xval_ok = xval_d <= Q3_XVAL_TOL_US

    # secondary INDEPENDENT change point (diagnostic; does NOT move the partition).
    cp_tick, _k = _changepoint(sorted(all_pairs), min_plateau)
    cp_inside = cp_tick is not None and lo <= cp_tick <= hi

    cls = classify_step(step)
    ct = q3_contract(guard_ticks, min_plateau)
    pr('=== Q3 STEP ANALYSIS (METRICS ONLY; the runner owns the cell verdict) ===')
    pr('Q3-CONTRACT ' + ' '.join(f'{k}={v}' for k, v in ct.items()))
    pr(f'anchors: [Ts={Ts}ms,cs={cs}]..[Te={Te}ms,ce={ce}] dur={dur_ticks}t (~{tick_dur_ms:.0f}ms) '
       f'cap={cap_ms}ms ; boundary slops {slops}')
    pr(f'transition window (reltick): lo={lo:.0f} hi={hi:.0f} unc=+/-{unc:.0f}t (REQ..later-DONE + guard {guard_ticks})')
    pr(f'whole-cell retention: central {ret_c*100:.1f}% periph {ret_p*100:.1f}% '
       f'({npairs} pairs; denom C={cN} P={pN}; >= {A.RETENTION_MIN*100:.0f}%)')
    pr(f'plateaus: pre={len(pre)} post={len(post)} excluded(straddle)={excl}')
    pr(f'  shape PRE {shape["pre"]} POST {shape["post"]} (IQR<={Q3_PLATEAU_IQR_MAX_TICKS} '
       f'drift<={Q3_PLATEAU_DRIFT_MAX_TICKS} conc(+/-{Q3_PLATEAU_CONC_EPS_TICKS})>={Q3_PLATEAU_CONC_FRAC}) OK')
    pr(f'  phase-retention (F-time snapshots, >= {Q3_PHASE_RETENTION_MIN} FROZEN): {phase_ret} OK')
    pr(f'  [diagnostic] phase-rate pre {rate_pre:.3e} vs post {rate_post:.3e} pairs/tick '
       f'imbalance {"n/a" if rate_imbal is None else f"{rate_imbal*100:.1f}%"} (NOT a gate -- early-connection transient)')
    pr(f'  PRE  median gap_proxy = {med_pre}t (tIFS~={tifs_pre:.2f}us)  [frozen baseline {calib_frozen} +/-{Q3_BASELINE_TOL_TICKS} OK]')
    pr(f'  POST median gap_proxy = {med_post}t (tIFS~={tifs_post:.2f}us)')
    pr(f'PRIMARY step = {step}t vs {Q3_EXPECT_STEP_TICKS}t : |d|={abs(step-Q3_EXPECT_STEP_TICKS)} '
       f'(<= {Q3_STEP_TOL}) class={cls}')
    pr(f'  cross-val: on-air {step/A.TICKS_PER_US:.2f}us vs on-chip {EXPECT_PRE_US}-{EXPECT_POST_US} delta {onchip_step}us '
       f'|d|={xval_d:.2f} (<= {Q3_XVAL_TOL_US}us) {"OK" if xval_ok else "DISAGREE"}')
    pr(f'  [secondary/diagnostic] independent change point ({Q3_CHANGEPOINT_METHOD}, '
       f'>= {min_plateau}/side) @ {None if cp_tick is None else round(cp_tick)}t inside window? {cp_inside}')

    common = dict(step=step, med_pre=med_pre, med_post=med_post, n_pre=len(pre), n_post=len(post),
                  ret_c=ret_c, ret_p=ret_p, onchip_step=onchip_step, xval_ok=xval_ok,
                  cp_inside=cp_inside, cls=cls, shape=shape, phase_ret=phase_ret,
                  phase_rate_imbal=(None if rate_imbal is None else round(rate_imbal, 4)))
    if dev:   # forced quarantine: an analysis under non-frozen knobs can never accept
        pr(f'=== CONTRACT-DEVIATION (forced quarantine): {dev} reason={deviation_reason!r} '
           f'-- would-be class={cls} ===')
        return dict(verdict='CONTRACT-DEVIATION', deviation=dev, reason=deviation_reason, **common), 3
    if cls == 'AGREEMENT' and xval_ok:
        pr('=== METRICS-OK: on-air step == 50us within tol AND on-chip agrees (cell still QUARANTINED) ===')
        return dict(verdict='METRICS-OK', **common), 0
    if cls == 'AGREEMENT':   # step agrees but on-chip cross-val disagrees
        pr('=== XVAL-DISAGREE: on-air step OK but on-chip cross-val out of tolerance -- investigate ===')
        return dict(verdict='XVAL-DISAGREE', **common), 3
    msg = {'REDUCTION-OFF-TARGET': 'clear reduction but off the registered 800t (NOT "no reduction")',
           'NO-REDUCTION': 'no on-air reduction observed (null)',
           'WRONG-DIRECTION': 'wrong-direction change (post spacing > pre)'}[cls]
    pr(f'=== {cls}: {msg} ===')
    return dict(verdict=cls, **common), 3


# ---------------- synthetic full-evidence generator + self-test ----------------
def _synth_q3(n_pre=60, n_post=90, pre_gap=3047, post_gap=2247, cs=1_000_000,
              Ts=1000, cap_ms=30000, req_h=9000, cd_h=9100, pd_h=9150,
              anchor_pre_tick=True, drop_periph_done=False, req_outside=False,
              short_capture=False, overlap=False, wrap=False, thin_pre=False,
              no_frozen=False, bad_done_sess=False, bad_lifecycle=False,
              dup_bin=False, xval_bad=False, low_retention=False, strip_fsu_fields=False,
              wrong_conn_aa=False, bad_min=False, alt_arm=False,
              jitter_post=False, drift_post=False, bimodal_post=False, phase_imbalance=False,
              post_phase_loss=False, drop_phasesnap=False, mid_dup=False,
              mid_wrong_aa=False, mid_wrong_sess=False, mid_out_window=False,
              bad_peer_initiator=False):
    import tempfile
    W = A.TIMER0_WRAP
    if wrap:
        cs = W - 240_000_000
    dur = cap_ms * A.TICKS_PER_US * 1000
    ce = (cs + dur) & (W - 1)
    Te = Ts + (cap_ms - 3000 if short_capture else cap_ms)
    tr = (req_h - Ts) * (dur / cap_ms)                # request reltick (analyzer's own map)
    recs, oseq = [], 0
    def emit(base_rel, gap):
        nonlocal oseq
        a_addr = (cs + int(base_rel)) & (W - 1); a_end = (a_addr + 1104) & (W - 1)
        b_addr = (a_end + gap) & (W - 1); b_end = (b_addr + 1104) & (W - 1)
        recs.append((oseq, a_addr, a_end)); oseq += 1
        recs.append((oseq, b_addr, b_end)); oseq += 1
    margin, step_ev = 4_000_000, 240_000
    if overlap:
        tot = n_pre + n_post
        for i in range(tot):
            emit(tr + (i - tot // 2) * 3000, pre_gap if i < n_pre else post_gap)
    else:
        for i in range(5 if thin_pre else n_pre):
            emit(tr - margin - (n_pre - i) * step_ev, pre_gap)
        pstep = step_ev * 2 if phase_imbalance else step_ev   # 2x spacing halves the post rate
        for i in range(n_post):
            if jitter_post:      g = post_gap + (12 if i % 2 else -12)     # wide IQR
            elif drift_post:     g = post_gap + i                         # monotonic trend
            elif bimodal_post:   g = post_gap + (50 if i % 7 == 0 else 0)  # ~14% second mode (IQR still 0)
            else:                g = post_gap
            emit(tr + margin + i * pstep, g)
    N = len(recs)
    obs = tempfile.mktemp(suffix='.txt')
    with open(obs, 'w') as f:
        f.write('HOSTMS 300 *** Booting ***\n')
        f.write(f'HOSTMS 500 mode=Q2 phy=1M AA=0x0000dead crcinit=0x00555555 ch=10 whiten=1 cap={cap_ms}ms\n')
        f.write(f'HOSTMS {Ts} CAPTURE-START Q2 ch10 cap={cap_ms}ms' + (f' tick={cs}' if anchor_pre_tick else '') + '\n')
        for o_, ad, en in recs:
            f.write(f'HOSTMS {Ts+10} REC oseq={o_} s0=0x01 len=0 crc=1 rssi=-50 pairid=0 '
                    f'txtag=0 st=0x00 addr={ad} end={en} air_us=0 gap_us=0\n')
        f.write(f'HOSTMS {Te} CAPTURE-END Q2 tick={ce}\n')
        if not no_frozen:
            f.write(f'HOSTMS {Te+5} FROZEN: records={N} ring_full_drops=0 max_isr_ticks=50\n')
        f.write(f'HOSTMS {Te+6} LOSS: addr_minus_end=0 end_minus_recorded=0 reversal=0 stale_addr=0 records={N}\n')
    cf, pf = tempfile.mktemp(suffix='.txt'), tempfile.mktemp(suffix='.txt')
    rq = (Te + 500) if req_outside else req_h
    npairs = N // 2
    pre_ct = 5 if thin_pre else n_pre                 # pre-plateau pairs (non-overlap)
    post_ct = n_post
    if post_phase_loss:                               # post retention < 0.95 while whole-cell >= 0.95
        pre_tx, post_tx = pre_ct, post_ct + 6
    elif low_retention:
        pre_tx, post_tx = pre_ct * 4, post_ct * 4     # whole-cell fails first
    else:
        pre_tx, post_tx = pre_ct, post_ct             # per-phase retention ~ 1.0
    tx0 = 1000; tx_mid = tx0 + pre_tx; tx1 = tx_mid + post_tx   # START / MID(F) / END
    aa_req = '0x0000beef' if wrong_conn_aa else '0x0000dead'
    # bad_min: REQ says 90 but DONE still 100 (INTERNALLY INCONSISTENT -> completeness
    # rejects). alt_arm: REQ + BOTH DONE all say 90 (internally consistent -> exercises
    # the registered-arm gate itself). Otherwise the registered 100us arm.
    rmin = 90 if (bad_min or alt_arm) else 100
    dspac = 90 if alt_arm else 100
    if strip_fsu_fields:   # AA/session/spacing/PHY/spacing-types absent on ALL three
        req_line = f'HOSTMS {rq} Q3FSU-REQ role=C min={rmin} max=150 rc=0 seq=1\n'
        cdone_line = f'HOSTMS {cd_h} Q3FSU-DONE role=C status=0x00 initiator=0\n'
        pdone_line = f'HOSTMS {pd_h} Q3FSU-DONE role=P status=0x00 initiator=2\n'
    else:
        dsess = 2 if bad_done_sess else 1
        req_line = (f'HOSTMS {rq} Q3FSU-REQ role=C aa={aa_req} sess=1 min={rmin} max=150 '
                    f'phys=0x1 types=0x3 rc=0 seq=1\n')
        cdone_line = (f'HOSTMS {cd_h} Q3FSU-DONE role=C aa={aa_req} sess={dsess} status=0x00 '
                      f'spacing={dspac} types=0x3 phys=0x1 initiator=0\n')
        pinit = 1 if bad_peer_initiator else 2   # 2=PEER (correct for the responder)
        pdone_line = (f'HOSTMS {pd_h} Q3FSU-DONE role=P aa={aa_req} sess=1 status=0x00 '
                      f'spacing={dspac} types=0x3 phys=0x1 initiator={pinit}\n')
    with open(cf, 'w') as f:
        f.write('HOSTMS 100 Q2READY role=C dev=1111111111111111 boottag=0x0000aaaa\n')
        f.write('HOSTMS 200 Q2CONN AA=0000dead CRCINIT=00555555 hop=7 chan_count=2 map=000c000000\n')
        f.write(f'HOSTMS 900 Q2SNAP role=C seq=0 boottag=0x0000aaaa aa=0x0000dead sess=1 ch=10 sched={tx0} tx={tx0}\n')
        m_host = (Te - 500) if mid_out_window else req_h        # after the window but within [START,END]
        m_aa = '0x0000beef' if mid_wrong_aa else '0x0000dead'
        m_sess = 2 if mid_wrong_sess else 1
        cmid = f'HOSTMS {m_host} Q3PHASESNAP role=C boottag=0x0000aaaa aa={m_aa} sess={m_sess} ch=10 sched={tx_mid} tx={tx_mid}\n'
        if not drop_phasesnap:
            f.write(cmid)
            if mid_dup:
                f.write(cmid)
        f.write(f'HOSTMS {Te+100} Q2SNAP role=C seq=1 boottag=0x0000aaaa aa=0x0000dead sess=1 ch=10 sched={tx1} tx={tx1}\n')
        f.write(req_line)
        f.write(cdone_line)
    with open(pf, 'w') as f:
        f.write('HOSTMS 100 Q2READY role=P dev=2222222222222222 boottag=0x0000bbbb\n')
        f.write(f'HOSTMS 900 Q2SNAP role=P seq=0 boottag=0x0000bbbb aa=0x0000dead sess=1 ch=10 sched={tx0} tx={tx0}\n')
        if not drop_phasesnap:
            f.write(f'HOSTMS {req_h} Q3PHASESNAP role=P boottag=0x0000bbbb aa=0x0000dead sess=1 ch=10 sched={tx_mid} tx={tx_mid}\n')
        f.write(f'HOSTMS {Te+100} Q2SNAP role=P seq=1 boottag=0x0000bbbb aa=0x0000dead sess=1 ch=10 sched={tx1} tx={tx1}\n')
        if not drop_periph_done:
            f.write(pdone_line)
        cl_h, fz_h = (31050, 1050) if bad_lifecycle else (1050, 31050)   # bad: freeze before clear
        f.write(f'HOSTMS {cl_h} TIFS-CLEARED role=P aa=0x0000dead sess=1 life=1\n')
        f.write(f'HOSTMS {fz_h} TIFS-FROZEN role=P aa=0x0000dead sess=1 life=1\n')
        m100 = 140 if xval_bad else 100
        f.write(f'HOSTMS {Te+101} TIFSBIN tifs=150 phy=1 n=100 nv=99 min=148 med=150 max=153 drop=0\n')
        f.write(f'HOSTMS {Te+101} TIFSBIN tifs=100 phy=1 n=100 nv=99 min={m100-2} med={m100} max={m100+3} drop=0\n')
        if dup_bin:
            f.write(f'HOSTMS {Te+101} TIFSBIN tifs=100 phy=1 n=50 nv=50 min=98 med=100 max=103 drop=0\n')
        f.write(f'HOSTMS {Te+102} TIFS-DRAIN-DONE role=P aa=0x0000dead sess=1 life=1\n')
    return obs, cf, pf


def _synth_steady(arm, n=60, cs=1_000_000, cap_ms=30000, wrong_median=False,
                  two_bins=False, f150_has_fsu=False, f100_fsu_in_window=False,
                  low_retention=False, stale_session=False, req_only_f150=False,
                  big_boundary_slop=False, insufficient_settle=False):
    """Full-evidence STEADY-STATE cell: one plateau (150us for f150 / 100us for f100),
    the arm's control plane, and exactly one on-chip bin. The f100 arm places its FSU
    completions >= Q3_STEADY_SETTLE_MIN_MS before CAPTURE-START (larger Ts gives the
    settle room); the on-chip lifecycle is emitted RELATIVE to the window edges so the
    boundary slops stay in [0, Q3_BOUNDARY_SLOP_MS]. Adversarial flags:
      stale_session      -- on-chip lifecycle session != snapshot/request session
      req_only_f150      -- f150 with a Q3FSU-REQ but no completion (any FSU token rejects)
      big_boundary_slop  -- TIFS-CLEARED lands > Q3_BOUNDARY_SLOP_MS after CAPTURE-START
      insufficient_settle-- f100 completions < Q3_STEADY_SETTLE_MIN_MS before the window."""
    import tempfile
    W = A.TIMER0_WRAP
    gap = 3047 if arm == 'f150' else 2247
    if wrong_median:
        gap += 20
    # f100 needs room BEFORE the window for the FSU settle; f150 has no pre-window FSU.
    Ts = 1000 if arm == 'f150' else (Q3_STEADY_SETTLE_MIN_MS + 1000)
    dur = cap_ms * A.TICKS_PER_US * 1000
    ce = (cs + dur) & (W - 1); Te = Ts + cap_ms
    # f100 FSU completion hosts (central ch, periph ph), both BEFORE the window.
    if f100_fsu_in_window:
        ch = ph = Te + 200                       # completes inside/after window -> rejected
    elif insufficient_settle:
        ch, ph = Ts - 600, Ts - 550              # settle ~550ms < min -> rejected
    else:
        ch, ph = 500, 550                        # settle = Ts - 550 >= min
    req_h = min(ch, ph) - 100                     # request precedes both completions
    # on-chip lifecycle hosts, RELATIVE to the window edges (bracket [START,END]).
    life_sess = 2 if stale_session else 1
    clear_h = (Ts + Q3_BOUNDARY_SLOP_MS + 500) if big_boundary_slop else (Ts + 50)
    freeze_h, drain_h = Te + 50, Te + 52
    recs, oseq = [], 0
    for i in range(n):
        base = 5_000_000 + i * 240_000
        a_addr = (cs + base) & (W - 1); a_end = (a_addr + 1104) & (W - 1)
        b_addr = (a_end + gap) & (W - 1); b_end = (b_addr + 1104) & (W - 1)
        recs.append((oseq, a_addr, a_end)); oseq += 1
        recs.append((oseq, b_addr, b_end)); oseq += 1
    N = len(recs); denom = n * 4 if low_retention else n
    tx0, tx1 = 1000, 1000 + denom
    obs = tempfile.mktemp(suffix='.txt')
    with open(obs, 'w') as f:
        f.write('HOSTMS 300 *** Booting ***\n')
        f.write(f'HOSTMS 500 mode=Q2 phy=1M AA=0x0000dead crcinit=0x00555555 ch=10 whiten=1 cap={cap_ms}ms\n')
        f.write(f'HOSTMS {Ts} CAPTURE-START Q2 ch10 cap={cap_ms}ms tick={cs}\n')
        for o_, ad, en in recs:
            f.write(f'HOSTMS {Ts+10} REC oseq={o_} s0=0x01 len=0 crc=1 rssi=-45 pairid=0 '
                    f'txtag=0 st=0x00 addr={ad} end={en} air_us=0 gap_us=0\n')
        f.write(f'HOSTMS {Te} CAPTURE-END Q2 tick={ce}\n')
        f.write(f'HOSTMS {Te+5} FROZEN: records={N} ring_full_drops=0 max_isr_ticks=50\n')
        f.write(f'HOSTMS {Te+6} LOSS: addr_minus_end=0 end_minus_recorded=0 reversal=0 stale_addr=0 records={N}\n')
    cf, pf = tempfile.mktemp(suffix='.txt'), tempfile.mktemp(suffix='.txt')
    with open(cf, 'w') as f:
        f.write('HOSTMS 100 Q2READY role=C dev=1111111111111111 boottag=0x0000aaaa\n')
        f.write('HOSTMS 200 Q2CONN AA=0000dead CRCINIT=00555555 hop=7 chan_count=2 map=000c000000\n')
        f.write(f'HOSTMS {Ts-100} Q2SNAP role=C seq=0 boottag=0x0000aaaa aa=0x0000dead sess=1 ch=10 sched={tx0} tx={tx0}\n')
        f.write(f'HOSTMS {Te+100} Q2SNAP role=C seq=1 boottag=0x0000aaaa aa=0x0000dead sess=1 ch=10 sched={tx1} tx={tx1}\n')
        if arm == 'f150':
            # a clean f150 has NO FSU token of any kind; the adversarial arms inject one.
            if f150_has_fsu:
                f.write('HOSTMS 400 Q3FSU-DONE role=C aa=0x0000dead sess=1 status=0x00 spacing=100 types=0x3 phys=0x1 initiator=0\n')
            elif req_only_f150:
                f.write('HOSTMS 400 Q3FSU-REQ role=C aa=0x0000dead sess=1 min=100 max=150 phys=0x1 types=0x3 rc=0 seq=1\n')
        else:
            f.write(f'HOSTMS {req_h} Q3FSU-REQ role=C aa=0x0000dead sess=1 min=100 max=150 phys=0x1 types=0x3 rc=0 seq=1\n')
            f.write(f'HOSTMS {ch} Q3FSU-DONE role=C aa=0x0000dead sess=1 status=0x00 spacing=100 types=0x3 phys=0x1 initiator=0\n')
    with open(pf, 'w') as f:
        f.write('HOSTMS 100 Q2READY role=P dev=2222222222222222 boottag=0x0000bbbb\n')
        f.write(f'HOSTMS {Ts-100} Q2SNAP role=P seq=0 boottag=0x0000bbbb aa=0x0000dead sess=1 ch=10 sched={tx0} tx={tx0}\n')
        f.write(f'HOSTMS {Te+100} Q2SNAP role=P seq=1 boottag=0x0000bbbb aa=0x0000dead sess=1 ch=10 sched={tx1} tx={tx1}\n')
        if arm == 'f100':
            f.write(f'HOSTMS {ph} Q3FSU-DONE role=P aa=0x0000dead sess=1 status=0x00 spacing=100 types=0x3 phys=0x1 initiator=2\n')
        f.write(f'HOSTMS {clear_h} TIFS-CLEARED role=P aa=0x0000dead sess={life_sess} life=1\n')
        f.write(f'HOSTMS {freeze_h} TIFS-FROZEN role=P aa=0x0000dead sess={life_sess} life=1\n')
        tifs, med = (150, 140) if arm == 'f150' else (100, 90)
        f.write(f'HOSTMS {Te+51} TIFSBIN tifs={tifs} phy=1 n=100 nv=100 min={med-2} med={med} max={med+3} drop=0\n')
        if two_bins:
            f.write(f'HOSTMS {Te+51} TIFSBIN tifs={100 if arm=="f150" else 150} phy=1 n=50 nv=50 min=88 med=90 max=93 drop=0\n')
        f.write(f'HOSTMS {drain_h} TIFS-DRAIN-DONE role=P aa=0x0000dead sess={life_sess} life=1\n')
    return obs, cf, pf


def _forged_calib():
    """A naked {status:ESTABLISHED} blob with no contract/inputs/lineage -- must be
    REJECTED by validate_calib (tampered calibration)."""
    import tempfile, json
    p = tempfile.mktemp(suffix='.json')
    json.dump({'status': 'ESTABLISHED', 'frozen_gap_proxy_dev': 3047}, open(p, 'w'))
    return p


def selftest():
    R = []
    def run(name, exp_rc, exp_verdict=None, calib=3047, guard=Q3_GUARD_TICKS,
            minp=Q3_MIN_PLATEAU_PAIRS, dev=None, **kw):
        obs, cf, pf = _synth_q3(**kw)
        r, rc = q3_analyze(obs, cf, pf, calib_frozen=calib, guard_ticks=guard,
                           min_plateau=minp, deviation_reason=dev, quiet=True)
        for x in (obs, cf, pf): os.unlink(x)
        ok = rc == exp_rc and (exp_verdict is None or r.get('verdict') == exp_verdict)
        R.append((name, ok, r))
    # happy paths
    run('clean mid-step -> METRICS-OK (step 800, xval OK)', 0, 'METRICS-OK')
    run('wraparound anchors -> METRICS-OK', 0, 'METRICS-OK', wrap=True)
    # anchor / duration / window
    run('malformed anchor (no tick=) -> INCOMPLETE', 3, 'INCOMPLETE', anchor_pre_tick=False)
    run('completion outside capture -> INCOMPLETE', 3, 'INCOMPLETE', req_outside=True)
    run('underestimated/short capture -> INCOMPLETE', 3, 'INCOMPLETE', short_capture=True)
    run('transition overlaps plateau -> INCOMPLETE', 3, 'INCOMPLETE', overlap=True)
    run('insufficient pre plateau -> INCOMPLETE', 3, 'INCOMPLETE', thin_pre=True)
    # stable-plateau SHAPE gate (calibrated from the six accepted Q2 cells)
    run('jittery post plateau (wide IQR) -> INCOMPLETE', 3, 'INCOMPLETE', jitter_post=True)
    run('drifting post plateau -> INCOMPLETE', 3, 'INCOMPLETE', drift_post=True)
    run('spread post plateau (IQR OK, concentration fails) -> INCOMPLETE', 3, 'INCOMPLETE', bimodal_post=True)
    # phase-rate imbalance must NOT gate (it is only a diagnostic now)
    run('phase-rate imbalance is NOT a gate -> METRICS-OK', 0, 'METRICS-OK', phase_imbalance=True)
    # phase-SPECIFIC retention (F-time snapshots) + MID identity/timing binding
    run('concentrated post-phase loss (whole-cell OK) -> INCOMPLETE', 3, 'INCOMPLETE', post_phase_loss=True)
    run('missing Q3PHASESNAP -> INCOMPLETE', 3, 'INCOMPLETE', drop_phasesnap=True)
    run('duplicate MID snapshot -> INCOMPLETE', 3, 'INCOMPLETE', mid_dup=True)
    run('MID wrong AA -> INCOMPLETE', 3, 'INCOMPLETE', mid_wrong_aa=True)
    run('MID wrong session -> INCOMPLETE', 3, 'INCOMPLETE', mid_wrong_sess=True)
    run('MID out of transition window -> INCOMPLETE', 3, 'INCOMPLETE', mid_out_window=True)
    # REGRESSION: the shape gate MUST accept all six pre-existing accepted Q2 captures
    import analyze_q2 as _A2
    six = ['symctl1', 'symctl2', 'nearfarA1', 'nearfarA2', 'nearfarB1', 'nearfarB2']
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'debug-evidence')
    for c in six:
        p = os.path.join(base, f'observer-q2-{c}-20260811', 'obs.txt')
        if not os.path.exists(p):
            R.append((f'shape replay {c}: capture present', False, {'reason': 'missing'})); continue
        o = _A2.parse_obs(p)
        spans = [pr_['span'] for pr_ in _A2.find_pairs([r for r in o['recs'] if r['crc'] == 1 and r['st'] == 0])]
        okp, rp, _sh = _plateau_shape(spans)
        R.append((f'shape gate ACCEPTS accepted Q2 cell {c} (real live-link)', okp, {'reason': rp}))
    # ADVERSARIAL (per review): each MUST NOT pass
    run('missing Q2 evidence (no FROZEN) -> INCOMPLETE q2-common', 3, 'INCOMPLETE', no_frozen=True)
    run('mismatched transaction (DONE sess!=REQ) -> INCOMPLETE control-plane', 3, 'INCOMPLETE', bad_done_sess=True)
    run('missing peer DONE -> INCOMPLETE peer', 3, 'INCOMPLETE', drop_periph_done=True)
    run('peripheral initiator!=PEER -> INCOMPLETE peer', 3, 'INCOMPLETE', bad_peer_initiator=True)
    run('invalid lifecycle (freeze<clear) -> INCOMPLETE on-chip', 3, 'INCOMPLETE', bad_lifecycle=True)
    run('duplicate on-chip bin -> INCOMPLETE on-chip', 3, 'INCOMPLETE', dup_bin=True)
    run('baseline mismatch (pre!=frozen) -> INCOMPLETE', 3, 'INCOMPLETE', pre_gap=3060)
    run('low whole-cell retention -> INCOMPLETE', 3, 'INCOMPLETE', low_retention=True)
    run('stripped FSU fields (None==None) -> INCOMPLETE', 3, 'INCOMPLETE', strip_fsu_fields=True)
    run('wrong connection AA (req!=Q2CONN) -> INCOMPLETE', 3, 'INCOMPLETE', wrong_conn_aa=True)
    run('inconsistent min (REQ 90, DONE 100) -> INCOMPLETE control-plane', 3, 'INCOMPLETE', bad_min=True)
    run('consistent alternate arm (all 90) -> INCOMPLETE registered-arm gate', 3, 'INCOMPLETE', alt_arm=True)
    # contract-integrity: overrides can never accept; zero/negative rejected
    run('guard override without reason -> INCOMPLETE', 3, 'INCOMPLETE', guard=999)
    run('guard override WITH reason -> CONTRACT-DEVIATION (never METRICS-OK)', 3,
        'CONTRACT-DEVIATION', guard=999, dev='smoke-widening')
    run('min-plateau override with reason -> CONTRACT-DEVIATION', 3,
        'CONTRACT-DEVIATION', minp=10, dev='smoke-small-n')
    run('zero guard -> INCOMPLETE', 3, 'INCOMPLETE', guard=0)
    run('negative min-plateau -> INCOMPLETE', 3, 'INCOMPLETE', minp=-5)
    # null / direction taxonomy -- NONE may be METRICS-OK
    run('zero step (null) -> NO-REDUCTION', 3, 'NO-REDUCTION', post_gap=3047)
    run('wrong-direction (post>pre) -> WRONG-DIRECTION', 3, 'WRONG-DIRECTION', post_gap=3200)
    run('clear reduction off target -> REDUCTION-OFF-TARGET', 3, 'REDUCTION-OFF-TARGET', post_gap=2200)
    run('step OK but on-chip disagrees -> XVAL-DISAGREE', 3, 'XVAL-DISAGREE', xval_bad=True)
    # tampered calibration (validate_calib path)
    fc = _forged_calib()
    obs, cf, pf = _synth_q3()
    r, rc = q3_analyze(obs, cf, pf, calib_path=fc, quiet=True)
    for x in (obs, cf, pf, fc): os.unlink(x)
    R.append(('tampered calibration -> INCOMPLETE (validate_calib rejects)',
              rc == 3 and r.get('verdict') == 'INCOMPLETE' and 'calibration' in r.get('reason', ''), r))
    # missing calib entirely
    obs, cf, pf = _synth_q3()
    r, rc = q3_analyze(obs, cf, pf, quiet=True)
    for x in (obs, cf, pf): os.unlink(x)
    R.append(('no --calib -> INCOMPLETE', rc == 3 and r.get('verdict') == 'INCOMPLETE', r))

    # ---- STEADY-STATE (ABBA arm) analyzer ----
    def runs(name, arm, exp_rc, exp_v=None, **kw):
        obs, cf, pf = _synth_steady(arm, **kw)
        r, rc = q3_steady_analyze(obs, cf, pf, arm, calib_frozen=3047, quiet=True)
        for x in (obs, cf, pf): os.unlink(x)
        R.append((name, rc == exp_rc and (exp_v is None or r.get('verdict') == exp_v), r))
    runs('steady f150 clean -> STEADY-METRICS-OK', 'f150', 0, 'STEADY-METRICS-OK')
    runs('steady f100 clean -> STEADY-METRICS-OK', 'f100', 0, 'STEADY-METRICS-OK')
    runs('steady f150 wrong median -> INCOMPLETE', 'f150', 3, 'INCOMPLETE', wrong_median=True)
    runs('steady f100 wrong median -> INCOMPLETE', 'f100', 3, 'INCOMPLETE', wrong_median=True)
    runs('steady f150 with unexpected FSU -> INCOMPLETE', 'f150', 3, 'INCOMPLETE', f150_has_fsu=True)
    runs('steady f100 FSU not before window -> INCOMPLETE', 'f100', 3, 'INCOMPLETE', f100_fsu_in_window=True)
    runs('steady f150 two on-chip bins -> INCOMPLETE', 'f150', 3, 'INCOMPLETE', two_bins=True)
    runs('steady f100 low retention -> INCOMPLETE', 'f100', 3, 'INCOMPLETE', low_retention=True)
    # hardened adversarial arms (stale session / request-only f150 / boundary slop / settle)
    runs('steady f100 stale on-chip session -> INCOMPLETE', 'f100', 3, 'INCOMPLETE', stale_session=True)
    runs('steady f150 stale on-chip session -> INCOMPLETE', 'f150', 3, 'INCOMPLETE', stale_session=True)
    runs('steady f150 request-only (no completion) -> INCOMPLETE', 'f150', 3, 'INCOMPLETE', req_only_f150=True)
    runs('steady f100 excessive boundary slop -> INCOMPLETE', 'f100', 3, 'INCOMPLETE', big_boundary_slop=True)
    runs('steady f150 excessive boundary slop -> INCOMPLETE', 'f150', 3, 'INCOMPLETE', big_boundary_slop=True)
    runs('steady f100 insufficient settle -> INCOMPLETE', 'f100', 3, 'INCOMPLETE', insufficient_settle=True)
    R.append(('steady bad arm -> INCOMPLETE',
              q3_steady_analyze(*_synth_steady('f150'), 'f999', calib_frozen=3047, quiet=True)[1] == 3, {}))

    print('=== Q3 ANALYZER SELFTEST ===')
    ok = all(v for _, v, _ in R)
    for n, v, r in R:
        print(f'  [{"PASS" if v else "FAIL"}] {n}' + ('' if v else f'  (got {r.get("verdict")}: {r.get("reason","")})'))
    print(f'Q3 ANALYZER SELFTEST: {"PASS" if ok else "FAIL"}')
    return 0 if ok else 1


if __name__ == '__main__':
    a = sys.argv[1:]
    if a and a[0] == '--selftest':
        sys.exit(selftest())
    # steady-state (ABBA confirmation arm) dispatch: --steady <f150|f100>
    steady_arm = None
    if '--steady' in a:
        i = a.index('--steady'); steady_arm = a[i + 1]; del a[i:i + 2]
    kw = {}
    for flag, key, conv in (('--calib', 'calib_path', str), ('--guard-ticks', 'guard_ticks', int),
                            ('--min-plateau', 'min_plateau', int),
                            ('--contract-deviation', 'deviation_reason', str)):  # NO --step-tol: +/-4 immutable
        if flag in a:
            i = a.index(flag); kw[key] = conv(a[i + 1]); del a[i:i + 2]
    if len(a) != 3:
        print(__doc__); sys.exit(2)
    if steady_arm is not None:
        # steady analyzer takes no guard/min-plateau/deviation (single plateau, no window)
        _r, rc = q3_steady_analyze(a[0], a[1], a[2], steady_arm, calib_path=kw.get('calib_path'))
    else:
        _r, rc = q3_analyze(a[0], a[1], a[2], **kw)
    sys.exit(rc)
