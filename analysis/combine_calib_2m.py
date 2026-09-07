#!/usr/bin/env python3
"""combine_calib driver at the 2M gap_proxy offset (+383). Sets the analyze_q2 globals to
2M (via analyze_q2_2m.apply_2m) BEFORE delegating to combine_calib.combine, so both the
per-cell re-analysis (A.analyze) and the reported calibrated_tifs_us use the 2M offset.
combine_calib itself is PHY-agnostic (homogeneity + provenance only); only the offset and
the per-cell PHY==2 gate change. Produces a provenance-bound gap-proxy-calibration-2m.json
(frozen ~2791 t = the rig's Q2-established 150.50us at 2M) for the Q3-2M --calib.

Usage: combine_calib_2m.py --out gap-proxy-calibration-2m.json <cell1> <cell2> [<cell3> ...]
"""
import sys
import analyze_q2_2m as A2
import combine_calib as C

A2.apply_2m()

a = sys.argv[1:]
out = 'gap-proxy-calibration-2m.json'
if '--out' in a:
    i = a.index('--out'); out = a[i + 1]; del a[i:i + 2]
if not a:
    print(C.__doc__); sys.exit(2)
sys.exit(C.combine(a, out))
