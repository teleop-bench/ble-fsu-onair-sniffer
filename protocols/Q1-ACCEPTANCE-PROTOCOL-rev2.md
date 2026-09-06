# Q1 acceptance protocol — REV 2 (frozen 2026-08-10)

Supersedes `../observer-q0-20260810/Q1-ACCEPTANCE-PROTOCOL.md` + its amendment.
Rev-2 folds in the review of the first (1M, single-board) run. The corrected
fidelity reference and the tolerance are fixed HERE, derived from Q0 — not
chosen after any FSU/connected result is visible.

## Ground truth = the COMMANDED gap (not the generator self-timestamp)
`CC_B - CC_A = airtime_A + G*16` is exact hardware, so the observer's
ADDRESS→END span must read `G*16 + OFFSET` where OFFSET = preamble+AA on-air:
- **1M: 640 ticks** (5 bytes × 8 µs × 16)   ·   **2M: 384 ticks** (6 bytes × 4 µs × 16)
The generator self-timestamp `gap_gen` is NOT the reference: it carries the
generator's own fixed TX-PHYEND-vs-ADDRESS capture-latency artifact (~−50 ticks).
It is retained only as an inter-instrument *constant-offset* consistency check.

## Tolerance (derived from Q0, pre-specified)
±2 ticks = observer timer resolution (1 tick, 62.5 ns) + the observer's
Q0-measured ADDRESS-vs-END capture bias (−1 tick, from single-packet Q0). No
free parameter is fit to Q1 data.

## STRUCTURAL-INTEGRITY gates — any failure = hard REJECT
(These detect tooling/instrument corruption and DUPLICATION; they do not punish
benign RF loss.) Implemented in `z54-q1-generator/analyze_q1.py`, self-tested
(a doubled capture must REJECT):
1. generator: GEN-DONE present, `missed_deadline==0`, unique pair ids,
   `pairs==parsed`.
2. observer: FROZEN & LOSS present; `parsed REC == FROZEN.records == LOSS.records`.
3. observer: `oseq` contiguous 0..n−1 (no dup/gap) — catches concatenation/reboot.
4. observer: `ring_full_drops==0`; `addr_minus_end==end_minus_recorded==reversal
   ==stale_addr==0`; `addr_irq==end_irq==records`.
5. observer: NO pairid has >2 records (duplication); all pairids are commanded.

## METRIC gates (per rung G ∈ {150,100,70,52}, per PHY)
Reconstruct pairs from CLEAN records only (`crc==1 & status==0`); a
missed/CRC-corrupted packet is a RETENTION loss, not a reject.
1. **Retention** ≥ 95% clean reconstructed pairs / commanded, AND ≤ commanded.
2. **Fidelity** |median span − (G*16 + OFFSET)| ≤ 2 ticks.
3. **Spread** report IQR; "no observed spread at 62.5 ns" iff IQR=0 (sub-tick
   jitter is unresolved — do NOT say "zero jitter").
4. **Separation** adjacent-rung median deltas = commanded (G1−G2)*16 within
   ±2 ticks (bias-immune).

## RUN MATRIX (all required for a full Q1 PASS)
- PHY: **1M and 2M** (per plan §Q1).
- ≥ **2 reset-isolated** captures per config.
- **Each nRF54 DK** used as generator (board-position / TX-identity invariance:
  medians must not materially change between boards).
- Coordinated single run per capture (no `west flash` auto-run overlap; trim to
  last boot to defeat serial-DTR double-boot).

## SCOPE (unchanged, do not overclaim)
- A and B are two packet CLASSES from ONE generator (asymmetric len 20/30, tags
  1/2). They prove PAIR MEMBERSHIP, not two-transmitter / near-far behaviour —
  that is Q2.
- The ~+49-tick observer−generator offset is CONSTANT across rungs; its
  mechanism (fixed TX-emit vs RX-detect event-latency, cross-chip) is not fully
  isolated, so the generator self-timestamp is a consistency check, NOT an
  independent calibration. It does not affect retention/separation/delta.
- Q1 qualifies the INSTRUMENT on one channel with a synthetic source:
  NECESSARY-not-sufficient. Q2 (real connection, two endpoints, near/far) and
  Q3 (connected SDC) still gate before any FSU verdict.

## AMENDMENT (post-review auditability, 2026-08-10)
- Payload-derived structural checks (pairid multiplicity, commanded-pairid) are
  built from CRC-good & status-good records ONLY — a CRC-bad record's pairid/
  txtag bytes are untrustworthy, so it counts against retention, never a
  structural reject (fixes an internal inconsistency).
- Malformed/truncated REC lines now produce a controlled REJECT (a hard gate),
  never a crash.
- Both PHY builds (1M/2M) archived under firmware/ with commands + sha256;
  both generator boards self-identify via FICR DEVICEID (BOARD-ID-MAP.txt);
  every capture attempt is classified in DISPOSITION.txt.
