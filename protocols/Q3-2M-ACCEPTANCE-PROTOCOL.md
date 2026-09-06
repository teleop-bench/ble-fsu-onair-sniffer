# Q3-2M acceptance protocol — REV 3 (amended 2026-08-13, BEFORE any ACCEPTED 2M cell)

A **distinct** contract from the 1M Q3 (`analyze_q3.py`, 150→100 µs, step 800 t). This
freezes the gates for the **2M, 150→52 µs** on-air FSU verification, whose purpose is to
**resolve the goodput-run ambiguity**: at 2M/50 ms the FSU host callback reported
`spacing=52 us` but the throughput rig saw no effect (completion-gap unchanged). Only an
independent on-air instrument can say whether tIFS physically dropped at 2M.

The already-qualified 1M END-capture instrument and the 1M Q3 contract are **untouched**.

**Amendment history (all before any ACCEPTED 2M cell):**
- **rev-1 → rev-2**: de-risking pass driven by the pitfalls that actually bit the 2026-08
  CoC/FSU work. Added, all *before* data: pre-flight assertions that fail fast (§2.5); the
  bench-shim on/off discriminator (§1); an explicit note that the frozen baseline gate
  doubles as the 2M-`AIRTIME_MIN`/empty-capture validity check (§3); two falsifiability
  controls the pipeline must pass (§5.5); "the pilot MEASURES capacity, never assume it"
  (§4); and the console/hygiene gate (§6.5). No change to the calibration constants or the
  outcome taxonomy — only extra guards.
- **rev-2 → rev-3**: driven by the 2026-08-13 QUARANTINED smoke
  (`debug-evidence/q3-2m-smoke-20260813/`), which validated the pipeline (both legs
  150→52 µs, matching frozen 2783/1215, step 1568) but was low-rate and used `--smoke`
  bypasses. §9 (new) freezes the **promotion path to an ACCEPTED cell + the objective
  measurement**, all before accepted data: the merged saturated same-session central; the
  paired-goodput ± FSU design (the observer makes a NULL goodput result interpretable);
  PORT-not-bypass of the three 1M-frozen gates (the `f52` arm, an `assert_fsu_config_2m`
  contract, a provenance-bound 2M `--calib`); a no-bypass audit; pilot-stability-first at
  2M/50 ms saturated; and the two firmware fixes the smoke found, now REQUIRED. No change
  to §1 calibration constants (2783/1215/1568/383) or the §5 taxonomy.

## 0. Instrument is already qualified at 2M (do NOT re-validate synthetically)

Q1 accepted the observer at 2M against the generator: 52/70/100/150 µs, both generator
DKs, 2 reset-isolated reps, medians **2783 / 1983 / 1503 / 1215 t** — exactly one tick
from commanded, exact rung deltas, 98.5–100 % retention
(`debug-evidence/observer-q1-20260810/`). That qualification uses observer
**EVENTS_END → CC[2]** (END, not PHYEND). This protocol **keeps END** — moving to PHYEND
would be a new instrument and void the calibration.

## 1. Calibration (2M) — frozen

- `gap_proxy = next.ADDRESS − prev.END = tIFS·16 + 383` (2M offset; 1M is +639).
- Baseline `frozen_gap_proxy_2m(150 µs) = 150·16 + 383 = 2783 t` (matches Q1-2M median).
- Post plateau `52 µs = 52·16 + 383 = 1215 t` (matches Q1-2M median).
- **Registered step 150→52 µs = 98·16 = 1568 t.** (`Q3_2M_EXPECT_STEP_TICKS = 1568`.)
- `TICKS_PER_US = 16`, `PREAMBLE_AA_OFF_2M = 383`.

## 2. Registered change + endpoint config (frozen)

- FSU request **[52..52] µs**, `phys=0x3` (1M+2M), `types=0x3` (ACL both dirs) —
  `reg_min=52, reg_max=52, reg_phys=0x3, reg_types=0x3`. (The analyzer MUST reject a
  capture whose on-chip `FSU: updated` spacing ≠ 52 or phys/types ≠ registered.)
- **On-chip cross-val bins: 150 and 52 µs.** `xval = |on_air_step/16 − (onchip150−onchip52)| ≤ 8 µs`.
- **Same-session requirement (the whole point).** The capture MUST watch the *exact*
  throughput session/firmware/config whose ambiguity is being resolved (2M / 50 ms /
  FSU-52 blast, `fill2-cen-fsu` ↔ `fsu-per-app` build3), bound by AA / CRCInit / channel /
  map / PHY. A separate low-rate 2M link would prove 2M capability, NOT that 52 µs was
  active in the throughput run — that capture is INADMISSIBLE for this claim.
- **FSU-revert guard.** Per Q3a, an automatic connection-parameter update reverts a
  negotiated FSU (`ull_conn_update_parameters` resets tIFS + per-PHY fsu_min/max to 150).
  The cell is INVALID unless either (a) central and peripheral preferred intervals match
  so no update fires, or (b) FSU is (re)requested AFTER the last conn-param update; the
  manifest must record final conn params identical across all legs.
- **Bench-shim discriminator (the prime suspect).** The current build enables
  `CONFIG_BT_CTLR_FSU_BENCH_FORCE_FEAT=y`, which forces the FSU feature bit *with no real
  page exchange* — a leading explanation for "host reports `spacing=52`, on air unchanged."
  Therefore run the observer against BOTH configurations and let it arbitrate:
  (i) **shim ON** (current), and (ii) **shim OFF** = real LL feature negotiation, if the
  peer advertises FSU support. Record which configuration physically moves tIFS on air.
  A shim-ON "52" that the observer shows as still-150 is a NEGATIVE result for physical
  application, not an instrument failure. Each configuration is its own set of cells;
  never pool shim-ON and shim-OFF captures.

## 2.5 Pre-flight assertions — fail FAST, before any capture is trusted

Each of these caught us the hard way in 2026-08; the runner MUST assert them and abort the
cell (INVALID-CONFIG) rather than capture-then-discover:

1. **Both endpoints carry the IFS clamp.** `CONFIG_BT_CTLR_EVENT_IFS_LOW_LAT_US=52` in the
   *resolved* `.config` of BOTH central and peripheral (FSU floors at 150 if the periph
   lacks it — not just the central).
2. **Right connection.** Observer `-DQ2_AA` / `-DQ2_CRCINIT` equal the central's `Q2CONN
   AA=… CRCINIT=…` line for THIS session; mismatch ⇒ abort (else timing noise / a
   different link).
3. **Same-session + saturated.** The central's `BLASTC comp=` counter is climbing
   *throughout* the capture window (a quiet or low-rate 2M link is inadmissible for this
   claim). Record comp-rate over the window.
4. **FSU actually negotiated, this session, post-update.** An `FSU: updated status=0x00
   spacing=52 us phys=0x3 types=0x3` line appears AFTER the last `conn param update`, and
   final conn params are identical across all legs. No such line ⇒ INVALID.
5. **Observer image is the 2M build.** `-DPHY2M=1` and `AIRTIME_MIN_TICKS=256` in the
   observer's resolved config; `observer_image_verified=TRUE` (not `--skip-obs-flash`).
6. **DTR/console live** (see §6.5): the observer's `CAPTURE-END … tick=` line was received
   intact; a missing/garbled dump ⇒ INCOMPLETE, never a partial parse.

## 3. AIRTIME gate (2M) — recomputed

A 2M **empty** PDU ADDRESS→END span ≈ (2 hdr + 0 + 3 CRC) B × 4 µs = 20 µs = **320 t**.
The 1M `AIRTIME_MIN_TICKS=500` would DROP 2M empties and break the per-tIFS gap chain
(gaps would then span empty+2·tIFS, not one tIFS). Frozen for 2M:
`AIRTIME_MIN_TICKS_2M = 256` (below the 320 t empty, above noise), `AIRTIME_MAX` unchanged.
A DATA PDU (244 B) span ≈ 249 B × 4 µs = 996 µs = 15 936 t (well inside MAX).

**`AIRTIME_MIN` self-validates for free — no extra step.** If it is set too high and the
empties are dropped, adjacent captured gaps span *empty + 2·tIFS* instead of one tIFS, so
the f150 baseline plateau would land near ~2× the true value, NOT at 2783 t. The frozen
baseline gate (§5: `med_pre` within ±2 t of 2783) therefore rejects a mis-set
`AIRTIME_MIN` as INCOMPLETE rather than passing a contaminated plateau. Keep the f150
control arm in *every* campaign — it is simultaneously the drift control and the
empty-capture validity check.

## 4. Capacity / ring (recomputed) — no silent overflow

Saturated 2M/50 ms ≈ 29 pairs/event × 20 events/s ≈ 1160 packets/s → the 2048-record ring
FILLS IN ≈ 1.76 s. Therefore:
- The runner MUST size the capture window so records < RING_N (target ≈ 1.4 s ⇒ ~1600
  records), OR enlarge the ring, OR the analyzer MUST treat any `drop>0` as
  **INCOMPLETE-CAPTURE** (never a silent truncation).
- Report captured-record count, `drop`, and `n_stale_addr`; require the FSU-off and FSU-on
  plateaus to each contain ≥ `Q3_MIN_PLATEAU_PAIRS` clean pairs BEFORE any verdict.
- **The 1160 pkt/s figure is an upper bound — MEASURE the real rate in the pilot, do not
  assume it.** A single-channel observer only sees the connection when the hop visits its
  channel, so the *effective* record rate (and thus the fill time and the right window
  length) is unknown until the smoke run reports it. The pilot's first job is to report the
  observed record rate + `drop`/`stale`; the accepted-cell window is sized from THAT, to
  keep `drop=0`.

## 5. Outcome taxonomy (frozen — NOT just "52 or 150")

Over the real-valued step `s = med_pre(150) − med_post`:
- **AGREEMENT**: `|s − 1568| ≤ 4` AND `med_pre` within ±2 t of 2783 AND on-chip xval ≤ 8 µs.
- **OFF-TARGET-REDUCTION**: `|s|` large but `|s − 1568| > 4` (a reduction, wrong magnitude).
- **NO-REDUCTION**: `|s| < 64`.
- **WRONG-DIRECTION**: `s ≤ −64`.
- **UNSTABLE-PLATEAU**: within-plateau IQR > 16 t (either plateau) ⇒ no verdict.
- **INCOMPLETE-CAPTURE**: `drop>0`, or either plateau < min pairs, or baseline `med_pre`
  outside ±2 t of 2783 ⇒ no verdict.
- **INVALID-CONFIG**: on-chip spacing ≠ 52 / phys/types mismatch / FSU-revert guard
  unmet / not-same-session ⇒ rejected before analysis.

## 5.5 Falsifiability controls — prove the pipeline CAN report "no reduction"

A rig that always outputs 1215 t would "confirm" FSU no matter what. Both controls must
pass in the campaign (they are cheap and use the same session/instrument):
1. **f150 control** (FSU-off arm) reads **2783 t** (NO-REDUCTION). Also the §3 empty-capture
   check.
2. **Deliberately-broken config**: one run with the peripheral's IFS clamp *removed*
   (`EVENT_IFS_LOW_LAT_US` unset) while the central still requests 52 — the observer MUST
   read ~2783 t (NO-REDUCTION). If it reads 1215 t here, the pipeline is self-confirming and
   NO 2M cell may be accepted until the defect is found.

## 6. Acceptance (mirrors Q3a discipline)

- **A single accepted cell is a CANDIDATE, never established.** Establishment requires
  **≥ 2 reset-isolated mid-step cells** (within-connection 150→52 reductions), each
  AGREEMENT, distinct AAs, identical final conn params.
- **Steady ABBA (optional, for a frozen estimate):** order f150/f52/f52/f150, one host
  boot; drift-cancelled estimate `((A1−B1)+(A2−B2))/2` vs 1568 t, `|d| ≤ 4`; homogeneity +
  current-lineage + firmware + final-params gates (reuse `combine_abba.py` with the 2M
  constants).
- **Provenance-bound manifest:** runner args, resolved conn config, source commit +
  tree-dirty, sha256 of observer hex / all logs / analyzer / runner / this protocol, and
  endpoint build (hex + `.config`) hashes; `observer_image_verified` FALSE under
  `--skip-obs-flash`. Every attempt preserved; failed campaigns quarantined, never reused.

## 6.5 Console / capture hygiene (cost hours in 2026-08 — make them gates)

- **Read the observer console with pyserial + DTR asserted.** The nRF52832 DK J-Link
  console gates output on DTR; a raw `os.open`/`cat` reads nothing after a reset or
  power-cycle. External `stty … raw` only — never `tcsetattr` in the reader (it corrupts
  the macOS cu.* CDC stream).
- **`CONFIG_LOG=n`** on the observer (raw synchronous `printk`); `CONFIG_LOG=y` routes
  through a deferred thread that can swallow the dump.
- **The `CAPTURE-END` ring dump is a burst** — size the reader and the capture duration so
  the whole dump lands *after* the CDC has re-settled; a truncated/garbled dump ⇒
  INCOMPLETE (§2.5.6), never a partial parse. Rely on the dump, not on catching one-time
  boot banners across the CDC re-enumeration gap.
- **Board identity is fixed and asserted**: observer = nRF52832 DK **1050347760** (reflash
  it from whatever it last held), FSU pair = central **1057794857** + peripheral
  **1057719509**. Confirm serials before flashing.

## 7. Analyzer/runner adaptation (mechanical from §1–§5)

`analyze_q3_2m.py` (co-located; leaves `analyze_q3.py` = 1M intact): sets
`PREAMBLE_AA_OFF=383`, `EXPECT_POST_US=52`, `EXPECT_STEP_TICKS=1568`,
`frozen baseline=2783`, `AIRTIME_MIN_TICKS=256`, on-chip bins {150,52}, and the §5
taxonomy; otherwise reuses the frozen change-point / plateau / retention / binding
machinery of `analyze_q3.py`. Observer built `-DPHY2M=1` with the session's
`-DQ2_AA/-DQ2_CRCINIT/-DQ2_CHAN`. **Unexercised until real 2M FSU data exists.**

## 8. Pre-registered interpretation

- **AGREEMENT** ⇒ FSU physically reduced tIFS to 52 µs at 2M *in the throughput session*.
  The goodput non-effect is then a DOWNSTREAM (event-fill/occupancy) matter, not an
  FSU-application failure.
- **NO-REDUCTION / INVALID** ⇒ the `spacing=52` host report did not correspond to an
  on-air reduction in this session (e.g., bench-force-feature shim, or a revert) ⇒ the
  open M0 FSU did not physically apply at 2M here.
- **OFF-TARGET / UNSTABLE / INCOMPLETE** ⇒ no claim; fix the flagged condition and re-run.

## 9. Promotion to an ACCEPTED cell + the objective measurement (frozen — REV 3)

The smoke proved capability; these gates turn it into an accepted result AND answer the
objective (does open+FSU beat open on throughput). Freeze BEFORE accepted data.

### 9.1 Same-session saturation — merge, don't juggle
The observed link MUST be the saturated goodput link, not a low-rate proxy (§2.5.3).
- **One central** does everything: a saturating blast + the pinned 2-channel map +
  `Q2CONN` + the mid-capture FSU trigger. Add the blast to `q2-central` (it already has
  pin-map/`Q2CONN`/trigger) — do NOT bolt the Q2 apparatus onto the blast app.
- Regime = the goodput run's: **2M / 50 ms**, matched pref intervals, `GAP_AUTO_UPDATE=n`.
- **Saturation proof, in-window**: on-chip occupancy ≈ full (report `avg_pe`), AND the
  observer record rate ≫ the low-rate smoke. The **f150 baseline MUST still land at
  2783 ± 2 t under saturation** — that is the proof the DATA(244 B)+empty gap chain is a
  clean per-tIFS chain and not contaminated by the long DATA packets.

### 9.2 The objective measurement — the observer makes a NULL result interpretable
Run, reset-isolated and counterbalanced, at the same regime:
- **Receiver-delivered goodput** (sink cumulative-byte Δ over a fixed window) with FSU OFF
  vs FSU ON — the ONLY admissible throughput metric (sender-completion/occupancy are NOT;
  retracted twice in this project).
- **The observer confirms tIFS 150→52 in the SAME session.** Then:
  - goodput rises with FSU ⇒ **open+FSU beats open** (objective YES, throughput).
  - goodput flat BUT observer shows tIFS dropped ⇒ **FSU applies but does not convert**
    (event-fill limit) — a clean NEGATIVE the goodput rig alone cannot establish.
- Do NOT infer conversion from `occupancy × pair-time`. Report the objective (yes/no)
  bounded; the *mechanism* of non-conversion (event start/end + close-reason +
  queue-nonempty-at-close) is a SEPARATE, deeper question — it must not block or inflate
  the objective answer.

### 9.3 PORT the three 1M-frozen gates — do NOT bypass them
The smoke's `--smoke` bypasses are INADMISSIBLE for an accepted cell:
- **`f52` arm**: register it in the runner (choices += `f52`; on-chip expect `{150,52}`;
  analyzer step 1568). Update the runner self-test so the 2M constants can't silently drift.
- **`assert_fsu_config_2m`**: allow `FSU_MIN=52` and `PHY_2M=y`; REQUIRE
  `EVENT_IFS_LOW_LAT_US=52` on BOTH endpoints, periph `TIFS_CAPTURE_BENCH=y`,
  `FRAME_SPACE_UPDATE=y`, and periph `GAP_AUTO_UPDATE_CONN_PARAMS=n`.
- **Provenance-bound 2M `--calib`**: freeze `gap-proxy-calibration-2m.json`
  (baseline 2783, post 1215) derived from the Q1-2M synthetic acceptance; the accepted
  cells gate against it, not the analyzer's built-in default.

### 9.4 No-bypass audit + mandatory firmware fixes (from the smoke)
- **Audit**: an accepted run has NO active `--smoke`/`--force-quarantine` bypass; it passes
  every §2.5/§3/§5.5/§6 gate legitimately.
- **REQUIRED firmware** (the smoke found these; they are now frozen requirements):
  `AIRTIME_MIN_TICKS=256` on the observer (a 2M empty PDU is ~20 µs / 320 t; the 1M 500 t
  floor drops empties and breaks the gap chain), and periph
  `GAP_AUTO_UPDATE_CONN_PARAMS=n` (its ~5 s auto-update caused the `0x08` drop that killed
  smoke-1 before the FSU trigger).

### 9.5 Pilot-stability-first
Before ANY accepted cell: one pilot confirming the saturated 2M/50 ms link stays up
through the FSU trigger AND the full capture window (zero disconnects; identical final
conn params across legs), and that the ring fills with `drop=0`. Only then run the
counterbalanced accepted cells (≥2 reset-isolated) + the §5.5 falsifiability controls.
