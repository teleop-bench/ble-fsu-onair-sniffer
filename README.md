# BLE-6 FSU on-air sniffer

A **novel tool to physically measure Bluetooth 6.0 Frame Space Update (FSU) inter-frame spacing
(tIFS) on the air** — not just decode the LLCP negotiation, but timestamp the actual RF and confirm
the controller really shortened the gap. Built because **no public tool measures achieved on-air FSU
spacing** (Wireshark tracks the FSU opcodes `0x3B/0x3C` but not the µs fields), and a professional
protocol analyzer (Ellisys / Frontline) costs on the order of **tens of thousands of dollars**. The
instrument here is a **~$50 nRF52 DK**.

Extracted from the [BLE throughput/latency benchmark](https://github.com/teleop-bench/BLE-bench-pub)
(the proof-of-work history lives there).

## What it is
- **`observer/`** — passive raw-radio **timestamping sniffer** on an **nRF52832** (PCA10040). The
  BLE controller is off; it drives `RADIO`/`TIMER0`/`PPI` directly, captures `RADIO.ADDRESS`→`TIMER0`
  and `RADIO.END`→`TIMER0` per packet at 16 MHz, and dumps tiny timestamped records. The inter-frame
  gap is `gap_proxy(next) = next.address_ticks − prev.end_ticks`, calibrated to remove fixed biases.
- **`generator/`** — an **nRF54L15** (PCA10156) hardware-scheduled **packet-pair source** (TIMER10
  compare → RADIO TXEN over DPPI) that emits pairs at *commanded* spacings — the ground-truth
  calibration signal.
- **`analysis/`** — the Python pipeline: `analyze_q1/q2/q3/q3_2m.py` (per-stage analyzers with
  structural + adversarial self-test gates), `combine_calib.py` / `combine_abba.py` (calibration and
  drift-cancelling ABBA confirmation), `q2_run*.py` (drive endpoints + observer + analyze), and
  `assert_fsu_config.py` (gate the FSU Kconfig before flashing).
- **`protocols/`** — the frozen acceptance protocols (Q1 retention/discrimination, Q2 live-link
  calibration, Q3 FSU physical measurement) that promotion is gated on.
- **`docs/INSTRUMENT.md`** — the capture design.

## It's validated (not just a script)
- **Q0/Q1 — pipeline + resolution:** timestamps grounded to ±1 tick (62.5 ns); the observer
  **retains ~99–100 % of 52/70/100/150 µs pairs and separates them to 1 tick** (IQR = 0), at **1M and
  2M**, across both boards and reset-isolated reps.
- **Q2 — live 2-endpoint link:** calibrated tIFS = **150.50 µs** (dead-on the 150 µs standard);
  geometry-independent across a 40 dB near/far span.
- **Q3 — FSU physically measured on-air:**
  - **1M, 150→100 µs:** ABBA-confirmed (`~49.94 µs, |d|=1`) and **formally accepted** — see
    `evidence-sample/RESULTS.md`.
  - **2M, 150→52 µs:** now **formally accepted** — 3 primary mid-step AGREEMENT cells (`|d|≤1`,
    on-air==on-chip `|d|≤0.06`) + a 4-cell **ABBA-CONFIRMED** (drift-cancelled step 1567.75 t, `|d|=0.25`),
    all non-smoke / clean-tree / provenance-bound, via the ported 2M tooling (`assert_fsu_config_2m`,
    `combine_calib_2m`, `combine_abba_2m`). See `evidence-sample/RESULTS.md`. (Still observer-based,
    not independently pro-analyzer qualified.)

## Build & run
**Measuring FSU on a live link (the main use)** needs two nRF54L15-DK endpoints running the patched
open Zephyr controller with Frame Space Update. Those endpoint apps and the controller patches live in
[BLE-bench](https://github.com/teleop-bench/BLE-bench-pub) (Zephyr fork:
[teleop-bench/zephyr](https://github.com/teleop-bench/zephyr), branch `fsu-m0-v442`), so run live-link
measurements from a BLE-bench checkout. One command detects the three boards, builds all images,
flashes, captures and analyzes:
```
tools/observer-smoke.sh 2m      # run in a BLE-bench checkout
```
A pass prints `METRICS-OK`: at 2M the on-air gap steps 150.4 → 52.4 µs (1568 ticks ±4), cross-checked
against the peripheral's on-chip timer. See BLE-bench's REPRODUCE.md, *On-air FSU observer*, for pass
criteria and the accepted-cell (mid-step / ABBA) recipes.

**Building the firmware here** (Zephyr west workspace):
```
# Observer (nRF52832 DK / PCA10040), live connection, runtime-configured by the runner:
west build -b nrf52dk/nrf52832 observer -- -DQ2=1 -DAIRTIME_MIN_TICKS=256   # 2M link
west build -b nrf52dk/nrf52832 observer -- -DQ2=1                           # 1M link (see note)
# Calibration with synthetic packet pairs: observer + nRF54L15 generator, same PHY on both
west build -b nrf52dk/nrf52832 observer -- -DQ1=1 [-DPHY2M=1]
west build -b nrf54l15dk/nrf54l15/cpuapp generator -- [-DPHY2M=1]
```
A fresh 1M live-link measurement currently can't be analyzed: the frozen 1M calibration is bound
to the analyzer versions that predate the 2M port, so it needs a new calibration campaign first. The
accepted 1M result stands on its archived record.

`-DAIRTIME_MIN_TICKS=256` is required at 2M (a 2M empty PDU is ~320 ticks; the default 1M floor drops
it and breaks the gap chain). The observer prints on the nRF52 DK's J-Link **vcom 0** port, gated on
DTR; a healthy boot ends with `CONFIG-READY Q2`.

**`analysis/` is a byte-identical mirror** of BLE-bench's `apps/misc/q2-central/` tooling (plus the
two config asserters from `zephyr-patches/fsu-m0-series/` and `analyze_q1.py`), synced from BLE-bench `main` @ `77e7fc0` (2026-10-03).
The runners and combiners resolve paths relative to a BLE-bench checkout, and the combiners hash-bind
these exact files to the archived evidence (editing them breaks re-verification), so run them from
BLE-bench. The copies here are for review.

## Caveats (read before trusting a result)
- **It is loss-limited** (a single antenna, offset from the link): its PER is an *upper bound*, and on
  an **on-air *negative*** it cannot distinguish "no reduced-spacing packet on air" from "I missed it"
  — an on-air negative requires a **positive control** (read a *known* sub-150 µs spacing first). It is
  **not a substitute for a professional analyzer** for third-party qualification.
- **Observer is nRF52-only** — it bit-bangs `NRF_RADIO`/`TIMER0`/`PPI`/`END`; nRF54 uses `DPPIC`,
  `PHYEND`, and `XOSTART` (the `generator/` already solves those on nRF54, so a port is feasible but
  not done — it won't build on nRF54 as-is).
- **Reproduce deltas/steps, not absolutes** — the *gap* it measures is the result, not any single
  timestamp.

## License
Apache-2.0 (see LICENSE) — the firmware are Zephyr applications; the analysis tooling is original.
