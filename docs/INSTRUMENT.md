# §6.1 capture instrument — build + solo de-risk (2026-08-07)

Overlay: `z54-lat-periph/fsu-fem-capture.overlay` (generic
`radio-fem-two-ctrl-pins`): ctx-gpios=P1.11 (TX-active), crx-gpios=P1.12
(RX-active), settle=0. P1.11/P1.12 are on gpiote20 (radio-routable — gpio2
has NO gpiote-instance and FAILED the build; only gpio0/gpio1 carry one)
and are the analyzer-verified header pins from the §14.9 watchdog bench.
Build: peripheral, `tput-open.conf;fsu-open.conf` +
`-DEXTRA_DTC_OVERLAY_FILE=fsu-fem-capture.overlay`. FLASH 131548 B, clean.

SOLO DE-RISK (no analyzer): FEM enabled → link connects, disc=0 across
40 s, FSU negotiates spacing=52, no FATAL/USAGE FAULT. Enabling the
radio-event pins does NOT perturb the low-latency path or break the link
(the primary risk). Evidence: fem-linkhealth-smoke-*.log.

REMAINING (bench, needs analyzer + user):
- PREREG gate 1 (pin-lands-on-boundary): wire P1.11, P1.12 + GND to the
  SparkFun analyzer; confirm crx(P1.12)-deassert → ctx(P1.11)-assert gap
  at spacing 150 reads a stable baseline (≈150 µs + fixed offset). Only
  then are captures accepted.
- PREREG gates 2-3 (≥16 MHz sampling; VDD/threshold check).
- Captures per the frozen PREREG.md: geometry A (1M 150→100), geometry B
  (2M 150→52), ≥5 events / ≥2 connections / arm, 2 s post-FSU settle.
- Central builds already exist: 1M mechanism = /tmp/fsu-c1m43-* family
  (150/100); 2M/52 = the physics f52/f150 central builds.

NOT on-air: physical-timing PROXY. The two DKs talk over the air; only
P1.11, P1.12, GND go to the analyzer. No board-to-board wiring.

## NEGATIVE FINDING (2026-08-07): FEM proxy does NOT work on nRF54L at steady state
Bench result: with the FEM correctly in the devicetree (verified in
zephyr.dts: ctx=P1.11, crx=P1.12) and the link healthy (periph pongs
+64/s, disc=0, RTT ~12 ms), the PA/LNA control pins DO NOT TOGGLE on
steady-state connection events. Evidence: all-8-channel and D4/D5 captures
across two builds (sink+blast and echo+latency), multiple resets, and
>1.5 s of clean 4 MHz capture — ZERO edges on the FEM pins. The only
activity ever seen was a handful of edges in a single ~50 ms probe right
after the first connection setup (transient, not reproduced). Wiring and
pin mapping are confirmed (those transient edges were on D4/D5). fx2lafw
also wedges on captures >~0.5 s at ≥16 MHz and on multi-second captures at
any rate (USB overrun; needs replug) — a separate instrument fragility.
CONCLUSION (scoped): the generic-FEM PA/LNA GPIO *instrument* is unusable
HERE — it did not produce steady-state markers in this build/config on
this rig. This does NOT prove the nRF54L FEM implementation is
conclusively defective (mechanism not root-caused; possible causes incl.
DPPI/GPIOTE channel allocation, the enable path not re-arming per event,
or our overlay). §6.1 needs a different instrument.

## AUDIT → better instrument (2026-08-07): subscribe markers to the
## controller's EXISTING event-capture DPPI channels
DPPI/GPIOTE ownership audit (per review step 4) found the controller
already publishes BOTH events I need, every packet, for its own EVENT_TIMER
capture scheduling:
- `hal_radio_end_time_capture_ppi_config()`: publishes RADIO
  END/PHYEND (`HAL_NRF_RADIO_TRX_EVENT_END`) on `HAL_RADIO_END_TIME_CAPTURE_PPI`.
- `hal_radio_ready_time_capture_ppi_config()`: publishes RADIO READY
  (`NRF_RADIO_EVENT_READY`) on `HAL_RADIO_READY_TIME_CAPTURE_PPI`.
DPPI is a broadcast bus, so adding a GPIOTE marker SUBSCRIBER to each
existing channel is non-conflicting (the controller's timer capture still
fires) — exactly the "subscribe, don't overwrite the publisher" path.
These events fire on EVERY packet (the controller depends on them), so
unlike the FEM enable path they are guaranteed live at steady state.
Measurement: END(rx) → next READY(tx) short gap = tIFS + fixed TX-ramp
offset; the 150→{100,52} change appears one-for-one, the offset cancels
in the delta. Long END→READY gaps (~interval) are ignored. REVISED PLAN (2026-08-08, per review): the GPIOTE-marker route would need
cross-domain wiring (RADIO on DPPIC10, gpio1/GPIOTE20 in another domain via
PPIB) — an UNVALIDATED, higher-risk inference, NOT established as the FEM
root cause, and UNNECESSARY. Instead read the controller's SHARED
EVENT_TIMER directly: `radio_tmr_ready_get()` (CC0) at the start of
`lll_conn_isr_tx` IS the RX-PHYEND→TX-READY interval (single-timer mode
clears the base on PHYEND; verified in radio.c). NOT CC0−CC2 (mixes
timebases). This is a controller hardware-event timing PROXY (shared timer,
not a dedicated one; stops at READY, before the on-air TX bit — NOT
on-air). Deferred output (ISR→ring buffer→thread emit), freshness/validity
gating, full per-record metadata. Frozen in PREREG-TIMER.md. Superseding
preregistration required before any accepted read (150 baseline smoke,
>=5 events/2 connections/arm, delta primary, marker-disabled control).
STATUS: audit complete, clean path identified; implementation pending.
Timeboxed per review step 4 (60-90 min); stop per step 5 if markers
aren't stable, then sniffer-or-defer.
