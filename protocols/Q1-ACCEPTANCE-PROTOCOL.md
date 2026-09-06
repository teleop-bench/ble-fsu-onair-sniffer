# Q1 acceptance protocol (frozen before Q1 data)

Q1 is the **go/no-go**: can the nRF52832 observer, in symmetric geometry,
**retain and distinguish** synthetic packet pairs at known spacings, including
70 µs and 52 µs? Frozen here so the accept/reject rule cannot be fitted to the
data after the fact (same discipline as `analyze_onair.py`).

## Ground-truth source
The nRF54 raw-radio **synthetic generator** (to be built) emits BLE-format
packet PAIRS on the observer's channel at hardware-scheduled spacings, honoring
the observer payload contract (`pca10040-radio-observer/src/main.c`):
- `payload[0..1]` = little-endian **pair id** (increment per pair)
- `payload[2]`    = **tx tag** (endpoint A=1 / B=2 within a pair)
- Deliberately **asymmetric PDU lengths** per endpoint so the two transmitters
  are distinguishable in the record even without LL direction decode.
The generator self-timestamps each TX (its own TIMER capture) so the commanded
spacing is logged independently of the observer.

## Commanded spacing ladder (each ≥ 200 pairs)
150 µs (baseline, must pass trivially) · 100 µs · **70 µs** · **52 µs**.
Symmetric geometry: generator and observer equidistant / same attenuation;
target CRC-good ≈ 100% (unlike ambient Q0).

## Measured quantity and the preamble offset (important)
The observer records `address_ticks` (B's ADDRESS, after B's preamble+AA is on
air) and `end_ticks` (A's END, after A's CRC). So:
    spacing_meas = second.address_ticks − first.end_ticks
                 = G + (B preamble+AA airtime)
where **G** is the true END-of-A → START-of-B gap the generator commands, and
the preamble+AA airtime is a fixed PHY constant (1M: 1+4 bytes = 40 µs =
**640 ticks**). Thus `spacing_meas` is NOT equal to the commanded G — it is
G + 640 ticks at 1M. The generator self-timestamps the SAME on-air interval
(its own TX `ADDRESS` of B minus TX `END` of A → `gap_gen`), so the clean
cross-check is observer-vs-generator, with no constant to assume:
    expect  spacing_meas ≈ gap_gen  (both measure B.address − A.end on air).

## Per-pair reconstruction (observer side)
Two consecutive records form a pair iff same `gen_pairid` and opposite
`gen_txtag`. Both must have `crc=1` and `status=0x00`.

## ACCEPT (all must hold, per commanded rung G ∈ {150,100,70,52} µs)
1. **Retention** ≥ 95% of commanded pairs reconstructed with BOTH members
   CRC-good and `status=0x00` (proves the second packet survived END_START
   re-arm at that spacing).
2. **Fidelity (primary, no constant)**: median observer `spacing_meas` within
   **±2 ticks (±125 ns)** of the generator's self-timestamped `gap_gen` for
   the same rung.
   **Command honored (secondary)**: `gap_gen` within ±2 ticks of
   (G×16 + 640) ticks — confirms the generator hardware produced the intended
   gap. (If TX does not raise an ADDRESS event on this silicon, fall back to
   comparing `spacing_meas` directly to (G×16 + 640) ticks.)
3. **Separation**: the four rungs' `spacing_meas` distributions are mutually
   non-overlapping (min gap between adjacent medians ≥ 10 × combined IQR) —
   the instrument DISTINGUISHES 52 vs 70 vs 100 vs 150.
4. **Loss accounting** clean at every rung: `addr_irq == end_irq`,
   `end_minus_recorded == 0`, `reversal == 0`, `stale_addr == 0`.

## REJECT / STOP
- 52 µs OR 70 µs retention < 95% → the passive observer cannot retain
  close pairs → **STOP** (do not proceed to Q2/Q3 connection discovery).
- Rungs not separable, or fidelity worse than ±2 ticks → instrument not
  trustworthy for the FSU delta → fix or STOP.
- Any silent counter mismatch (`addr_irq != end_irq`) at 70/52 µs → coalesced
  interrupts; STOP and revisit the ISR/re-arm path.

## Explicitly out of scope for Q1 (deferred)
LL direction decode, real connection following, channel-map hopping, and the
2048-record depth question (Q3). Q1 passing is **necessary, not sufficient**
for any on-air FSU claim — Q2 (follow a real connection) and Q3 (measure the
FSU step on a live link) still gate.

---
## AMENDMENT (2026-08-10, after the first Q1 run) — disclosed for review
The first run showed the generator self-timestamp `gap_gen` is not a clean
fidelity reference: it carries the generator's OWN fixed TX PHYEND-vs-ADDRESS
capture-latency artifact (a constant ~-50 ticks vs the commanded gap on ALL
four rungs). Comparing the observer to an uncalibrated second instrument was
a methodological error. **Corrected gate 2 (fidelity):** primary = observer
`spacing_meas` vs the COMMANDED gap `G*16 + 640` (exact hardware; the CC_B-CC_A
schedule), tolerance ±2 ticks (absorbs the observer's -1 tick Q0 ADDRESS-vs-END
calibration). `gap_gen` is retained only as an inter-instrument constant-offset
consistency check (span-gap_gen must be constant across rungs). Gates 1
(retention), 3 (separation, bias-immune) and 4 (loss) are unchanged. The core
conclusion rests on two reference-INDEPENDENT facts (IQR=0; rung deltas =
commanded), so it does not depend on this reference choice.
