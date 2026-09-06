# Sniffer capture plan — on-air packet analysis

The air sniffer serves **two roles across the project's phases**:
- **Phase 1 (throughput) — Jobs A & B below:** *measure* on-air quantities the
  endpoints can't (packets-per-event; on-air PDU size).
- **Phase 2 (safety qualification) — Jobs C–E, at the end:** *attribute* — tell
  you which layer (RF link vs stack vs app) a latency/loss/failsafe problem lives
  in. See [`PHASE2-SAFETY-QUALIFICATION.md`](PHASE2-SAFETY-QUALIFICATION.md).

In Phase 2 the endpoints are the *primary* instruments (round-trip latency,
sequence-gap loss, watchdog failsafe timestamp); the sniffer is the
**ground-truth-of-the-air** that corroborates and attributes them.

## Phase 1 jobs

The air sniffer has **two Phase-1 jobs** (the 2nd came out of the §5.1 BlueZ re-test):

**Job A — nRF path: count packets-per-connection-event. ✅ DONE 2026-07-29.**
Result: **4 packets/event** (89.7 % of 10,346 events; mean 3.83), and the mechanism
is **refill-starve, not early-close** — the dongle holds **MD=1 (99.1 %)** yet emits
**empty PDUs mid-event (97.5 %, ~1.9/event)**: the app can't refill fast enough.
Lever to go higher = feed the controller faster, *not* more `FORCE_MD`. Full
analysis: [`sniffer-data/throughput-packets-per-event.md`](sniffer-data/throughput-packets-per-event.md).
Original question (now answered):
- event **closes early** (dongle sets MD=0 with data still queued) → push
  `FORCE_MD_COUNT` / event length — **ruled out (MD=1 held)**, **or**
- dongle **runs dry mid-event** (gaps / empty PDUs, app can't refill) → fix the
  blast-thread feed / L2CAP credits — **confirmed**.

**Job B — RTL8761/BlueZ path: on-air LL PDU size. ✅ DONE 2026-07-29.** Result:
RTL8761 emits **≤54-byte on-air PDUs (mean 45 B, max 54, zero ≥200)** despite DLE-251
negotiated, while packing **~4 PDUs/event (mean 3.79) — the SAME as the nRF**. Root
cause read from the chip: **LE ACL Data Packet Length = 27 B** (`hcitool cmd 0x08 0x0002`
→ `1B 00`), the pre-DLE minimum, so BlueZ can only feed it 27-byte chunks — a fixed
firmware limit, not tunable. **So the ~4.6× central gap is on-air PDU size, NOT
packets-per-event** (245÷~50 ≈ 4.6× = 117÷26). Analysis:
[`sniffer-data/rtl8761-job-b-pdu-size.md`](sniffer-data/rtl8761-job-b-pdu-size.md). Original notes below. In the
§5.1 re-test, `btmon` showed DLE negotiated to **251** but BlueZ handing the
controller **27-byte HCI fragments** — and HCI-level `btmon` **cannot** show
whether the RTL8761 transmits **251-byte PDUs on air or re-fragments to 27**. Only
the sniffer settles it. If on-air PDUs are ~27 bytes, that per-packet overhead
explains a chunk of the ~4.5× gap vs the nRF dongle (both @ 7.5 ms/2M/DLE-251); if
they're ~251, the gap is purely packets-per-event. Sniff the **BlueZ/RTL8761 → DK**
connection (same DK, Linux TP-Link as central) and read the on-air `btle.length`.

---

## 0. Rig state to start from (verified 2026-07-27)

| Role | Device | Notes |
|---|---|---|
| Peripheral / receiver | nRF52832 **DK** (PCA10040), addr `CB:44:64:FB:AA:3A`, adv name `zenoh-nrf-l2cap` | J-Link console = `/dev/cu.usbmodem0010503477601` @115200 (**needs DTR asserted**) |
| Central / sender | nRF52840 **Dongle** (PCA10059) | runs the app; **invisible on USB** unless in DFU bootloader |
| **Sniffer (NEW)** | a **3rd** nRF52840 Dongle | passive; flashed with nRF Sniffer firmware |

Expected healthy link: `SINK rx: ~117 KB/s`, CV ~2%, every connection 2M/7.5 ms/DLE-251.
The DK+dongle are both busy being the two ends — the sniffer must be a **third** radio.

---

## 1. How a BLE sniffer works (why the procedure below is shaped this way)

BLE hops across **37 data channels**, changing channel **every connection event**.
A sniffer has one radio, so it must learn the hop pattern and jump in lockstep:

1. It watches the **3 advertising channels** (37/38/39) for the target.
2. When the dongle sends the **`CONNECT_IND`** (connection request), that packet
   carries **everything** needed to follow: access address, hop increment,
   channel map, interval, CSA. The sniffer computes the hop sequence from it.
3. It then retunes to the right channel at each event's anchor point and captures.
4. The link **starts on 1M PHY**, then runs `LL_PHY_UPDATE` → **2M**; the sniffer
   must switch its receiver to 2M (nRF52840 can; a BT-4 radio would go deaf).

**Consequence:** the sniffer must be **running and following the device *before*
the connection is (re)established.** It cannot grab an already-live connection —
it never saw the `CONNECT_IND`. → **Start sniffer, then power-cycle the dongle.**

---

## 2. One-time setup (nrfutil method — current)

The modern flow uses **nrfutil** (nRF Util), which bundles the sniffer *firmware*
and installs the Wireshark plugin for you — no manual hex download or extcap copy.
Refs: [Nordic Developer Academy — Setting up nRF Sniffer](https://academy.nordicsemi.com/courses/bluetooth-low-energy-fundamentals/lessons/lesson-6-bluetooth-le-sniffer/topic/nrf-sniffer-for-bluetooth-le/),
[DevZone #120208 — program sniffer fw with `nrfutil device`](https://devzone.nordicsemi.com/f/nordic-q-a/120208/programming-the-nrf-sniffer-firmware-to-the-nrf52840-dongle-using-the-nrfutil-device-command).

**Prereq:** install Wireshark (has the `btle` + `nordic_ble` dissectors).

```bash
# 1. install nRF Util (download the macOS binary from Nordic), then:
chmod +x nrfutil && sudo mv nrfutil /usr/local/bin/ && nrfutil --version
# 2. install the ble-sniffer + device commands (bundles firmware + extcap):
nrfutil install ble-sniffer device
# 3. put the SNIFFER dongle in DFU (press its side button -> red LED blinking).
#    NOTE: the CoC throughput dongle is invisible on USB, so only the sniffer
#    (in DFU) shows up here — but double-check the serial before flashing.
nrfutil device list
# 4. flash the bundled sniffer firmware (.zip DFU package):
find ~ -path '*nrfutil-ble-sniffer/firmware*' -name 'sniffer_nrf52840dongle*' 2>/dev/null
nrfutil device program --firmware <path-from-find> --serial-number <SN-from-list>
# 5. wire it into Wireshark (admin needed -> sudo on macOS):
sudo nrfutil ble-sniffer bootstrap
```

**Verify:** open/restart Wireshark → **"nRF Sniffer for Bluetooth LE"** appears as
a capture interface (else View → Interfaces, or Capture → Refresh Interfaces).

**Fallback (older method):** download the *nRF Sniffer for Bluetooth LE* package
and flash `sniffer_nrf52840dongle_nrf52840_*.hex` with **nRF Connect Programmer**
(the DFU flow), then still run `sudo nrfutil ble-sniffer bootstrap` for Wireshark.

---

## 3. Capture procedure (the important part — order matters)

1. **Power the rig** — DK advertising, dongle running (link may already be up).
2. **Wireshark → start capture on the nRF Sniffer interface.**
3. In the Wireshark **nRF Sniffer toolbar**, open the **device list** and
   **select `zenoh-nrf-l2cap` / `CB:44:64:FB:AA:3A`** to follow. (If it's not
   listed yet, the DK must be advertising — it advertises whenever not connected.)
4. **Now power-cycle the dongle** (unplug → replug, **no** RESET button — RESET
   re-enters the bootloader). This forces a fresh `CONNECT_IND` the sniffer can
   catch, so it locks onto the connection from the start.
5. Watch packets stream in. You should see: `ADV_IND` → `CONNECT_IND` →
   (1M data) → `LL_PHY_UPDATE_IND` → **2M** data → `LL_LENGTH_REQ/RSP` (DLE) →
   L2CAP CoC data PDUs → the DK's empty PDUs (ACKs).
6. Let it run ~30–60 s of steady SINK blasting, then **stop** and save the
   `.pcapng` (e.g. `sniffer-run.pcapng`).

**Gotchas (learned on this rig):**
- Start-then-power-cycle, as above — don't try to catch a live connection.
- Link is **unencrypted (L1)** → payloads are in the clear (no keys needed). 
- Expect **some packet loss** — one radio chasing 2M/7.5 ms won't catch 100%;
  you get solid statistics, not a perfect trace.
- Follows **one connection at a time**.

---

## 4. Analysis — counting packets per connection event

The nRF Sniffer adds a **`nordic_ble`** pseudo-header to every packet, including a
per-connection-event index: **`nordic_ble.event_counter`**. Packets sharing the
same `event_counter` are in the **same connection event**. That's the key.

**In Wireshark:**
- Add columns: `nordic_ble.event_counter`, `nordic_ble.channel`,
  `btle.data_header.more_data` (the **MD bit**), `btle.length`,
  `btle.control_opcode`, `nordic_ble.rssi`.
- Filter to just the data PDUs of our connection, e.g.
  `btle.data_header.llid` present and `frame.len > ...` (or filter by the
  connection's `btle.access_address` once you see it).
- **Count packets per event:** group/eyeball by `nordic_ble.event_counter` —
  count how many **non-empty** data PDUs (from the dongle, `btle.length > 0`)
  share each event index. That's packets/event.

**What to record (per event, over many events):**
| Observable | Field / how | Tells us |
|---|---|---|
| Data PDUs per event (dongle→DK) | count non-empty per `event_counter` | are we at ~4 vs the ~5 ceiling? |
| **MD bit** on the dongle's last data PDU of an event | `btle.data_header.more_data` | MD=0 with more data queued → **event closes early** (push FORCE_MD) |
| Empty PDUs / gaps mid-event | dongle sends length-0 PDUs before event ends | app **starved** the event (fix feed/credits) |
| Retransmissions | SN/NESN not advancing; repeated PDUs | RF interference wasting airtime |
| PHY / interval | `LL_PHY_UPDATE_IND`, `CONNECT_IND` interval | confirm 2M / 7.5 ms on-air |

**Decision rule:**
- Last dongle PDU each event has **MD=0** but throughput < ceiling and buffers
  were available → the controller is **closing the event early** → next
  experiment: raise `FORCE_MD_COUNT` (and/or sweep `BT_BUF_ACL_TX_COUNT`).
- Events contain **empty PDUs / gaps** from the dongle before closing → the app
  **can't refill fast enough** → raise blast-thread priority / pre-queue more
  SDUs / increase the DK's L2CAP RX credits (`rx_pool`).

---

## 4b. Job B — RTL8761/BlueZ on-air LL PDU size (DLE-on-air check)

Same sniffer, different connection: capture the **BlueZ/RTL8761 → DK** link (the
Linux TP-Link acting as central) and read the **on-air LL data PDU length**.

**Capture:**
1. Free the DK for BlueZ (unplug the nRF dongle so the DK re-advertises).
2. Sniffer capturing in Wireshark, **follow `zenoh-nrf-l2cap` / `CB:44:64:FB:AA:3A`**.
3. On the Linux box, start the connection so the sniffer catches the `CONNECT_IND`:
   `sudo python3 test-l2cap-echo.py CB:44:64:FB:AA:3A random` (the echo/stream phases
   generate sustained traffic).

**What to read:** on the **data** PDUs central→peripheral, the on-air payload size —
`btle.length` (the LL Data PDU length). The decisive question:
| On-air `btle.length` | Meaning |
|---|---|
| **~251** | RTL8761 reassembles the 27-byte HCI fragments and uses DLE on air → the ~4.5× gap vs the nRF dongle is **purely packets-per-event** |
| **~27** | RTL8761 **re-fragments to 27 on air** despite DLE=251 → tiny-packet overhead explains a big chunk of the gap; a real RTL8761/BlueZ inefficiency |

Also count **packets-per-event** here (same `nordic_ble.event_counter` method) — the
RTL8761/BlueZ is expected to pack far fewer per event than the nRF dongle (cf.
DevZone #7059, ~1 pkt/CE); this is the *other* half of the 4.5× gap. Feed the
finding into `README.md` §5.1 (resolves the "DLE on-air unconfirmed" caveat).

---

## Phase 2 jobs — safety attribution (see PHASE2-SAFETY-QUALIFICATION.md)

In Phase 2 the sniffer is a **diagnostic**, not the primary metric source. Same
capture method (follow the safety connection); read different fields.

**Job C — reliability attribution (metric M3).** Under interference / range, count
on-air **LL retransmissions**, **CRC errors** (`nordic_ble.crcok == false`), and
**missed connection events** (gaps in `nordic_ble.event_counter`). This separates
*link/RF* loss (retransmits, CRC fails, missed events) from *stack/app* loss
(delivered on-air but dropped above the LL). The endpoint sequence-gap count is
the authoritative *absolute* loss; the sniffer explains **why**.

**Job D — true link-loss instant (metric M4).** Timestamp the **last successful
on-air ACKed exchange** before a link goes dead — the endpoints can't see this
cleanly. Compare against the robot's watchdog / supervision-timeout detection
timestamp to get the real **loss → detection** latency (not just detection → stop).

**Job E — queuing under load (metrics M1, M5, M6).** With several streams running,
look within each `event_counter`: how many PDUs, and **whose** (by CID / handle) —
does the **stop command wait behind other streams inside an event**? Decompose M1
into app→air + on-air + air→app by timestamping the command PDU and its ACK.

**Limitations (state these in any Phase-2 report):**
- **The sniffer OVER-reports loss, not under.** It sits at a *different antenna*
  than the real receiver and drops packets itself, so packets it shows as "lost"
  or CRC-failed **may have been received fine** by the intended device (Nordic
  DevZone [#108440](https://devzone.nordicsemi.com/f/nordic-q-a/108440/ble-sniffer-detects-different-packets-in-wireshark-than-nrf-connect-ble-tool-log-file)).
  So **endpoint sequence-gaps are authoritative for absolute loss**; use the
  sniffer for **retransmit patterns and RF context**, treating its loss as *its*
  view. (It's passive — it never *triggers* retransmissions itself.)
- **Coded PHY (S2/S8) is harder to follow** than 2M — matters for the PHY study (M9).
- If the safety link is **encrypted**, the sniffer sees **LL headers / timing /
  retransmits in the clear** (enough for M3/M4 and reliability), but needs the
  **keys** to decode payloads and app sequence numbers.

---

## 5. After the capture

- Note the **measured packets/event** (mean over ≥50 events) and which failure
  mode dominates (early-close vs starve).
- Feed that into `README.md` §8: it selects experiment #1 (FORCE_MD/ACL-TX sweep)
  vs #3 (feed/credit tuning) as the path from ~117 toward ~135–150.
- Save `sniffer-run.pcapng` and add a short findings note to §6.1 / §8.

## 6. Tooling reminders (from the 2026-07-27 session)
- Build: `python3 -m platformio run` (`pio` not on PATH).
- DK console read needs **DTR asserted** — use pyserial, not bare `cat`
  (`scratchpad/readser.py <port> <secs> 115200`); DK streams `SINK rx:` every ~1 s.
- Dongle: **RESET = enter DFU bootloader** (red LED, for flashing);
  **power-cycle = run the app**. Same for the sniffer dongle.
- References: `README.md` §10 (nRF Sniffer, Nordic Developer Academy sniffer
  lesson, Wireshark BTLE dissector).
