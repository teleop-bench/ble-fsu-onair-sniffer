#!/usr/bin/env python3
"""Q3-2M FSU on-air analyzer -- 2M, 150->52 us (step 1568 t). A DISTINCT contract from
analyze_q3.py (1M, 150->100, step 800): see Q3-2M-ACCEPTANCE-PROTOCOL.md (frozen).

It does NOT mutate the 1M analyzer. It reuses the frozen change-point / plateau /
retention / binding / cross-val machinery of analyze_q3.py, but rebinds the calibration
and registered-change constants to their 2M values BEFORE delegating:

  gap_proxy = tIFS*16 + 383            (2M offset; 1M is +639)
  baseline 150us = 150*16 + 383 = 2783 t     post 52us = 52*16 + 383 = 1215 t
  registered step 150->52 = 98*16   = 1568 t
  on-chip cross-val bins            = {150, 52}
  registered FSU spacing            = [52..52]

Observer firmware for the live 2M capture MUST be built -DPHY2M=1 with AIRTIME_MIN_TICKS
lowered to 256 (a 2M empty PDU ADDRESS->END span ~= 320 t; the 1M 500 t floor would drop
empties and break the per-tIFS gap chain). That is a firmware -D, not an analyzer knob.

UNEXERCISED against real 2M FSU data. The self-test below only checks constant
consistency; a functional test requires 2M records + on-chip bins.
"""
import sys
import analyze_q2 as A
import analyze_q3 as Q3

# ---- 2M calibration constants (frozen; see protocol S1) --------------------
PREAMBLE_AA_OFF_2M   = 383
FROZEN_BASELINE_2M   = 150 * A.TICKS_PER_US + PREAMBLE_AA_OFF_2M   # 2783
FROZEN_POST_2M       = 52 * A.TICKS_PER_US + PREAMBLE_AA_OFF_2M    # 1215
EXPECT_STEP_2M       = (150 - 52) * A.TICKS_PER_US                 # 1568

def _apply_2m_overrides():
    """Rebind analyze_q2 / analyze_q3 module globals to 2M values. Q3's functions read
    these as module globals at call time (Python late binding), so overriding here makes
    the frozen 1M machinery evaluate the 2M contract. Idempotent."""
    A.PREAMBLE_AA_OFF        = PREAMBLE_AA_OFF_2M
    Q3.Q3_EXPECT_STEP_TICKS  = EXPECT_STEP_2M
    Q3.Q3_ABBA_EXPECT_STEP   = EXPECT_STEP_2M
    Q3.Q3_REG_MIN, Q3.Q3_REG_MAX = 52, 52
    Q3.EXPECT_PRE_US, Q3.EXPECT_POST_US = 150, 52
    # step_tol (4), baseline_tol (2), null_threshold (64), IQR (16), phys/types (0x3/0x3)
    # are PHY-independent and stay frozen at their analyze_q3 values.

def q3_2m_analyze(obs, cf, pf, calib_frozen=FROZEN_BASELINE_2M, **kw):
    _apply_2m_overrides()
    return Q3.q3_analyze(obs, cf, pf, calib_frozen=calib_frozen, **kw)

def q3_2m_steady_analyze(obs, cf, pf, arm, calib_frozen=FROZEN_BASELINE_2M, **kw):
    _apply_2m_overrides()
    return Q3.q3_steady_analyze(obs, cf, pf, arm, calib_frozen=calib_frozen, **kw)

def _selftest():
    _apply_2m_overrides()
    ok = []
    ok.append(('baseline 2783',   FROZEN_BASELINE_2M == 2783))
    ok.append(('post 1215',       FROZEN_POST_2M == 1215))
    ok.append(('step 1568',       EXPECT_STEP_2M == 1568))
    ok.append(('step = base-post', FROZEN_BASELINE_2M - FROZEN_POST_2M == EXPECT_STEP_2M))
    ok.append(('Q3 sees step',    Q3.Q3_EXPECT_STEP_TICKS == 1568))
    ok.append(('Q3 sees offset',  A.PREAMBLE_AA_OFF == 383))
    ok.append(('Q3 bins 150/52',  (Q3.EXPECT_PRE_US, Q3.EXPECT_POST_US) == (150, 52)))
    ok.append(('classify AGREE',   Q3.classify_step(1568) == 'AGREEMENT'))
    ok.append(('classify NO-RED',  Q3.classify_step(10) == 'NO-REDUCTION'))
    ok.append(('classify WRONG',   Q3.classify_step(-800) == 'WRONG-DIRECTION'))
    ok.append(('900 != AGREEMENT', Q3.classify_step(900) != 'AGREEMENT'))   # reduction, wrong magnitude
    bad = [n for n, v in ok if not v]
    for n, v in ok:
        print(f'  {"OK " if v else "FAIL"} {n}')
    print('SELFTEST', 'PASS' if not bad else f'FAIL {bad}')
    return 0 if not bad else 1

if __name__ == '__main__':
    a = sys.argv[1:]
    if not a or a[0] == '--selftest':
        sys.exit(_selftest())
    # CLI mirrors analyze_q3.py: [--steady f150|f52] [--calib PATH] obs cf pf
    steady_arm = None
    kw = {}
    if a and a[0] == '--steady':
        steady_arm = a[1]; a = a[2:]
    i = 0
    while i < len(a) and a[i].startswith('--'):
        if a[i] == '--calib':
            kw['calib_path'] = a[i + 1]; i += 2
        else:
            i += 2
    pos = a[i:]
    if steady_arm:
        _r, rc = q3_2m_steady_analyze(pos[0], pos[1], pos[2], steady_arm, **kw)
    else:
        _r, rc = q3_2m_analyze(pos[0], pos[1], pos[2], **kw)
    sys.exit(rc)
