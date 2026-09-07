#!/usr/bin/env python3
"""Q2-2M calibration analyzer -- plain Q2 (no FSU) at the 2M PHY. A thin 2M rebind of
analyze_q2.py, mirroring how analyze_q3_2m rebinds analyze_q3: the raw-radio gap_proxy
offset is +383 at 2M (1M is +639), so the 150us baseline lands at 150*16 + 383 = 2783 t
(vs 3039 t at 1M). It does NOT mutate the 1M analyzer -- it sets the module globals, then
delegates to A.analyze (the same callable the __main__ path and combine_calib use).

Purpose: analyze plain-Q2 2M symmetric CONTROL cells (central built with fsu-f52 but no F
sent -> a 150us FSU-off baseline) so they ACCEPT at 2M, then combine_calib_2m freezes a
provenance-bound gap-proxy-calibration-2m.json (~2791 t = the rig's Q2-established 150.50us
at the 2M offset). The Q3-2M mid-step baseline gate then compares against that rig baseline
instead of the idealized 2783 t default.
"""
import sys
import analyze_q2 as A

PREAMBLE_AA_OFF_2M = 383


def apply_2m():
    """Rebind analyze_q2 globals to 2M. Idempotent. TIFS_TOL, retention, and the near/far
    RSSI bands are PHY-independent and stay at their analyze_q2 values."""
    A.EXPECT_PHY = 2
    A.PREAMBLE_AA_OFF = PREAMBLE_AA_OFF_2M
    A.TIFS_EXPECT = 150 * A.TICKS_PER_US + PREAMBLE_AA_OFF_2M   # 2783


if __name__ == '__main__':
    apply_2m()
    a = sys.argv[1:]
    if a and a[0] == '--selftest':
        sys.exit(A.selftest())
    mode = A._take(a, '--mode') or 'nearfar'
    near = A._take(a, '--near') or 'central'
    calib = A._take(a, '--calib')
    if mode not in ('symmetric', 'nearfar'):
        print(f'REJECT: --mode must be symmetric|nearfar (got {mode})'); sys.exit(2)
    if near not in ('central', 'periph'):
        print(f'REJECT: --near must be central|periph (got {near})'); sys.exit(2)
    tifs_dev = A.load_calib(calib) if calib is not None else None
    if len(a) != 3:
        print(A.__doc__); sys.exit(2)
    sys.exit(A.analyze(a[0], a[1], a[2], mode, near, tifs_dev))
