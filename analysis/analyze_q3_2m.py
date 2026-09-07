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
    Q3.Q3_ABBA_ARM_SEQ       = ('f150', 'f52', 'f52', 'f150')   # 2M ABBA order (1M was f150/f100)
    Q3.Q3_REG_MIN, Q3.Q3_REG_MAX = 52, 150   # request integrity: matches fsu-f52.conf [52..150]
    Q3.Q3_REG_PHYS = 0x2                      # 2M mask (firmware .phys = PHY_2M_MASK); 1M arm used 0x1
    Q3.EXPECT_PRE_US, Q3.EXPECT_POST_US = 150, 52
    A.EXPECT_PHY = 2             # the link IS 2M for the 2M FSU arm (config-binding gate)
    # step_tol (4), baseline_tol (2), null_threshold (64), IQR (16), phys/types (0x3/0x3)
    # are PHY-independent and stay frozen at their analyze_q3 values.

def _calib_default(calib_frozen, kw):
    # Use the built-in 2783 default ONLY when no provenance-bound --calib is given.
    # (A non-None default would shadow calib_path, so q3_analyze would never load it.)
    if calib_frozen is None and not kw.get('calib_path'):
        return FROZEN_BASELINE_2M
    return calib_frozen

def q3_2m_analyze(obs, cf, pf, calib_frozen=None, **kw):
    _apply_2m_overrides()
    return Q3.q3_analyze(obs, cf, pf, calib_frozen=_calib_default(calib_frozen, kw), **kw)

def q3_2m_steady_analyze(obs, cf, pf, arm, calib_frozen=None, **kw):
    _apply_2m_overrides()
    return Q3.q3_steady_analyze(obs, cf, pf, arm, calib_frozen=_calib_default(calib_frozen, kw), **kw)

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
    # CLI mirrors analyze_q3.py: obs cf pf with --steady/--calib in ANY position.
    # (The old flags-must-precede-positionals parser silently dropped a trailing
    # --calib -- exactly how the runner passes it -- so the calib never loaded.)
    steady_arm = None
    kw = {}
    pos = []
    i = 0
    while i < len(a):
        if a[i] == '--steady':
            steady_arm = a[i + 1]; i += 2
        elif a[i] == '--calib':
            kw['calib_path'] = a[i + 1]; i += 2
        elif a[i].startswith('--'):
            i += 2                        # skip unknown flag+value (guard-ticks/min-plateau = frozen defaults)
        else:
            pos.append(a[i]); i += 1
    if steady_arm:
        _r, rc = q3_2m_steady_analyze(pos[0], pos[1], pos[2], steady_arm, **kw)
    else:
        _r, rc = q3_2m_analyze(pos[0], pos[1], pos[2], **kw)
    sys.exit(rc)
