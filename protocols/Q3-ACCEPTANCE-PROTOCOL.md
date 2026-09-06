# Q3a acceptance protocol — FROZEN rev-6 (event-gated steady ABBA, 2026-08-12)

**rev-6 corrects the steady ABBA arm after the failed integration block b1.** Live
collection (2026-08-12) accepted the two mid-step PRIMARY cells but the first ABBA
block failed: seq1 f100 showed 150 µs on air AND on-chip despite host FSU-completion at
spacing=100. Root cause (confirmed at source + config + on air): the peripheral's
**automatic connection-parameter update ~5 s after connect** (`BT_GAP_AUTO_UPDATE_CONN_PARAMS`,
pref 30–50 ms ≠ the central's fixed 7.5 ms, `CONN_PARAM_UPDATE_TIMEOUT=5000`) runs
`ull_conn_update_parameters()`, which **resets the negotiated frame space** (`tifs_tx/rx`
and per-PHY `fsu_min/max`) to the 150 µs default. An FSU requested BEFORE that update is
silently reverted; requested AFTER, it persists. The mid-step PRIMARY pair is UNAFFECTED
(its FSU is requested ~11–18 s in, after the update) and REMAINS the primary evidence.
This reset-on-update is disclosed as a **controller limitation** of the open M0 / PR-99473
reference (negotiated frame space is not preserved across a later connection update).

rev-6 makes the steady arm **EVENT-GATED** on that update (not a fixed timer):
- both endpoints emit ONE machine-readable `Q3PARAMUPD` record (AA/session/interval/
  latency/timeout/seq) — a minimal HOST-APP addition; the FSU CONTROLLER + radio-path
  instrumentation are byte-identical to the primary cells;
- the runner waits for it on BOTH endpoints, requires agreement + the FROZEN final
  interval (50 ms = 40 units), then: **f150** update → sham settle → START snaps → GO;
  **f100** update → FSU + both completions → settle → START snaps → GO;
- START snapshots move AFTER all pre-window activity (fixing block b1's snapshot-slop
  bracket failure); NO later param-update may occur before/through the window (verified);
- the manifest records the final connection parameters; `combine_abba` requires them
  IDENTICAL across all four cells.

**Evidence tiers (kept separate, never pooled):** rev-5 primary = old host app, two
accepted on-air 150→100 µs within-connection reductions; rev-6 ABBA = instrumented host
app + SAME FSU controller, four-cell confirmation. Failed block b1 is preserved and is the
integration finding that motivated rev-6. Option C (fix the controller to preserve FSU
across updates) is a later upstream-fix campaign that, if done, reruns all six cells.

---

## (superseded) Q3a acceptance protocol — FROZEN rev-5 (binary-provenance hardened, 2026-08-12)

FROZEN for accepted-cell collection. **rev-5 closes the last two provenance gaps** a
review found in rev-4:
- **Binary lineage was schema-checked, not evidence-checked.** The ABBA combiner
  validated that firmware digests *looked like* 64-hex but never re-resolved the images.
  Now every accepted cell ARCHIVES HEX+ELF+.config for all three devices into
  `firmware/<dev>.<ext>`; the combiner RE-HASHES those archived binaries against the
  manifest digests AND reruns `assert_fsu_config.py` over the archived central (@arm) +
  peripheral `.config` — a fabricated digest, a missing build file, a post-cell binary
  edit, or a wrong-arm config now rejects.
- **Stray steady runs could self-accept.** The campaign/seq/arm gate fired only when
  `--abba-seq` was supplied. Now EVERY accepting `--q3-steady` run must carry a nonempty
  `--abba-campaign-id`, an in-range `--abba-seq`, and the arm the A/B/B/A sequence
  demands at that seq (checked before any hardware mutation); a standalone steady
  diagnostic must use `--smoke` (which can never accept).

**Acceptance ordering (rev-5.1):** firmware archival is now a PRE-FLASH, validated
ACCEPTANCE PREREQUISITE for BOTH the mid-step and steady modes (previously the combiner
re-verified only steady cells; the two primary mid-step cells had no such check).
`snapshot_firmware()` copies all nine artifacts into the cell BEFORE flashing, requires
completeness + valid digests + a passing `assert_fsu_config` on the archived configs;
the flash reads the same build dirs; `firmware_unchanged()` requires them byte-identical
post-run (closing the time-of-check/time-of-use gap); and `decide_verdict` refuses to
ACCEPT unless `firmware_archive_valid` (→ `firmware-archive-invalid` quarantine). The
ABBA combiner's independent rehash/assert stays as defense-in-depth.

**Operational (per review):** run all four ABBA cells during the SAME host boot
(`time.monotonic_ns()` resets on reboot, so cross-run ordering only holds within a
boot); write cell output OUTSIDE the repo, or commit/archive each cell between runs, so
the next run still passes the clean-tree hard gate. The two primary mid-step f100 cells
need NO ABBA metadata.

rev-5 supersedes **rev-4** (which hardened the ENFORCEMENT of the rev-3 contract —
rev-3 defined the complete steady + ABBA contract but a review found four enforcement
gaps between the frozen constants and what the code actually checked; rev-4 closed them,
no constant changes, the ±4-tick primary gate untouched):
- **F1 — current-lineage binding.** The ABBA combiner bound cells only to EACH OTHER;
  four cells sharing one stale/fabricated hash passed. Now it rehashes the CURRENT
  analyzer/runner/protocol/assert/combiner tree and requires each cell's recorded digest
  to match, AND requires each cell's `q3_contract` to equal `q3_contract()` exactly.
- **F2 — provable chronology.** `abba_seq`/campaign were operator labels with no time
  proof. Each steady manifest now records absolute `time.monotonic_ns()` start/end, and
  the combiner requires strictly-ordered, non-overlapping intervals in A/B/B/A order;
  the runner also gates `campaign/seq/arm` consistency BEFORE any hardware mutation.
- **F3 — settle in the contract.** `Q3_STEADY_SETTLE_MIN_MS` is now emitted by
  `q3_contract()`; a steady run that overrides `guard`/`min-plateau` (which the steady
  analyzer ignores) without a declared deviation is REFUSED before capture.
- **F4 — full image provenance.** Endpoint AND observer acceptance now require valid
  HEX **+ ELF + .config** digests (was HEX+.config for endpoints, HEX-only for observer).

rev-4 supersedes rev-3 (which froze the steady + ABBA estimators/tolerances in code),
which supersedes rev-2, which supersedes DRAFT rev-1 (six preregistration defects:
arithmetic, tolerance, lineage, f150 definition, single-direction scope, the
"tri-cross-validation" overclaim).

The full acceptance contract is frozen across the review chain; **smoke-5
(`observer-q3-smoke5-20260812`) is the successful PRE-ACCEPT VALIDATION**: the registered
mid-step analyzer reached METRICS-OK end-to-end on hardware (RF step 802 t, on-chip
cross-val Δ=50 µs, peer participation, retention, plateau shape — every leg gated).
NOTHING here is spec-quoted; see `zephyr-patches/fsu-m0-cheatsheet.md` for source tiers.

**Q3a is the DIRECT ON-AIR VERIFICATION of an FSU-reduced tIFS — the pending
Phase-4 physical gate.** Scope: **Q3a only**, one arm **150→100 µs at 1 M**; generality
(37-ch hop-follow, CONNECT_IND auto-discovery, SDC, 2 M, 52 µs floor) is deferred.

### Scope + qualifications (what this does and does NOT establish)
- **CLOSES:** physical on-air application for the **1 M 150→100 µs** geometry only.
- **NOT:** FSU conformance evidence, 52 µs physical evidence, or a refinement of the
  existing throughput CI.
- **smoke-5 is permanently QUARANTINED + non-evidentiary.** It recorded
  `pre_run_dirty=true` and MUST NOT be promoted retroactively or relabelled as an
  accepted cell. Accepted cells are collected FRESH from a CLEAN tree (below).
- **CC0-overwrite** is the confirmed perturbation cause **under this tested
  build/configuration**; do not generalize it to every nRF54 controller configuration.
- smoke-5's central pre-phase retention was **exactly 0.950** (on the frozen boundary).
  It passes legitimately; the 0.95 floor is IMMUTABLE — accepted replicas must pass it
  unchanged, and it is NOT loosened if a later cell falls below it.

## What carries over from Q2 (qualified, unchanged)
- Observer (nRF52832, 30 s window) measures the on-air `gap_proxy` = prev-packet
  END → next-packet ADDRESS = tIFS·16 + preamble/AA. Q1/Q2 proved ±1-tick
  resolution over 52–150 µs.
- Q2 froze the FSU-OFF baseline at `gap_proxy = 3047 ticks`: **two symmetric
  controls established 3047; four near/far cells subsequently validated it** (live
  medians 3047–3049, cross-cell wobble ≤ 2 ticks; IQR 8–10). Note the
  nominal-150 µs prediction is 150·16+639 = **3039**; the measured **+8-tick
  offset** (3047) is UNEXPLAINED (fixed bias vs sub-µs real tIFS vs calib) and is
  characterised, not assumed away, below.

## The offset problem (why absolute plateaus are NOT the primary gate)
`gap = tIFS·16 + 639 + ε`, with ε = +8 ticks measured at 150 µs (origin unknown).
IF ε is tIFS-INDEPENDENT (fixed preamble/calib bias), it CANCELS in a difference,
so the **reduction is offset-free**: `step = (150−100)·16 = 800 ticks`, and the
f100 plateau would be `2239 + 8 = 2247` (NOT 2239). One cannot gate on both an
800-tick step and a 2239-tick plateau (3047 − 2239 = 808). Therefore:

- **PRIMARY (offset-cancelling): the on-air STEP** `median(f150) − median(f100)`.
- **SECONDARY / DIAGNOSTIC: the absolute plateaus** f150≈3047 and f100 (recorded,
  predicted ≈2247 if ε is fixed). These CHARACTERISE ε (does it persist / scale)
  but are NOT a pass/fail gate until ε's origin is established.

## Primary metric & preregistered tolerance
The negotiated reduction is 50 µs, i.e. the expected step is **800 ticks**. The
tolerance is derived from Q0–Q2 uncertainty BEFORE any smoke: each plateau median
reproduced cross-cell within **±2 ticks** in Q2, so their difference carries at
least the sum, **±4 ticks**. Preregister:

> **PRIMARY GATE: |on-air step − 800| ≤ 4 ticks (= 50 ± 0.25 µs).**

This ±4-tick primary gate is IMMUTABLE — frozen here from Q0–Q2 uncertainty. The
go/no-go smoke may refine INTEGRATION details and SECONDARY diagnostics
(transition-window length, minimum sample counts, cross-val tolerance, the ε
characterisation) but MUST NOT change the primary target (800) or its ±4 tolerance.

The effect (800 ticks) vs the per-capture IQR (8–10) is an **effect-to-IQR ratio ≈
80** — NOT an SNR; it means step *existence* is unambiguous, not that its accuracy
is 1/80 of a tick.

**Two distinct outcomes (do not conflate):**
- **(a) reduced spacing physically OBSERVED** — a clear downward step of order
  50 µs (e.g. ~790 ticks) demonstrates FSU changes the on-air turnaround; this is
  the qualitative physical result.
- **(b) exact REGISTERED agreement** — step = 800 ± 4 ticks, the quantitative
  accuracy gate. A ~790-tick step FAILS (b) but still satisfies (a); it must be
  reported as "reduction observed, off registered target by N ticks — investigate,"
  NOT as "no physical reduction." Only a step ≈ 0 refutes (a).

## Direction — Q3a directly observes ONE turnaround only
The observer's `gap_proxy` is the intra-event **central-packet END →
peripheral-packet ADDRESS**, i.e. the **peripheral's response turnaround** after
receiving the central's packet. The reverse (central) turnaround occurs before the
NEXT event's opener, separated by the connection interval — NOT observable as a
tIFS in a single-PDU event. Therefore the core verdict is scoped to the
**observed responder-side turnaround**; claims of "per direction" or "×2 = −100 µs
round trip" are RETRACTED for the core.

- **Reverse-direction is a SEPARATE Q3b draft, NOT a Q3a companion.** It would push
  data so events carry MULTIPLE PDUs (C→P, tIFS, P→C, tIFS, C→P, …) and the observer
  reads ALTERNATING gaps (responder AND central turnaround). It is the highest-value
  next step (> a second spacing) for supporting the round-trip mechanism, but it
  has its OWN undefined preregistration — traffic pattern, per-turnaround
  attribution, sample-count and acceptance rules — that must be written and frozen
  BEFORE any collection. Until that Q3b draft exists, reverse-direction is out of
  scope and the Q3a verdict is explicitly single-direction.

## Conditions & cells
- **f150 (FSU-OFF control) = NO request** (`APP_FSU_MAX_US=0`; matches
  `q2-central/fsu-f150.conf`). tIFS stays 150 µs; observer plateau must reproduce
  3047 (confirms enabling FSU *support* alone does not shift tIFS).
- **f100 (FSU-ON)** = request `[100..150]`, responder floor 52 → SELECTED 100.
- **Optional diagnostic (not the control):** a separate `150→150` no-op REQUEST
  cell (MIN=MAX=150) — exercises the FSU machinery with no change. Note it emits NO
  HCI Complete event (ntf gated on `fsu_changed`), so it is behavioural-only.

**Design (per review): within-connection step is PRIMARY.**
1. **Mid-connection step (primary):** one 30 s capture per cell where FSU is
   triggered mid-run; the observer plateau drops 3047→~2247 at that instant, SAME
   AA/channels/boards/build — the only thing that changes is tIFS, eliminating the
   build/connection confound. ≥2 cells. The runner must: start capture, collect a
   fixed BASELINE DWELL, send an `F` command at a preregistered time, record the
   request/central-completion/peripheral-completion timestamps, EXCLUDE a fixed
   transition window, and require minimum sample counts + stable plateaus on BOTH
   sides. The analyzer splits at the step, excludes the transition, and reports both
   plateaus + the step.
2. **Steady-state confirmation:** separate f150 and f100 cells in an **ABBA
   counterbalanced** order (f150, f100, f100, f150 — or ABBA across sessions),
   paired-step estimate as confirmation. If mid-step proves too risky to implement,
   the counterbalanced steady-state pairs are the fallback primary.

## Cross-validation — NOT symmetric across spacings
Each method carries its own offset, so compare **step-wise** (each sees the same
reduction), not by absolute agreement.
- **f150:** on-air (observer) + on-chip baseline. **No HCI completion** (no-request
  control). → a **two-method** baseline.
- **f100:** **three layers** — HCI control-plane (evt 0x35 SELECTED = 100, on the
  central) + on-chip proxy + direct on-air. → the **three-method** condition.

**The on-chip proxy MUST measure the same direction as the on-air primary.** The
observer's gap is the peripheral's RX→TX turnaround, so `TIFS_CAPTURE_BENCH` must be
enabled and cleared/frozen/**drained on the PERIPHERAL** — a central-only drain
measures a different turnaround (or nothing useful with single-PDU events). For a
mid-step cell, clear the histogram BEFORE the baseline dwell and retain SEPARATE
150 / 100 histogram bins through the whole run (the instrument keys on programmed
tIFS, so both plateaus are captured in one run).

**Peer participation** must be shown independently of the initiator's own view: the
HCI `initiator` field alone does not prove the peer engaged. Require a
**peripheral-side FSU completion/diagnostic tied to the same AA/session** (a
`frame_space_updated` callback on q2-periph, currently ABSENT — must be added).
Capturing the on-air `LL_FRAME_SPACE_REQ/RSP` (0x3B/0x3C) is useful but OPTIONAL —
the observer parked on ch10 may miss a control event that lands on ch11.

## On-chip instrument — currently NOT wired (must be fixed before its leg counts)
The on-chip tIFS instrument (`lll_conn.c`) is gated by
**`CONFIG_BT_CTLR_TIFS_CAPTURE_BENCH`** (NOT `CONFIG_INSTRUMENTATION`, which rev-0
wrongly named). The current fsu confs do NOT enable it, and the apps do not
clear/freeze/drain its histogram. Until that symbol is enabled and a drain path
prints its median per programmed tIFS, the on-chip leg is UNAVAILABLE and the smoke
runs on-air + HCI only.

## Calibration lineage (Q3 uses a SEPARATE artifact)
The pair-window widening (2900–3200 → 1200–3300, required so 100 µs pairs (≈2247)
are found at all) changes `analyze_q2.py`'s hash, so `validate_calib()` correctly
REJECTS the Q2 artifact `gap-proxy-calibration-20260811.json` under the Q3 analyzer
("lineage … != current tool"). Procedure: (a) commit the Q3 analyzer; (b) RECOMBINE
the ORIGINAL two Q2 symmetric-control cell dirs with `combine_calib.py` into a NEW,
separately-named Q3 artifact (Q3-analyzer lineage); (c) PRESERVE the original Q2
artifact unchanged. The Q3 baseline is thus re-derived from the same raw controls
under the frozen Q3 tooling.

## ACCEPT gates (freeze; all must pass)
1. Per cell: all Q2 gates (structural, config binding incl. map==000c000000 &
   chan_count==2, AA cross-checks, combined slop ≤ 3 %, retention lower bound ≥
   95 %, monotonic counters).
2. f150 plateau reproduces 3047 within Q2 cross-cell reproducibility (±2 ticks).
3. **PRIMARY: |on-air step − 800| ≤ 4 ticks** (mid-connection preferred; else the
   ABBA paired steady-state estimate).
4. f100: HCI evt 0x35 SELECTED == 100 µs on the central AND a peripheral-side
   completion/diagnostic at the same AA/session (peer participation).
5. Cross-val (step-wise): on-air step and on-chip step agree within tolerance once
   the on-chip leg is wired (see limitation above).
6. Transition instant excluded; both plateaus steady-state.
7. Absolute plateaus RECORDED (f150≈3047, f100 measured) and ε characterised —
   reported, not gated.

## REJECT / STOP (see the two-outcome distinction above)
- Step ≈ 0 → outcome (a) REFUTED: no on-air reduction; STOP, do NOT report
  FSU-verified (this would contradict the indirect evidence — a major finding).
- Step is a clear ~50 µs reduction but |step − 800| > 4 → outcome (a) holds,
  accuracy gate (b) FAILS: report "reduction observed, off registered target by N
  ticks — investigate," NOT "no reduction."
- f150 plateau ≠ 3047 (±2) → enabling FSU support shifted tIFS; the comparison is
  confounded; STOP.
- No peripheral-side FSU completion at the connection's AA/session → peer
  participation unproven; STOP (do not rely on the initiator field alone).
- Link reconnect (AA change) within a cell → REJECT that cell.

## Out of scope for Q3a (→ later)
Reverse-direction (a separate Q3b DRAFT, above); CONNECT_IND auto-discovery; 37-ch
hop-follow; connected SDC; 2 M; the 52 µs floor; a second reduced spacing;
spec-correct feature-65 exchange (bench uses the `FSU_BENCH_FORCE_FEAT` shim —
acceptable for a scoped bench PHYSICAL claim, not a conformance claim, provided
peer participation is independently logged).

## Frozen acceptance constants (authoritative = `analyze_q3.q3_contract()`)
Every **defined** acceptance constant is frozen in code and emitted per run as one
machine-readable `Q3-CONTRACT …` line (pinned into each manifest with the structured
contract + runner args + tool/protocol/calib hashes). The ±4-tick PRIMARY gate is
IMMUTABLE. `guard_ticks`/`min_plateau` are frozen too: any deviation REQUIRES an
explicit reason and forces a non-accept (`CONTRACT-DEVIATION`, never METRICS-OK);
zero/negative are rejected. The stable-plateau SHAPE gate is now implemented and
frozen, **calibrated from the six accepted Q2 live-link cells** (not the synthetic
generator). The acceptance CONTRACT is frozen; the analyzer PATH is NOT yet promoted
to accepting — the runner QUARANTINES (`q3-preaccept-smoke`) until the pre-accept
smoke validates it on real air. **rev-3/rev-4:** `q3_contract()` now also emits the
frozen steady (`steady_tol_ticks`, `steady_settle_min_ms`) and ABBA (`abba_arm_seq`,
`abba_expect_step`, `abba_tol_ticks`) constants, so the complete executable contract —
mid-step, steady, and ABBA — is pinned into every manifest with no constant introduced
after this freeze, and the ABBA combiner requires each cell's `q3_contract` to equal
the current `q3_contract()` (not merely to match across cells).

| constant | value | role |
|---|---|---|
| `step_tol` | **4 ticks** | IMMUTABLE primary gate: \|median(pre)−median(post) − 800\| ≤ 4 |
| `expect_step` | 800 ticks | (150−100) µs × 16; the registered target |
| `dur_tol_ms` | 1500 ms | tick- AND host-duration each vs declared 30 s cap |
| `guard_ticks` | 160 000 (10 ms) | extra margin around the completions in the transition window |
| `min_plateau` | 30 pairs | minimum clean pairs per plateau AFTER exclusion |
| `baseline_tol_ticks` | 2 ticks | median(pre) vs the frozen 150 µs `gap_proxy` (3047) from `--calib` |
| `xval_tol_us` | 8 µs | \|on-air step/16 − (onchip med₁₅₀ − onchip med₁₀₀)\| |
| `null_thr_ticks` | 64 (4 µs) | \|step\|<thr ⇒ NO-REDUCTION; step≤−thr ⇒ WRONG-DIRECTION |
| `host_jitter_ms` | 50 ms | cross-port RELATIVE time uncertainty (observer-anchor + endpoint-UART arrival) |
| `boundary_slop_ms` | 1000 ms | clear-after-START / freeze-after-END host allowance |
| `changepoint` | `max-abs-mean-split` | preregistered INDEPENDENT **diagnostic** estimator; both sides constrained ≥ `min_plateau` (no k=1/n−1 edge win); does NOT move the partition |
| `plateau_iqr_max` | **16 ticks** | per-plateau IQR (six accepted Q2 cells: 8–10 t; threshold = margin above) |
| `plateau_drift_max` | 5 ticks | \|median(first half) − median(second half)\| (cells ≤ 2 t) |
| `plateau_blocks` / `plateau_block_max` | 4 / 5 ticks | each time-block median within 5 t of the plateau median (cells ≤ 2 t) |
| `plateau_conc_eps` / `plateau_conc_frac` | 10 t / 0.95 | ≥ 95 % of samples within ±10 t of the median (cells ≥ 0.99) — a CONCENTRATION check, not a modality test |
| `phase_retention_min` | **0.95 FROZEN** | per-phase pairs/tx floor (F-time snapshots), inherited from the established Q2 `RETENTION_MIN`; if the smoke misses it → instrument incompleteness + redesign, NOT retune |

**Whole-cell retention is enforced** (observer C→P pairs vs the controller `tx`
denominators, overcount → REJECT, ≥95% both sides), reusing the Q2 `RETENTION_MIN`.

### Stable-plateau SHAPE gate — implemented + frozen (analyze_q3.py, calibrated from real cells)
Each plateau (pre + post) must be a single stable tIFS: IQR ≤ 16 t, half-to-half
drift ≤ 5 t, per-block (4) median stability ≤ 5 t, and ≥ 95 % concentration within
±10 t. Thresholds are **calibrated from the six accepted Q2 live-link captures**
(symctl1/2 + nearfarA1/A2/B1/B2 — real IQR 8–10 t, concentration ≥ 0.99), with a
replay regression test that requires all six to PASS. A failure ⇒ INCOMPLETE.

### Phase-specific retention — IMPLEMENTED (F-time counter snapshots); pair-rate demoted
- **Pair-rate balance is a DIAGNOSTIC ONLY** (reported, never a gate). The six accepted
  cells show a strong early-connection pair-rate transient (57–61 % pre/post imbalance
  across an 8 s split with NO FSU), so "pair rate is FSU-invariant" is empirically
  **false** for this instrument — it cannot be a gate.
- **Phase-SPECIFIC retention is IMPLEMENTED + FROZEN** (`q3_phase_retention`): both
  endpoints emit a `Q3PHASESNAP` at F-time (runner sends `M`), splitting the ch10 TX
  denominator into pre (START→MID) and post (MID→END). The MID snapshot is
  IDENTITY- and TIMING-bound: exactly one per role, matching AA/session/boottag/channel,
  strictly between the START/END snaps, and its reltick ± uncertainty inside the
  transition window (which is widened to start from the EARLIEST of {REQ, MID_C, MID_P},
  so MID is structurally inside the exclusion). The analyzer requires observer pairs /
  phase-tx ≥ 0.95 for BOTH phases on BOTH roles — catching a concentrated per-phase
  loss that whole-cell retention DILUTES (a self-test proves a post-phase loss passing
  whole-cell at 100 % is rejected at post 93.8 %). Conservative (MID sits inside the
  excluded transition, so the phase denominators slightly over-count). The 0.95 floor
  is FROZEN (Q2 `RETENTION_MIN`); a smoke miss ⇒ instrument incompleteness + redesign,
  never a retune of the threshold from the FSU-bearing capture.

**Outcome taxonomy** (only METRICS-OK passes; the cell is QUARANTINED
`q3-preaccept-smoke` until the smoke promotes the analyzer path): `METRICS-OK` (800±4
AND on-chip agree) ·
`REDUCTION-OFF-TARGET` (clear reduction, off target — NOT a null) · `NO-REDUCTION`
(null) · `WRONG-DIRECTION` · `XVAL-DISAGREE` · `INCOMPLETE` (a gate could not be
computed). Q3 reuses **all applicable Q2 structural/configuration gates**
(`analyze_q2.validate_common`) **plus explicit Q3 whole-cell retention**, a validated
`--calib`, and the strict control-plane/peer/on-chip/boundary-slop validators, before
any step math.

## Steady-state single-plateau contract (rev-3 — frozen in `q3_steady_analyze`)
The ABBA confirmation arm uses STEADY cells: one plateau per cell, no mid-run trigger.
The steady analyzer reuses every Q2 structural/config gate + whole-cell retention +
the frozen plateau-SHAPE gate + a validated `--calib`, and adds:

| constant | value | role |
|---|---|---|
| `steady_tol_ticks` | **4 ticks** | f100-steady single-plateau median vs expected `3047 − 800 = 2247` |
| (f150 baseline) | `baseline_tol_ticks` = 2 t | f150-steady plateau vs the frozen 3047 (same as the mid-step pre gate) |
| `steady_settle_min_ms` | **2000 ms** | f100-steady: min interval from BOTH FSU completions to CAPTURE-START (rev-4: now emitted by `q3_contract()`; a steady run overriding `guard`/`min-plateau` without a declared deviation is refused before capture) |

- **f150 arm:** rejects FSU activity of ANY kind (`Q3FSU-REQ` / `-REJECT` / `-DONE`) —
  a clean no-request control shows zero FSU tokens; exactly ONE on-chip 150 bin, its
  lifecycle session bound to the **connection snapshot** session.
- **f100 arm:** the FSU must be COMPLETE + peer-participated (`initiator=PEER`) + bound
  (request session == both endpoint snapshot sessions) and SETTLED ≥ 2000 ms BEFORE the
  window opens; exactly ONE on-chip 100 bin, its lifecycle session bound to the **FSU
  request** session; the clear/freeze boundary slops gate [0, 1000] ms exactly as
  mid-step.
- Verdict `STEADY-METRICS-OK`; the cell is still QUARANTINED `q3-preaccept-smoke` until
  the promotion commit (below) enables acceptance.

## ABBA confirmation contract (rev-3 — frozen in `combine_abba.py`)
The within-connection f100 STEP analyzer stays the **PRIMARY** experiment; ABBA is the
independent BETWEEN-connection **confirmation**. Four steady cells collected in the
frozen **A/B/B/A = f150, f100, f100, f150** order; the drift-cancelling estimator

> **`estimate = ((A1 − B1) + (A2 − B2)) / 2`**, gated **|estimate − 800| ≤ 4 ticks**

removes any LINEAR environmental drift over the campaign (A1=a, B1=b+d, B2=b+2d, A2=a+3d
⇒ estimate = a−b). **Scope honesty:** each cell's plateau is already pinned by the
per-cell steady tolerance (f100 ±4 t, f150 ±2 t), so the drift ABBA can absorb is
bounded by those envelopes — ABBA confirms the step via a design with FOUR distinct
reset-isolated connections, it does not widen the per-cell tolerance.

| constant | value | role |
|---|---|---|
| `abba_arm_seq` | `f150,f100,f100,f150` | frozen A/B/B/A collection order |
| `abba_expect_step` | 800 ticks | drift-cancelled target (= `expect_step`) |
| `abba_tol_ticks` | **4 ticks** | \|estimate − 800\| ≤ this (SAME immutable primary gate) |

ABBA gates (ALL must pass; a single failure ⇒ the WHOLE campaign is INCOMPLETE — NO cell
substitution, NO selective rerun; a failed block is repeated FRESH):
1. exactly 4 cells, arms == the frozen sequence, presented in collection order;
2. **campaign binding**: one shared `abba_campaign_id`, `abba_seq` exactly 0..3 in order
   (no reorder, no cell borrowed from another campaign);
3. **provenance per cell**: `observer_image_verified==true`, `tree_dirty==false`, no
   contract deviation, stored log hashes still match the on-disk logs;
4. **re-analysis is the source of truth**: each cell re-run through `q3_steady_analyze`
   under its own validated `--calib` (sha == manifest) ⇒ `STEADY-METRICS-OK`;
5. **homogeneity**: matched observer image (HEX+ELF+.config) + peripheral firmware +
   geometry + calib + analyzer/runner/protocol/assert lineage + frozen `q3_contract`;
   the central image is IDENTICAL within an arm and DIFFERENT between arms. **Scope
   honesty:** "same-within, different-between" is NECESSARY but not SUFFICIENT to prove
   the arm is the ONLY difference — the cell dir archives image *digests*, not the
   configs, so the combiner cannot diff them post-hoc. The semantic binding that the
   central runs the CORRECT arm's config is the per-run `assert_fsu_config.py --arm`
   gate (enforced at collection, recorded in each manifest), NOT a post-hoc config diff;
6. **distinct reset-isolated connections**: four distinct connection AAs;
7. **current-lineage (F1)**: every cell's analyzer/runner/protocol/assert/combiner digest
   equals the CURRENT tree and its `q3_contract` equals `q3_contract()`;
8. **chronology (F2)**: absolute `abba_start_ns`/`abba_end_ns` strictly ordered +
   non-overlapping in A/B/B/A order;
9. **binary provenance (rev-5)**: each cell archives `firmware/<dev>.<ext>` (HEX+ELF+
   .config × observer/central/periph); the combiner RE-HASHES them against the manifest
   digests and reruns `assert_fsu_config.py` over the archived central (@arm) + periph
   `.config`. A fabricated digest, missing build file, post-cell edit, or wrong-arm
   config rejects.

Every accepting `--q3-steady` run is MANDATORILY bound to the campaign (nonempty
`--abba-campaign-id` + in-range `--abba-seq` + the arm the sequence demands), enforced
before any hardware mutation; a standalone steady diagnostic must use `--smoke`.

Verdict `ABBA-CONFIRMED` on pass, else `INCOMPLETE`.

## Implementation checklist (before freezing the accepted-cell protocol / running)
Order per review; the ±4-tick PRIMARY gate is fixed and must NOT be revised by the
smoke.
1. [x] **Runtime `F` FSU trigger + Q3 runner timing** — auto-at-connect scheduling
       removed; `CONFIG_APP_Q3_MODE` gates one `F` after CAPTURE-START + baseline
       dwell; request/central/peripheral completion timestamps recorded; transition
       excluded by the timestamp envelope; min sample counts enforced (plateau-shape
       still deferred, below).
2. [x] **Peripheral completion callback bound to AA/session** (`frame_space_updated`
       on q2-periph — peer participation; `q3_peer_participation` validates it).
3. [x] **Peripheral-side on-chip capture/drain** (`CONFIG_BT_CTLR_TIFS_CAPTURE_BENCH`
       on the PERIPH; clear at CAPTURE-START, freeze at CAPTURE-END, drain after;
       separate 150/100 exact-1µs bins; `q3_onchip_check` validates the lifecycle).
4. [x] **Q3 step analyzer + dedicated self-tests** — `analyze_q3.py` (distinct path):
       reuses `validate_common` (Q2 structural/config gates) + explicit Q3 whole-cell
       retention + validated `--calib` (baseline ±2) + strict control-plane/peer/
       on-chip/boundary-slop validators + presence-strict FSU schema bound to the
       observed connection (AA/session/registered arm); conservative whole-interval
       timestamp partition; 800±4 primary + µs on-chip cross-val + constrained
       `max-abs-mean-split` change point (diagnostic); 4-outcome null taxonomy +
       CONTRACT-DEVIATION. 28 self-tests incl. adversarial (missing Q2 evidence, low
       retention, stripped/missing fields, wrong-AA, alternate-arm, mismatched txn,
       invalid lifecycle, tampered calib, duplicate bins, zero/negative step).
5. [x] **Recombine the Q2 controls into a SEPARATE Q3 calibration artifact.** Every
       `analyze_q2.py` change re-stales the artifact's lineage, so it is REGENERATED
       (same two accepted controls, no Q2 rerun) whenever that happens; the prior file
       is preserved as SUPERSEDED. **Current: `gap-proxy-calibration-q3-r2-20260811.json`**
       (frozen 3047, passes current `validate_calib`). Superseded:
       `gap-proxy-calibration-q3-20260811.json` (r1, stale lineage after the MID
       window change). The Q2 artifact remains byte-identical.
6. [x] **Export/pin the final Zephyr controller changes** — the 5 uncommitted Q2
       diagnostic files committed on `fsu-m0`; the full `v4.4.1..fsu-m0` delta
       exported as a 9-patch series (`zephyr-patches/fsu-m0-series/`) with base
       commit, verified clean `git am` onto v4.4.1 (post-apply tree
       `cd5d1a718b…`, byte-identical), patch + source SHA-256, three-layer
       separation (FSU / bench-instrument / Q2 diagnostic — patch 0001 also carries
       the non-spec `INEVENT_ECHO`, required off in Q3; patch 0009 is hot-path
       instrumentation, not data-path-free), historical-overlap note, and archived
       resolved `.config` + `build_info.yml` provenance (HEX/ELF re-pin at step 8).
       Wording corrected per review: this is the CANDIDATE Q3 firmware base; on-air
       application is what Q3 measures, not an assumption.
7. [x] Resolve the FSU build. ROOT CAUSE: the 52-floor symbols
       (`CONN_INTERVAL_LOW_LATENCY`, `EVENT_IFS_LOW_LAT_US`) sit in the Kconfig
       `menu "Advanced features"` (`visible if BT_CTLR_ADVANCED_FEATURES`), so a
       clean build silently kept the 150 floor. FIX: `BT_CTLR_ADVANCED_FEATURES=y`
       (+ explicit `BT_CTLR_INEVENT_ECHO=n`) added to every FSU overlay. Pristine
       central + peripheral builds now resolve **tIFS = 52** and PASS
       `assert_fsu_config.py --arm {f100|f150|periph}` (advanced features + low-lat
       interval + tIFS=52 + host/ctlr FSU + FORCE_FEAT + 1M-only + registered request
       params + Q3 mode; bench on-periph/off-central; echo off). The gate is
       **runner-enforced**: every `--q3` run validates BOTH endpoint `.config` files
       BEFORE flashing and QUARANTINES (`q3-fsu-config-invalid`, recorded in the
       manifest with the script hash) on failure. All three arms (f100/f150/periph)
       built + archived (`.config` + `build_info.yml`) under `.../provenance/`.
8. [~] **Freeze the SMOKE protocol, then run the quarantined f100 mid-step smoke.**
       (a) [~] Stable-plateau SHAPE gate IMPLEMENTED + FROZEN in `analyze_q3.py`
       (IQR/drift/block/concentration), **calibrated from the six accepted Q2 cells**
       with a replay regression test (all six PASS) + adversarial tests
       (jitter/drift/spread). Pair-rate balance DEMOTED to a diagnostic (early-connection
       transient). Phase-SPECIFIC retention is now IMPLEMENTED + FROZEN via F-time
       `Q3PHASESNAP` counter snapshots (firmware `M` both roles + runner send +
       IDENTITY/TIMING-bound MID + analyzer gate at 0.95 + adversarial tests). The
       full acceptance contract is frozen. (b) [x] HARDWARE RUN DONE — **smoke-5**
       (`observer-q3-smoke5-20260812`, controller chain 0010→rev-6→rev-7→rev-8) reached
       METRICS-OK: RF step 802 t (AGREEMENT), on-chip 150/100 Δ=50 µs (xval |d|=0.12 µs),
       peer `initiator=PEER`, whole-cell 97.5/98.1 %, phase-retention all ≥0.95, shape.
       smoke-5 stays permanently QUARANTINED + non-evidentiary (`pre_run_dirty=true`);
       it is the pre-accept VALIDATION, not an accepted cell.
9. **Accepted-cell collection (this rev-3, in order):**
       (a) [x] Implement + freeze the **steady-state** path: **f150** = no request → one
           stable 150 µs plateau, only a 150 on-chip bin; **f100-steady** = FSU completes
           + settles ≥ 2000 ms BEFORE the measurement window → one stable 100 µs plateau.
           `q3_steady_analyze` frozen (6 hardened validity gaps + adversarial self-tests);
           runner `--q3-steady` sequences it; the within-connection f100 STEP analyzer
           stays the PRIMARY experiment. (see the steady-state contract above)
       (b) [x] Implement + freeze the **ABBA combiner** (`combine_abba.py`): drift-cancelling
           estimator `((A1−B1)+(A2−B2))/2`, ±4-tick tolerance, campaign binding, homogeneity
           (central differs only by arm), distinct-AA, re-analysis-as-source-of-truth,
           whole-campaign-or-nothing failure rule. 18-case self-test; every negative rejects
           at its intended gate. (see the ABBA contract above)
       (c) [x] **Separate promotion commit** (02d223a) + **rev-4 enforcement hardening**:
           acceptance ONLY for the registered mid-step + steady-state modes; `--smoke`
           forced-quarantine; reject contract deviations + wrong/unregistered arms;
           HARD-GATE `pre_run_dirty==false`; require verified observer + endpoint images
           (HEX+ELF+.config); the ABBA combiner binds to current lineage (F1), proves
           chronology via absolute timestamps (F2), pins the settle in the contract +
           refuses silent knob overrides (F3), and requires full-image digests (F4).
           Every bypass has a self-test that stays quarantined/rejected. **← pause here
           for review before any collection.**
       (d) [ ] Collect FRESH from a CLEAN tree: **2 reset-isolated f100 mid-step cells**
           (each independently METRICS-OK) + **4 steady-state cells in frozen ABBA order
           f150, f100, f100, f150**. Do NOT reuse or relabel smoke-5.
       (e) [ ] Report the evidence HIERARCHY: primary = replicated within-connection on-air
           step; confirmation = counterbalanced steady-state ABBA; mechanistic cross-val
           = peripheral CC3 READY; control plane = matching central + peer completions.
