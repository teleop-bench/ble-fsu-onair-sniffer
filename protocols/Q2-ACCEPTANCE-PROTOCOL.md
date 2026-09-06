# Q2 acceptance protocol — REV 12 (amended 2026-08-11, before any ACCEPTED cell)

Q1 qualified the observer against ONE synthetic source; **Q2 qualifies it on a
REAL BLE connection with TWO physical transmitters in near/far geometry**,
timing a genuine central↔peripheral exchange it does not control.

**Amendment history (all before any ACCEPTED observer/near-far cell):**
- rev-1 → rev-2: pre-data review (non-circular denominators, atomic snapshots,
  config binding, conservative window).
- rev-2 → rev-3: after ENDPOINT-ONLY health diagnostics (no observer capture)
  revealed that `sched` and `tx` are NOT one-to-one, the retention DENOMINATORS
  were changed from `sched` to the `tx`-delta counters (central = actual-TX
  completion; peripheral = CRC-good response-opportunity, conservative), the
  `sched`-equality gates were REMOVED, and snapshot role/boottag/channel/DEVICEID
  binding was added.
- rev-3 → **rev-4**: after ONE end-to-end SMOKE/PILOT observer capture
  (`debug-evidence/observer-q2-fullsmoke-20260810-QUARANTINED/`, 402 records /
  201 pairs / 95.3% — captured to prove the pipeline runs, **not accepted as a
  cell**), a review corrected four measurement-validity points, applied here
  BEFORE any accepted cell:
  1. **gap_proxy vs tIFS.** The observer's **previous-packet END → next-packet
     ADDRESS** span (`gap = b.addr − a.end` in the analyzer) is a `gap_proxy` =
     tIFS·16 + preamble/AA(640) − 1 tick, **not** tIFS. The pilot's ~3049-tick
     median is a gap_proxy; the calibrated tIFS estimate is
     ≈ (gap_proxy − 639)/16 µs ≈ 150.6 µs. The analyzer now reports
     `gap_proxy_dev` (ticks) AND the calibrated tIFS estimate, and STOPS calling
     the raw span a tIFS.
  2. **Candidate vs established.** A symmetric control that ACCEPTs yields an
     ACCEPTED CANDIDATE only; otherwise it is not a candidate. Near/far runs are
     compared against a FROZEN `gap_proxy_dev` (see rev-5).
  3. **Retention is a LOWER BOUND.** The `tx`-delta denominator brackets a
     slightly wider interval than the observer window (bounded by the snapshot
     slop), so paired retention is reported as "lower bound ≥ X%", not "= X%".
  4. **Tight-bracket slop gate.** The old fixed 3 s slop ceiling is incompatible
     with a ≥95% gate over a 10 s window. The gate is now COMBINED per-endpoint
     slop ≤ 3% (`SLOP_FRAC`) of the capture duration (with a `MAX_SLOP_MS` gross
     -error backstop per boundary); the runner's fixed 200 ms pre-END-snap delay
     was removed (snaps immediately follow CAPTURE-END).
- rev-4 → **rev-5**: a follow-up review closed the calibration contract BEFORE
  any accepted control:
  1. **A single run never ESTABLISHES.** One accepted symmetric run is an
     ACCEPTED CANDIDATE; the frozen reference is produced only by a separate
     combination of **≥2** accepted controls.
  2. **Cross-run reproducibility is now GATED, not just reported.**
     `frozen_gap_proxy_dev` = round-half-to-even(median of the accepted run
     medians); **every** control median must be within **±2 ticks** of that
     frozen value, else the calibration is INCONSISTENT and NO reference is
     established. (Rounding is explicit because the median of two integer medians
     can be half-integral.)
  3. **The frozen reference is PROVENANCE-BOUND.** `combine_calib.py` re-analyzes
     each control's raw logs, and writes `gap-proxy-calibration.json` recording
     each input's sha256 + verdict + median, the spread, and the frozen value.
     Near/far consumes that artifact via `--calib` (status must be ESTABLISHED),
     not a naked `--tifs-dev` integer (which is retained only for the self-test).
  4. **Manifest tightened**: full runner args, resolved connection config, source
     commit + tree-dirty flag, sha256 of observer hex / all three logs /
     analysis.txt / analyzer / runner / combiner / this protocol, optional
     endpoint build (hex + `.config`) hashes, and an `observer_image_verified`
     flag that is FALSE under `--skip-obs-flash`.
- rev-5 → **rev-6**: a review found the rev-5 contract was described but not
  ENFORCED end-to-end; this revision closes the bypasses (still before any
  accepted cell):
  1. **Unverified image is auto-QUARANTINED.** `--skip-obs-flash` now forces the
     cell verdict to QUARANTINED and the runner to exit nonzero (a passing
     analyzer no longer yields ACCEPT). The manifest verdict is QUARANTINED, and
     the combiner rejects it. A warning is no longer the only guard.
  2. **The combiner is PROVENANCE-GATED.** `combine_calib.py` now REQUIRES each
     control's `manifest.json` and rejects unless `verdict==ACCEPT`,
     `observer_image_verified==true`, and the manifest's stored log hashes still
     match the on-disk logs (catches quarantined/unverified runs, missing
     manifests, and post-hoc log edits). It records tool/source LINEAGE
     (commit + analyzer/combiner/protocol hashes) and each input's manifest hash
     into the artifact.
  3. **No naked timing value anywhere in production.** `--tifs-dev` is removed
     from BOTH the runner and the analyzer CLI; near/far and `--roleswap` accept
     ONLY a `--calib` ESTABLISHED artifact (role-swap applies ONE shared frozen
     value to both configs). The raw integer survives only inside the in-process
     self-test.
  4. **Auditable tree state.** The source-tree dirty flag is captured BEFORE the
     run creates any output under the repo, so writing logs no longer makes it
     spuriously true.
- rev-6 → **rev-7**: a review found the consumer trusted `status` alone and the
  combiner did not enforce matched replications; closed before any accepted cell:
  1. **Consumer re-verifies the WHOLE contract.** `validate_calib()` (shared by
     near/far and `--roleswap`) recomputes and checks: status ESTABLISHED, ≥2
     inputs each with verdict==ACCEPT + log/manifest hashes, `medians[]` matches
     the inputs, `frozen = round-half-even(median of medians)`, every median
     within ±2 ticks, contract fields equal the rev-7 constants, and
     analyzer/combiner/protocol LINEAGE hashes equal the current tools. A forged
     `{status, frozen}` blob is rejected.
  2. **Combiner enforces matched replications (HOMOGENEITY gate).** All controls
     must agree on run mode (symmetric), observer board + verified image,
     central/peripheral DEVICEIDs in the same roles, PHY/map/observer-channel,
     analyzer/combiner/protocol lineage, source commit, and endpoint
     firmware/config identity. AA/CRCInit legitimately differ per connection and
     are NOT matched.
  3. **Endpoint build provenance is MANDATORY for acceptance.**
     `--central-build/--periph-build` (hex + `.config` hashes) are required; a
     passing analyzer without them is QUARANTINED, and the combiner rejects a
     control lacking endpoint build hashes.
  4. **Explicit enums + dual exit codes.** Runner/analyzer `--mode` and `--near`
     are constrained (a misspelling can no longer fall through to near/far), and
     the manifest records `analyzer_exit` and `cell_exit` separately (a
     QUARANTINED analyzer-pass shows analyzer_exit=0, cell_exit=3).
- rev-7 → **rev-8**: a review found a build-provenance FALSE-ACCEPT (present-as-a
  -nonempty-string ≠ verified); closed before any accepted cell:
  1. **Endpoint provenance is RESOLVED, not assumed.** `--central-build`/
     `--periph-build` must each contain a readable `zephyr/zephyr.hex` and
     `zephyr/.config`; the runner hashes them BEFORE deciding the verdict and
     ACCEPTs only if both are real 64-hex digests. A nonexistent dir / missing
     artifact hashes to `MISSING` → QUARANTINED. The combiner INDEPENDENTLY
     re-enforces the same digest schema (endpoint builds + observer.hex must be
     64-hex, never `MISSING`/`UNVERIFIED`), and `validate_calib()` requires each
     artifact input to carry three valid 64-hex log digests + a valid manifest
     digest. So `--central-build /does/not/exist` can no longer produce an accept.
  2. **`source_commit` is NO LONGER a homogeneity-equality field.** Requiring
     equal HEAD across controls would force leaving HEAD unchanged across both
     runs (no intervening commit); the recommended flow is still "run both
     controls, combine, then commit the bundle under one HEAD", but the actual
     experimental identity is carried by the tool/observer/endpoint firmware +
     config hashes, so an intervening commit that leaves those files unchanged is
     harmless. `source_commit` is still recorded per cell for audit.
  3. **Stable observer image.** A fixed observer image is flashed so both controls
     share one `observer.hex` (a fresh `west build` is not bit-reproducible and
     would fail the homogeneity gate). Build once, run both controls against it.
- rev-8 → **rev-9**: a review found rev-8's integration claims were not all true
  in the committed code; closed before any accepted cell:
  1. **`--obs-prebuilt` could not flash.** `west flash --hex-file` alone fails
     (`--build-dir was not given`; it needs `runners.yaml`). Replaced with
     `--obs-prebuilt-build DIR` → `west flash -d DIR --no-rebuild --hex-file
     DIR/zephyr/zephyr.hex`, after validating the dir has `zephyr.hex` + `.config`.
  2. **Endpoint hashes did not prove the RUNNING image.** rev-8 only reset the
     endpoints and hashed whatever dirs were supplied (two unrelated valid builds
     would still ACCEPT). The runner now FLASHES both supplied endpoint builds
     (`west flash -d DIR --no-rebuild`) before the cell, so the hashed image IS the
     running image.
  3. **`validate_calib()` checked digest SYNTAX, not the evidence.** An artifact
     with nonexistent input dirs and fabricated `aaaa…` digests still returned a
     frozen value. The consumer now RESOLVES each input dir (relative to the
     artifact) and REHASHES the logs + manifest against the recorded digests, and
     re-checks the referenced manifest's ACCEPT verdict + image/build digest
     schema. Fabricated or post-combine-edited evidence is rejected.
- rev-9 → **rev-10**: a review found two residual trust gaps; closed before any
  accepted cell:
  1. **Explicit integration-smoke mode.** A normal run with full provenance +
     passing analyzer would ACCEPT, so a "smoke" of the flash paths could
     accidentally become the first accepted cell. `--smoke` (alias
     `--force-quarantine`) runs every flash + capture + analysis but FORCES
     `verdict=QUARANTINED`, nonzero `cell_exit`, and
     `quarantine_reason=integration-smoke`. Self-tested: pass + complete
     provenance + `--smoke` still QUARANTINES.
  2. **Consumer RE-ANALYZES, not just rehashes.** `validate_calib()` previously
     trusted the artifact's reported medians; an internally-consistent
     median+frozen edit (3039→3040) over untouched evidence still validated. It
     now re-runs the symmetric analyzer over each referenced log bundle and
     requires the recomputed median to equal the artifact's input median,
     re-checks each manifest's internal log hashes, and REAPPLIES the cross
     -control homogeneity gate — all through the SAME `combine_calib` code the
     producer used, so consumer and producer cannot diverge. A homogeneity
     substitution (repointing an input to a board-mismatched cell) is rejected.
- rev-10 → **rev-11**: the FIRST hardware integration smoke
  (`debug-evidence/observer-q2-smoke-20260811/`, `--smoke` so QUARANTINED/REJECT,
  never accepted) validated the flash paths and surfaced two IMPLEMENTATION bugs
  (no acceptance-contract/threshold change); fixed before any accepted cell:
  1. **Interleaved `Q2CONN` parse.** On the shared endpoint console the
     controller's `Q2CONN` printk can collide with the app's `Q2EVT` line
     (`Q2EVT role=CQ2CONN AA=...`). `parse_conn` matched only at line start and
     read 0 connections → spurious REJECT. It now searches anywhere for
     `Q2CONN AA=…` (as the runner already did); the AA is still cross-checked
     against both `Q2SNAP` aa's and the map/chan_count gates, so a mangled
     `Q2CONN` can only REJECT, never false-ACCEPT.
  2. **Snapshot slop over the 3% ceiling.** At the 10 s window the runner's 100 ms
     `wait_for` poll put the boundary snapshots ~350 ms combined outside the
     capture edges (> the 300 ms = 3% ceiling). Tightening the poll to 20 ms
     lands each snapshot closer to the edge → ~260 ms combined (< ceiling) → a
     genuinely tighter retention lower bound. No firmware/window change was
     needed. (The smoke also confirmed gap_proxy_dev ≈ 3049 ticks / tIFS ≈
     150.6 µs and 97.2% retention with the boards in an arbitrary, non-symmetric
     placement.)
- rev-11 → **rev-12**: a first accepted-control attempt REJECTed at 94.9%
  retention — just under 95%, dragged there by that run's higher snapshot slop
  (~260 ms = ~2.5% of a 10 s window), not by real packet loss (true retention
  ~97%). Rather than re-run until a low-slop run passes (selection bias), the
  observer capture window was lengthened **10 s → 30 s** (`CAP_MS`), so the same
  fixed ~250 ms slop is <1% of the window and the retention lower bound reflects
  true capture quality (~97%) reliably. This also triples the per-run event count
  (tighter gap_proxy median). The ring (2048) holds ~1230 records at 30 s with
  headroom. The analyzer's duration gate now reads the observer's DECLARED
  `cap=Nms` (± `CAP_TOL`) instead of a hardcoded 10 s range, so the window length
  is not baked into the analyzer; the slop ceiling was already a fraction of the
  capture duration and auto-scales. (Also noted, not gated: on these dev kits the
  USB cable is often the dominant radiator, so received RSSI is anchored near
  where the cables converge — convenient for the symmetric Δ≤6 dB control, but
  near/far will need real cable-separation/attenuation, not just PCB distance.)
  This is a legitimate pre-acceptance amendment: no accepted observer/near-far
  cell has been collected under any revision.

## Setup
- **Central** = nRF54L15 (open controller, `CONFIG_BT_LL_SW_SPLIT=y` — required
  to read the connection AA/CRCInit). **Peripheral** = nRF54L15. **Observer** =
  nRF52832 (passive raw-radio instrument).
- Fixed 7.5 ms interval, **1M**, **tIFS = 150 µs standard (no FSU)**. Each
  connection event = C→P then, 150 µs later, P→C.
- **Channel map pinned to 2 channels {10,11} BEFORE connecting** (host channel
  classification set pre-connect), so the connection is CREATED with that map —
  no channel-map-update transition/instant. The controller `Q2CONN` print logs
  the ACTUAL applied `map=` + `chan_count=` (must read 2); HCI success alone is
  not accepted as proof. Observer parks on ch10.
- **AA/CRCInit are READ, not pinned**: an UNCONDITIONAL controller diagnostic in the patched TEST tree
  (`zephyr-patches/q2-print-conn-params.patch`; not Kconfig-gated -- revert for
  production) prints the central-minted
  `AA=LE32(access_addr)` + `CRCINIT=LE24(crc_init)` at `ll_create_connection`.
  The observer build REQUIRES `-DQ2 -DAA=.. -DCRCINIT=.. -DCHAN=10` (no silent
  synthetic fallback; CHAN range-checked 0..36).

## NON-CIRCULAR retention denominator (the load-bearing fix)
"Fraction of events the observer captured" is circular — events where the
observer misses BOTH packets are invisible. Instead each endpoint's controller
keeps TWO per-channel counters (volatile, CONTROLLER-OWNED, FREE-RUNNING — never
reset by the app, so no race; see Config-binding section for the window/AA id):
- `lll_conn_q2_evt[ch]` = SCHEDULED opportunities (incremented at `lll_chan_set`
  in each role's prepare).
- `lll_conn_q2_tx[ch]` = the DENOMINATOR counter, with a ROLE-SPECIFIC meaning
  (renamed conceptually "txcount"): on the CENTRAL it is ACTUAL TX-completion
  (`lll_conn_isr_tx`); on the PERIPHERAL it is a CRC-GOOD RESPONSE-OPPORTUNITY
  (`lll_conn_isr_rx`, crc_ok), which is CONSERVATIVE (>= actual transmitted
  responses -- e.g. multi-PDU events). Both index the ACTUAL remapped per-event
  channel (`lll_conn_q2_curchan`), NOT `lll->data_chan_use` (CSA#1 unmapped
  state). Q2 is SINGLE-CONNECTION scope: `curchan` is one global, valid only for
  one live connection on the bench.
- `sched` and `tx` are NOT one-to-one on real runs (measured: periph tx can
  slightly exceed sched; central tx != sched transiently). `sched` is a
  connection-HEALTH / packets-per-event DIAGNOSTIC ONLY -- never an equality
  bound.
The app reports both + the connection AA + session: `Q2EVT role=C/P aa=0x..
sess=<s> t=<ms> ch=10 sched=<n> tx=<n>`. Denominators use the RIGHT counter per
role -- each is `observed / txcount-delta` (NOT sched):
```
central retention    = clean observed CENTRAL pkts / (central ACTUAL-TX delta)
peripheral retention = clean observed PERIPH  pkts / (periph  RESP-OPP delta, conservative)
paired retention     = complete observed C+P pairs / (periph  RESP-OPP delta)
diagnostic           = tx/sched per role (packets-per-event; health only, not gated)
```
The denominator is guaranteed conservative by COMMAND ORDERING (not arrival-time
heuristics -- HOSTMS is arrival, not generation, and USB buffering can reorder
across ports). ARMED/GO/snapshot handshake: the observer boots, prints `ARMED`,
and WAITS for a `G` byte. The runner (a) commands both endpoints (`S` byte) to
emit an ATOMIC START `Q2SNAP` (seq=0), (b) sends the observer `G` ->
`CAPTURE-START`..`CAPTURE-END`, (c) after CAPTURE-END commands both endpoints to
emit an ATOMIC END `Q2SNAP` (seq=1). Since the START snaps are requested+received
before `G` is sent, and the END snaps after CAPTURE-END -- all on the runner's
SINGLE host clock (verified: START_snap_host <= Ts, END_snap_host >= Te) -- the
endpoint counter interval [START,END] CONTAINS the observer interval, so the
denominator is >= the true count -- which makes every retention a LOWER BOUND
(reported "≥ X%", never "= X%"; the true value is higher by the over-counted
slop). Because that over-count directly loosens the bound, the COMBINED
per-endpoint slop is gated to ≤ 3% (`SLOP_FRAC`) of the capture duration (a
`MAX_SLOP_MS` per-boundary backstop rejects gross bracket errors); the runner
takes the END snaps immediately after CAPTURE-END (no fixed delay). The analyzer
REJECTS: not exactly one CAPTURE-START/CAPTURE-END/FROZEN; missing/duplicate
snapshots; snap ordering violated; capture duration not ~CAP_MS; combined slop >
3% of capture; AA/session change or counter reset between snaps; numerator >
denominator. Report all three retentions; the FAR side's is the stressor.

## Direction attribution — INFERRED, not decoded (mode-dependent)
A BLE data-PDU header carries LLID/NESN/SN/MD/CP/length — **no direction bit**.
- **SYMMETRIC mode**: the two RSSI populations OVERLAP by design (Δ ≤ 6 dB), so
  RSSI cannot attribute. Use intra-event ORDERING ONLY (first-in-event pairing)
  for timing + paired retention. No RSSI split is attempted (a forced 2-means on
  overlapping noise would manufacture spurious populations).
- **NEAR/FAR mode**: attribute by the RSSI population (2-means split; near vs
  far) and VALIDATE against ordering. The **role swap** (config A central-near
  vs config B periph-near, via the tx-swap.conf overlays / physical placement)
  must swap the RSSI population of the first-in-event packet while ordering is
  unchanged. Gate: 0 ordering-vs-RSSI disagreements among clean pairs, in BOTH
  role assignments; two RSSI populations separable (Δ ≥ 20 dB). Labeled inferred.

## gap_proxy / tIFS fidelity — endpoint-derived tolerance (anchor jitter does NOT apply)
The observer measures a `gap_proxy` = prev-packet END → next-packet ADDRESS,
which is the intra-event C→P interval PLUS the preamble+AA of the second packet:
`gap_proxy ≈ tIFS·16 + 640 − 1` ticks. It is NOT the physical tIFS; the
calibrated tIFS estimate is `≈ (gap_proxy − 639) / 16 µs`. This intra-event
interval is unaffected by connection-anchor drift / window-widening (those affect
first-packet ACQUISITION across events), so the tolerance is an ENDPOINT-timing
allowance, not anchor jitter. Procedure: characterize the device's actual
gap_proxy in the SYMMETRIC control run(s); with ≥2 reset-isolated ACCEPTED
controls `combine_calib.py` sets the frozen `gap_proxy_dev` =
**round-half-to-even(median of the per-run medians)** and GATES every control
median to **±2 ticks** of it (else INCONSISTENT → no reference); the max
cross-run spread is recorded in the artifact. Expected symmetric gap_proxy ≈
150·16+640−1 = 3039 ticks (calibrated tIFS ≈ 150 µs). Gate: in every near/far
run, median gap_proxy within **±2 ticks (instrument resolution + Q0 calib)** of
the frozen `gap_proxy_dev`; report the spread (IQR). Context only: LL says T_IFS
SHALL be 150 µs (the nearby 2 µs is a RECEIVER timing allowance, NOT a
transmitter tIFS tolerance). A single accepted symmetric run is only an ACCEPTED
CANDIDATE; only the ≥2-control combination establishes the frozen value.

## Weak-side stress — absolute far-side RSSI, with a symmetric control
A 15 dB delta between two very strong signals is not a weak-side test. Freeze:
- **Symmetric control(s)** first: both transmitters at similar RSSI at the
  observer (Δ ≤ 6 dB) — each accepted run is a `gap_proxy_dev` CANDIDATE; ≥2 are
  combined into the frozen `gap_proxy_dev` reference, plus baseline retention and
  attribution sanity, with no near/far confound.
- **Near/far**: FAR-side absolute median RSSI in **[−85, −70] dBm** (approaching
  the receiver floor), achieved by TX-power (role-swap build overlays q2-*/tx-swap.conf reverse
  which endpoint is strong) AND physical distance/attenuation (power alone
  keeps both signals strong -- the far side needs real attenuation to reach the
  weak band), with near−far Δ ≥ 20 dB. Record actual RSSI distributions. A far side that
  stays > −60 dBm does NOT exercise the weak case → rerun with more attenuation.

## Config binding + window (analyzer hard gates, all must pass)
The connection Access Address is the SHARED id: the controller sets it race-free
at setup (central `ll_create_connection`, periph on CONNECT_IND copy) and both
apps print it in `Q2EVT aa=..` (ATOMIC snapshot under irq_lock); the observer
prints its build AA. The runner (`q2_run.py`) does: capture EXACTLY ONE Q2CONN -> build/flash the
observer FROM it -> ARMED/GO/snapshot handshake (atomic Q2SNAP boundaries) ->
wait Q2-DONE -> analyze. Endpoints answer 'S' with an atomic Q2SNAP; the observer
waits for 'G'. The analyzer REQUIRES:
- exactly one Q2CONN; `chan_count==2` AND `map==000c000000` (channels {10,11}).
- observer AA/CRCInit == connection; observer channel ∈ {10,11}; observer PHY 1M.
- central `aa` == periph `aa` == observer AA (the shared connection id).
- `aa` constant within the bracket (else reconnect → REJECT); counters monotonic.
- COMBINED per-endpoint snapshot slop ≤ 3% of capture duration (tight lower
  bound); per-boundary `MAX_SLOP_MS` backstop.
- near/far requires a PROVENANCE-BOUND frozen reference: `--calib
  gap-proxy-calibration.json` whose WHOLE contract re-verifies via
  `validate_calib()` (status ESTABLISHED, ≥2 accepted inputs, medians reproduce
  the frozen value, spread ≤ ±2, contract fields + tool/protocol lineage match the
  current tools, AND every referenced input dir is RESOLVED relative to the
  artifact, its logs+manifest REHASHED against the recorded digests, RE-ANALYZED
  so the recomputed median equals the artifact's, and the cross-control
  homogeneity gate reapplied). A naked `--tifs-dev` integer is self-test-only; a
  forged `{status, frozen}` blob, fabricated evidence, an internally-consistent
  median edit, or a homogeneity substitution is rejected.
- cross-run role-swap check (`--roleswap`): the near/strong population must flip
  first<->second-in-pair between config A and config B.
- central & periph txcount deltas > 0 (link exchanging). NO sched-equality
  gate (sched/tx are not one-to-one).
Counters are CONTROLLER-OWNED and FREE-RUNNING (no app reset → no race); the
denominator = END − START atomic `Q2SNAP`, ordering-guaranteed to contain the
observer interval (START snap requested before GO; END snap after CAPTURE-END).

## ACCEPT gates (all retentions are LOWER BOUNDS — "≥")
1. Applied map logged = {10,11}, chan_count=2 (pre-connect, no transition).
2. Symmetric control (≥2 reset-isolated): paired retention lower bound ≥ 95%;
   attribution methods agree; each accepted run is a CANDIDATE. `gap_proxy_dev` is
   ESTABLISHED only by `combine_calib.py` over ≥2 accepted controls: frozen =
   round-half-to-even(median of run medians), and EVERY control median within ±2
   ticks of it (else INCONSISTENT → no reference). A single control never
   establishes.
3. Near/far: **FAR-side** central AND peripheral retention lower bound ≥ 95% (the
   real gate); near-side ≥ 95%; paired ≥ 95%.
4. Attribution: 0 method-disagreements among clean pairs, in BOTH role swaps.
5. gap_proxy: median within ±2 ticks of the frozen `gap_proxy_dev` every run;
   spread (IQR) reported; calibrated tIFS estimate ≈ (median−639)/16 µs reported.
6. RSSI: symmetric Δ ≤ 6 dB; near/far Δ ≥ 20 dB AND far-side median ∈ [−85,−70].
7. Combined snapshot slop ≤ 3% of capture; observer loss accounting clean; Q2
   analyzer structural gates all pass.

## REJECT / STOP
- FAR-side retention < 95% → characterize the RSSI floor; do not claim connected
  near/far capability. · Attribution disagreement > 0 → not trustworthy; STOP.
- Far-side RSSI not in the weak band → geometry didn't stress near/far; rerun.

## Run matrix
- Symmetric control (≥2 reset-isolated) → near/far config A (central-near) ≥2 →
  near/far config B (periph-near, roles/powers swapped) ≥2.
- **Combination of the ≥2 symmetric controls (`combine_calib.py`):** it first
  PROVENANCE-GATES each control (its `manifest.json` must be present with
  `verdict==ACCEPT` and `observer_image_verified==true`, and its stored log hashes
  must still match the on-disk logs), then HOMOGENEITY-GATES the set (matched
  replications: same mode/board/observer-image/DEVICEIDs-in-roles/PHY/map/channel/
  lineage/endpoint-build identity, all as VALID 64-hex digests; AA/CRCInit and
  source-commit excluded), re-analyzes
  each control's raw logs (a stale `analysis.txt` is never trusted), requires each
  to ACCEPT, sets frozen `gap_proxy_dev` = round-half-to-even(median of run
  medians), gates every control median to ±2 ticks of it (else INCONSISTENT → no
  reference), and writes the provenance-bound `gap-proxy-calibration.json`
  (per-input sha256 + manifest hash + source commit + verdict + median, spread,
  frozen value, tool/source lineage). Near/far config A and B both gate against
  this single artifact via `--calib`, whose whole contract is re-verified by
  `validate_calib()` -- INCLUDING resolving each referenced input dir and
  rehashing its evidence (there is no naked timing input anywhere).
- Same hardened lineage; `analyze_q2.py` + `combine_calib.py` + `q2_run.py` frozen
  + self-tested (invisible-event denominators, pairing, attribution, role-swap
  consistency, RSSI gates, slop gate, loss accounting, calibration combine/spread,
  CLI enforcement + enum guard, manifest provenance + homogeneity + digest-schema
  gates, `validate_calib` schema/forgery/lineage/evidence-rehash, endpoint build
  resolution, verdict/quarantine table) BEFORE the first capture.
- **Evidence archiving (every cell):** the runner FLASHES the supplied observer +
  both endpoint builds (so hashed == running image), then writes `analysis.txt`
  (analyzer stdout + verdict) and `manifest.json` — full runner args, resolved
  AA/CRCInit/map/channel, source commit + PRE-RUN tree-dirty flag, board,
  DEVICEIDs, endpoint build (hex + `.config`) hashes, the calibration-artifact
  hash, `analyzer_exit` + `cell_exit`, and sha256 of the observer hex (or
  `UNVERIFIED` under `--skip-obs-flash`) + all three logs + analysis.txt + analyzer
  + runner + combiner + this protocol. The cell verdict is ACCEPT only when the
  analyzer passes AND the observer image is verified AND both endpoint builds were
  flashed with valid digests; `--skip-obs-flash` or missing/invalid
  `--central-build/--periph-build` forces QUARANTINED (nonzero exit) so it can
  never be accepted or feed the combiner.

## Out of scope for Q2 (→ Q3)
Full CONNECT_IND sniff + AA/CRC/hop auto-discovery; full 37-channel hop
following; connected SDC (closed); FSU-reduced tIFS on a live link. Q2 passing
is NECESSARY-not-sufficient; Q3 still gates before any on-air FSU verdict.
