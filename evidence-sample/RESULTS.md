# Q3a FSU on-air verification — collection results (2026-08-12)

Controller: open Zephyr M0 FSU (fsu-m0 HEAD 9999e040, PR-99473 reference import).
Endpoints nRF54L15; observer nRF52832. Calib gap-proxy-calibration-q3-r2 (frozen 3047).
Outputs were ORIGINALLY collected outside the repo and COPIED into this durable archive
(byte-for-byte verified; see SHA256SUMS); every attempt is preserved.

## 2M/52 µs — also formally accepted (2026-09-07)

The 2M step (150→52 µs) later passed the **same protocol** as the 1M step below: **3 primary mid-step
AGREEMENT cells** (`|d|≤1`, on-air==on-chip `|d|≤0.06`) + a **4-cell ABBA-CONFIRMED** (drift-cancelled
step 1567.75 t, `|d|=0.25`), all non-smoke / clean-tree / provenance-bound, via the ported 2M tooling
in `analysis/` (`analyze_q2_2m`, `assert_fsu_config_2m`, `combine_calib_2m`, `combine_abba_2m`; frozen
2M calib 2791 t = the Q2-established 150.50 µs). Full evidence + firmware live in the benchmark repo:
`debug-evidence/q3-2m-accept-20260906/`. Still observer-based, not independently pro-analyzer qualified.

## Claim hierarchy (1M, 150→100 µs)

### PRIMARY — two accepted, reset-isolated within-connection 150→100 µs reductions (rev-5 firmware)
| cell | RF step | on-chip | cross-val | retention | AA |
|---|---|---|---|---|---|
| midstep-1 | 802t (\|d\|=2) | 150+100 bins | 0.12 µs | 97.6/98.8% | 0x106ba7e2 |
| midstep-2 | 800.0t (\|d\|=0) | 150+100 bins | 0.00 µs | 97.7/98.1% | 0xd816a44c |

verdict=ACCEPT (promoted path: firmware snapshot valid + re-verified, clean tree, all legs).

### CONFIRMATION — fresh rev-6 ABBA block b2 (event-gated; instrumented host app, SAME controller)
Campaign ABBA-fsu-m0-9999e040-rev6-20260812-b2, one host boot, order f150/f100/f100/f150.
medians [3047, 2249, 2249, 3049] → steps (798, 800) →
**drift-cancelled estimate ((A1−B1)+(A2−B2))/2 = 799.0t (~49.94 µs), |d|=1.0 ≤ 4 → ABBA-CONFIRMED.**
All four ACCEPT; distinct AAs; identical final conn params (interval=40 units = 50 ms); combine_abba
passed campaign + homogeneity + current-lineage + firmware + final-params gates.

### LIMITATION (disclosed) — negotiated FSU is not preserved across a connection-parameter update
Root cause of the FIRST ABBA block (b1) failure. The peripheral's automatic ~5 s
connection-parameter update (BT_GAP_AUTO_UPDATE_CONN_PARAMS; pref 30–50 ms ≠ central's
7.5 ms) runs ull_conn_update_parameters(), which resets tifs_tx/rx AND per-PHY
fsu_min/max to the 150 µs default (ull_conn.c:2380). An FSU negotiated before that update
is silently reverted; after it, it persists.

A/B control (quarantined, for the upstream bug report), identical early-FSU timing:
- auto-update **ON**  (block b1):   RF **3047t / 150.5 µs**, on-chip **150** (n=600)  — reverted.
- auto-update **OFF** (control):     RF **2247t / 100.5 µs**, on-chip **100** (n=4005) — persists.
- source mechanism: ull_conn_update_parameters() resets the effective FSU state.

## Disposition
- **Six conclusion-bearing accepted manifests**: the 2 primary (`primary-rev5/`) + the 4
  confirmation (`confirmation-rev6/`) cells. NOTE the archive also contains a SEVENTH
  `verdict=ACCEPT` manifest — `failed-block-b1/abba-0-f150/` — which individually passed
  before block b1 failed at seq1; it belongs to a FAILED campaign and is EXCLUDED from the
  conclusion (never reused).
- Failed ABBA block b1 preserved under `failed-block-b1/` (with `WHY-FAILED.md`; never reused).
- Evidence tiers are separate, never pooled: rev-5 primary (old app), rev-6 ABBA
  (app adds le_param_updated record; FSU controller + radio-path instrumentation identical).
- Option C (fix the controller to preserve FSU across updates) is a later upstream-fix
  campaign; if done, rerun all six cells under corrected firmware.
