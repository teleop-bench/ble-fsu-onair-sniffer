/*
 * PCA10040 / nRF52832 passive raw-RADIO timing observer.
 * Q0 build: RX on one BLE channel, hardware-timestamp every packet via the
 * pre-programmed PPI (ch26: RADIO ADDRESS -> TIMER0 CAPTURE[1]; ch27: RADIO
 * END -> TIMER0 CAPTURE[2]), copy a tiny fixed record in the END ISR, and
 * dump the ring over UART after a frozen capture window. No BT controller.
 *
 * gap_proxy(next) = next.address_ticks - prev.end_ticks  (within-PHY delta
 * cancels the fixed ADDRESS-after-preamble offset; see plan §7).
 */
#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/device.h>
#include <string.h>
#include <stdlib.h>
#include <hal/nrf_radio.h>
#include <hal/nrf_timer.h>
#include <hal/nrf_ppi.h>

/* ---- capture record (plan §6 schema) ---- */
/* Generator payload contract (Q1): the nRF54 synthetic generator places, in
 * the PDU payload immediately after the 2-byte header, a fixed marker the
 * observer copies as ground-truth transmit identity. rx_buf layout for an adv
 * PDU: [0]=S0/header, [1]=length, [2..]=payload.
 *   payload[0..1] = little-endian transmitted pair id (gen_pairid)
 *   payload[2]    = transmitter tag (gen_txtag: which of the two endpoints)
 * For ambient Q0 traffic these bytes are whatever the advertiser sent and are
 * NOT meaningful — they only become ground truth under the generator. */
#define GEN_PAIRID_OFF 2   /* rx_buf offset of pairid low byte */
#define GEN_TXTAG_OFF  4   /* rx_buf offset of tx tag */

/* per-record status bits */
#define ST_REVERSAL   0x01  /* end_ticks - address_ticks not a plausible airtime */
#define ST_STALE_ADDR 0x02  /* address_ticks == previous record's (CC[1] not refreshed) */

struct capture_record {
	uint32_t address_ticks;   /* TIMER0 CC[1] @ EVENTS_ADDRESS */
	uint32_t end_ticks;       /* TIMER0 CC[2] @ EVENTS_END */
	uint16_t observer_seq;    /* observer-local monotonic (NOT the tx seq) */
	uint16_t gen_pairid;      /* generator ground-truth pair id (Q1) */
	uint16_t pdu_len;         /* header length byte */
	uint8_t  gen_txtag;       /* generator transmitter tag (Q1) */
	uint8_t  hdr_s0;          /* raw header S0 byte (was mislabeled tx_tag) */
	int8_t   rssi;            /* -RSSISAMPLE dBm */
	uint8_t  crc_ok;
	uint8_t  status;          /* ST_* bits */
};
#define RING_N 2048U
static struct capture_record ring[RING_N];
static volatile uint32_t ring_head;   /* ISR writes */
static uint32_t ring_tail;             /* thread drains */
static volatile uint32_t drop_full;    /* ring FULL (ONLY ring-full, not total loss) */
static volatile uint32_t addr_irq;     /* EVENTS_ADDRESS interrupts */
static volatile uint32_t end_irq;      /* EVENTS_END interrupts (== ISR record entries) */
static volatile uint32_t n_reversal;   /* records with implausible end-addr airtime */
static volatile uint32_t n_stale_addr; /* records where CC[1] did not refresh */
static volatile uint32_t max_isr_ticks;
static volatile uint16_t seq;
static volatile uint32_t prev_addr_ticks;
static volatile uint8_t  have_prev;
static volatile uint8_t  capturing;
/* plausible ADDRESS->END airtime bounds in 16MHz ticks: shortest legal
 * (len=0 -> 5 bytes -> 40us @1M = 640 ticks, minus slack) .. longest
 * (len=255 -> 260 bytes -> 2080us @1M = 33280 ticks, plus slack). */
#ifndef AIRTIME_MIN_TICKS
#define AIRTIME_MIN_TICKS 500u
#endif
#define AIRTIME_MAX_TICKS 34000u

/* rx double buffer (we only read header bytes, not full payload) */
static uint8_t rx_buf[258] __aligned(4);

/* ---- BLE PHY / channel config (Q0: advertising ch37, 1M) ---- */
#define AA_ADV        0x8E89BED6u
#define CRCINIT_ADV   0x555555u
/* Q1: dedicated access address so the observer hears ONLY the synthetic
 * generator (ambient advertising is excluded). Set Q1_MODE=1 for a Q1 run;
 * Q0 (ambient integrity) keeps Q1_MODE=0 and is unaffected. */
#ifndef Q1_MODE
#define Q1_MODE 0
#endif
#define AA_Q1         0x71764129u
/* Q1 PHY: 1M (default) or 2M via -DPHY2M=1 (must match the generator). */
#ifndef Q1_PHY_2M
#define Q1_PHY_2M 0
#endif
/* Q2: tune to a REAL connection's AA/CRCInit on a DATA channel (whitening ON,
 * IV=channel). AA/CRCInit/channel come from the central's printed connection
 * params: -DQ2_MODE=1 -DQ2_AA=0x.. -DQ2_CRCINIT=0x.. -DQ2_CHAN=N [-DPHY2M=1] */
#ifndef Q2_MODE
#define Q2_MODE 0
#endif
#ifndef Q2_AA
#define Q2_AA         0x71764129u
#endif
#ifndef Q2_CRCINIT
#define Q2_CRCINIT    0x555555u
#endif
#ifndef Q2_CHAN
#define Q2_CHAN       0
#endif
#if Q2_MODE
#define AA_ACTIVE      Q2_AA
#define CRCINIT_ACTIVE Q2_CRCINIT
#define CHAN_ACTIVE    Q2_CHAN       /* data channel 0..36 */
#define WHITEN_ACTIVE  1             /* real data channel IS whitened */
#define PHY2M_ACTIVE   Q1_PHY_2M
#define CAP_MS         30000         /* 30s: dilutes fixed snapshot slop to <1% -> tight retention LB (ring 2048 >> ~1230 recs) */
#define MODE_NAME      "Q2"
#elif Q1_MODE
#define AA_ACTIVE      AA_Q1
#define CRCINIT_ACTIVE CRCINIT_ADV
#define CHAN_ACTIVE    37
#define WHITEN_ACTIVE  0             /* Q1 synthetic: whitening off both sides */
#define PHY2M_ACTIVE   Q1_PHY_2M
#define CAP_MS         10000
#define MODE_NAME      "Q1"
#else
#define AA_ACTIVE      AA_ADV
#define CRCINIT_ACTIVE CRCINIT_ADV
#define CHAN_ACTIVE    37
#define WHITEN_ACTIVE  1             /* advertising is whitened */
#define PHY2M_ACTIVE   0
#define CAP_MS         6000
#define MODE_NAME      "Q0"
#endif
/* BLE channel index -> RF frequency register (2400 + F MHz) + whitening IV */
static uint8_t ble_freq(uint8_t ch)      /* ch = data-channel 0..36 or 37/38/39 adv */
{
	/* adv: 37->2402(F=2), 38->2426(F=26), 39->2480(F=80) */
	if (ch == 37) return 2;
	if (ch == 38) return 26;
	if (ch == 39) return 80;
	/* data channels 0..36 map to 2404..2478 skipping adv freqs */
	static const uint8_t f[37] = {
		4,6,8,10,12,14,16,18,20,22,24,28,30,32,34,36,38,40,42,44,46,48,
		50,52,54,56,58,60,62,64,66,68,70,72,74,76,78 };
	return f[ch];
}

static void radio_configure(uint8_t mode_2m, uint32_t aa, uint32_t crcinit,
			    uint8_t ch, uint8_t whiten)
{
	NRF_RADIO->POWER = 1;
	NRF_RADIO->MODE = mode_2m ? RADIO_MODE_MODE_Ble_2Mbit : RADIO_MODE_MODE_Ble_1Mbit;
	/* fast ramp-up so END_START re-arms RX quickly (plan §6 budget) */
	NRF_RADIO->MODECNF0 = (RADIO_MODECNF0_RU_Fast << RADIO_MODECNF0_RU_Pos) |
			      (RADIO_MODECNF0_DTX_Center << RADIO_MODECNF0_DTX_Pos);
	/* S0=1 byte, LENGTH=8 bit, S1=0; preamble 8-bit(1M)/16-bit(2M) */
	NRF_RADIO->PCNF0 = (1UL << RADIO_PCNF0_S0LEN_Pos) |
			   (8UL << RADIO_PCNF0_LFLEN_Pos) |
			   (0UL << RADIO_PCNF0_S1LEN_Pos) |
			   ((mode_2m ? RADIO_PCNF0_PLEN_16bit : RADIO_PCNF0_PLEN_8bit)
			    << RADIO_PCNF0_PLEN_Pos);
	/* whitening: ON for real BLE (adv/data channels, IV=ch), OFF for the Q1
	 * synthetic link (removes any DATAWHITE poly/IV mismatch; airtime same). */
	NRF_RADIO->PCNF1 = ((whiten ? RADIO_PCNF1_WHITEEN_Enabled
				    : RADIO_PCNF1_WHITEEN_Disabled)
			    << RADIO_PCNF1_WHITEEN_Pos) |
			   (RADIO_PCNF1_ENDIAN_Little << RADIO_PCNF1_ENDIAN_Pos) |
			   (3UL << RADIO_PCNF1_BALEN_Pos) |
			   (0UL << RADIO_PCNF1_STATLEN_Pos) |
			   (255UL << RADIO_PCNF1_MAXLEN_Pos);
	NRF_RADIO->BASE0 = (aa << 8) & 0xFFFFFF00u;
	NRF_RADIO->PREFIX0 = (aa >> 24) & 0xFF;
	NRF_RADIO->TXADDRESS = 0;
	NRF_RADIO->RXADDRESSES = 1;   /* logical address 0 */
	NRF_RADIO->CRCCNF = (RADIO_CRCCNF_LEN_Three << RADIO_CRCCNF_LEN_Pos) |
			    (RADIO_CRCCNF_SKIPADDR_Skip << RADIO_CRCCNF_SKIPADDR_Pos);
	NRF_RADIO->CRCPOLY = 0x100065Bu;
	NRF_RADIO->CRCINIT = crcinit;
	NRF_RADIO->DATAWHITEIV = ch;
	NRF_RADIO->FREQUENCY = ble_freq(ch);
	NRF_RADIO->PACKETPTR = (uint32_t)rx_buf;
	NRF_RADIO->SHORTS = RADIO_SHORTS_READY_START_Msk |
			    RADIO_SHORTS_END_START_Msk |
			    RADIO_SHORTS_ADDRESS_RSSISTART_Msk;
	NRF_RADIO->EVENTS_END = 0;
	NRF_RADIO->EVENTS_ADDRESS = 0;
	/* ADDRESS int enables loss accounting (addr vs end counts); END int
	 * records. */
	NRF_RADIO->INTENSET = RADIO_INTENSET_END_Msk | RADIO_INTENSET_ADDRESS_Msk;
}

static void timer0_start(void)
{
	NRF_TIMER0->TASKS_STOP = 1;
	NRF_TIMER0->MODE = TIMER_MODE_MODE_Timer;
	NRF_TIMER0->BITMODE = TIMER_BITMODE_BITMODE_32Bit;
	NRF_TIMER0->PRESCALER = 0;    /* 16 MHz -> 62.5 ns/tick */
	NRF_TIMER0->TASKS_CLEAR = 1;
	NRF_TIMER0->TASKS_START = 1;
}

static void ppi_capture_enable(void)
{
	/* fixed pre-programmed channels (nRF52832 PS): 26 ADDRESS->CAPTURE[1],
	 * 27 END->CAPTURE[2]. Just enable them. */
	NRF_PPI->CHENSET = (1UL << 26) | (1UL << 27);
}

ISR_DIRECT_DECLARE(radio_isr)
{
	/* live ISR-span budget: capture the FREE-RUNNING timer into a spare CC
	 * (CC[3]) at entry and exit. CC[1]/CC[2] are PPI-frozen and cannot be
	 * used for this. */
	NRF_TIMER0->TASKS_CAPTURE[3] = 1;
	uint32_t t0 = NRF_TIMER0->CC[3];
	if (NRF_RADIO->EVENTS_ADDRESS) {
		NRF_RADIO->EVENTS_ADDRESS = 0;
		if (capturing) addr_irq++;
	}
	if (NRF_RADIO->EVENTS_END) {
		NRF_RADIO->EVENTS_END = 0;
		if (capturing) {
			end_irq++;
			uint32_t h = ring_head;
			if ((h - ring_tail) >= RING_N) {
				drop_full++;
			} else {
				struct capture_record *r = &ring[h & (RING_N - 1)];
				uint32_t a = NRF_TIMER0->CC[1];
				uint32_t e = NRF_TIMER0->CC[2];
				r->address_ticks = a;
				r->end_ticks = e;
				r->observer_seq = seq++;
				r->gen_pairid = (uint16_t)rx_buf[GEN_PAIRID_OFF] |
					((uint16_t)rx_buf[GEN_PAIRID_OFF + 1] << 8);
				r->gen_txtag = rx_buf[GEN_TXTAG_OFF];
				r->pdu_len = rx_buf[1];
				r->hdr_s0 = rx_buf[0];
				r->rssi = -(int8_t)(NRF_RADIO->RSSISAMPLE & 0x7F);
				r->crc_ok = (NRF_RADIO->CRCSTATUS &
					     RADIO_CRCSTATUS_CRCSTATUS_Msk) ? 1 : 0;
				uint8_t st = 0;
				uint32_t air = e - a;   /* mod 2^32 */
				if (air < AIRTIME_MIN_TICKS || air > AIRTIME_MAX_TICKS) {
					st |= ST_REVERSAL; n_reversal++;
				}
				if (have_prev && a == prev_addr_ticks) {
					st |= ST_STALE_ADDR; n_stale_addr++;
				}
				prev_addr_ticks = a; have_prev = 1;
				r->status = st;
				ring_head = h + 1;
			}
		}
		/* END_START shortcut already re-armed RX in hardware; ISR does
		 * NOT restart the radio. */
	}
	NRF_TIMER0->TASKS_CAPTURE[3] = 1;
	uint32_t dur = NRF_TIMER0->CC[3] - t0;   /* live ISR span in ticks */
	if (dur < 0x40000000u && dur > max_isr_ticks) max_isr_ticks = dur;
	return 1;  /* no scheduling */
}

static void print_ficr(void)
{
	printk("FICR PART=0x%08x VARIANT=0x%08x\n",
	       (unsigned)NRF_FICR->INFO.PART, (unsigned)NRF_FICR->INFO.VARIANT);
	printk("timer=16MHz(62.5ns) ring=%u\n", RING_N);
}

int main(void)
{
	printk("\n== PCA10040 raw-RADIO observer (Q0) ==\n");
	print_ficr();

	/* RADIO requires HFCLK from the external 32 MHz crystal (HFXO). On boot
	 * HFCLK runs off the internal RC, which the radio will not use. */
	NRF_CLOCK->EVENTS_HFCLKSTARTED = 0;
	NRF_CLOCK->TASKS_HFCLKSTART = 1;
	while (!NRF_CLOCK->EVENTS_HFCLKSTARTED) { }
	printk("HFXO started\n");

	IRQ_DIRECT_CONNECT(RADIO_IRQn, 0, radio_isr, 0);
	irq_enable(RADIO_IRQn);

	timer0_start();
	ppi_capture_enable();
	/* Q0: ambient adv ch37 1M. Q1: synthetic AA ch37, whitening off. Q2:
	 * real connection AA/CRCInit on a data channel, whitening on. */
	uint32_t rt_aa = AA_ACTIVE, rt_crc = CRCINIT_ACTIVE;
	uint8_t rt_ch = CHAN_ACTIVE, rt_phy2m = PHY2M_ACTIVE, rt_whiten = WHITEN_ACTIVE;
#if Q2_MODE
	/* RUNTIME config over UART: flash the observer ONCE, tune per connection
	 * (no rebuild, no stale AA). Read one line:
	 *   CFG aa=0x.. crc=0x.. ch=N phy=1|2   -> echo CONFIG-ECHO. */
	{
		const struct device *ucon = DEVICE_DT_GET(DT_CHOSEN(zephyr_console));
		char line[96]; unsigned char c; char *p = NULL;
		printk("CONFIG-READY Q2\n");
		/* read lines until a VALID CFG line arrives; ignore stray bytes /
		 * partial lines from the DTR reset + USB enumeration. */
		for (;;) {
			int nn = 0;
			for (;;) {
				if (uart_poll_in(ucon, &c) != 0) { k_msleep(2); continue; }
				if (c == '\n' || c == '\r') break;
				if (nn < (int)sizeof(line) - 1) line[nn++] = c;
			}
			line[nn] = 0;
			if (strstr(line, "aa=0x")) break;   /* a real CFG line */
		}
		if ((p = strstr(line, "aa=0x")))  rt_aa = strtoul(p + 5, NULL, 16);
		if ((p = strstr(line, "crc=0x"))) rt_crc = strtoul(p + 6, NULL, 16);
		if ((p = strstr(line, "ch=")))    rt_ch = strtoul(p + 3, NULL, 10);
		if ((p = strstr(line, "phy=")))   rt_phy2m = (strtoul(p + 4, NULL, 10) == 2);
		rt_whiten = 1;   /* real data channel is whitened */
		printk("CONFIG-ECHO aa=0x%08x crc=0x%06x ch=%u phy=%uM\n",
		       (unsigned)rt_aa, (unsigned)rt_crc, rt_ch, rt_phy2m ? 2 : 1);
	}
#endif
	printk("mode=%s phy=%uM AA=0x%08x crcinit=0x%06x ch=%u whiten=%u cap=%ums\n",
	       MODE_NAME, rt_phy2m ? 2 : 1, (unsigned)rt_aa,
	       (unsigned)rt_crc, rt_ch, rt_whiten, CAP_MS);
	radio_configure(rt_phy2m, rt_aa, rt_crc, rt_ch, rt_whiten);

	seq = 0; ring_head = ring_tail = 0; drop_full = 0; max_isr_ticks = 0;
	addr_irq = end_irq = n_reversal = n_stale_addr = 0;
	prev_addr_ticks = 0; have_prev = 0;
	printk("ARMED %s ch%u\n", MODE_NAME, rt_ch);   /* config done, pre-RXEN */
#if Q2_MODE
	/* Q2 handshake: wait for the runner's GO so it can take the START
	 * snapshots of BOTH endpoint counters FIRST -> the endpoint interval is
	 * guaranteed to CONTAIN the observer interval (command ordering, not
	 * arrival-time heuristics). */
	{
		const struct device *ucon = DEVICE_DT_GET(DT_CHOSEN(zephyr_console));
		unsigned char c = 0;
		while (c != 'G') {
			if (uart_poll_in(ucon, &c) != 0) { c = 0; k_msleep(2); }
		}
	}
#endif
	capturing = 1;
	NRF_RADIO->TASKS_RXEN = 1;
	/* TIMING ANCHOR: hardware-capture TIMER0 (the SAME acquisition timebase as the
	 * REC addr/end ticks) into spare CC[0] at the capture edges. With the host-clock
	 * HOSTMS the reader stamps on these two lines, (HOSTMS, tick) at START and END
	 * give a 2-point map from host time -> observer timer domain, so a completion
	 * HOSTMS becomes an observer-record boundary WITHOUT first finding the step. */
	NRF_TIMER0->TASKS_CAPTURE[0] = 1;
	printk("CAPTURE-START %s ch%u cap=%ums tick=%u\n",
	       MODE_NAME, rt_ch, CAP_MS, (unsigned)NRF_TIMER0->CC[0]);
	k_msleep(CAP_MS);

	/* freeze -- CAPTURE-END is the immediate capture edge (before the settle
	 * + FROZEN dump), so the runner takes END snapshots against the true end. */
	capturing = 0;
	NRF_TIMER0->TASKS_CAPTURE[0] = 1;
	printk("CAPTURE-END %s tick=%u\n", MODE_NAME, (unsigned)NRF_TIMER0->CC[0]);
	NRF_RADIO->TASKS_DISABLE = 1;
	k_msleep(5);

	uint32_t n = ring_head;
	printk("FROZEN: records=%u ring_full_drops=%u max_isr_ticks=%u (%u ns)\n",
	       n, drop_full, max_isr_ticks, max_isr_ticks * 625 / 10);
	/* loss accounting: addr_irq should ~equal end_irq (every matched ADDRESS
	 * that completes gives one END); end_irq should equal records+drops. */
	printk("LOSS: addr_irq=%u end_irq=%u records=%u ring_full_drops=%u "
	       "addr_minus_end=%d end_minus_recorded=%d reversal=%u stale_addr=%u\n",
	       addr_irq, end_irq, n, drop_full,
	       (int)(addr_irq - end_irq), (int)(end_irq - (n + drop_full)),
	       n_reversal, n_stale_addr);
	uint32_t crc_ok = 0;
	for (uint32_t i = 0; i < n && i < RING_N; i++) {
		if (ring[i].crc_ok) crc_ok++;
	}
	printk("crc_ok=%u/%u (qualitative: ambient traffic, NOT a coherence gate)\n",
	       crc_ok, n);
	/* Q0: preview 40 records. Q1/Q2: dump ALL records so the offline analyzer
	 * can reconstruct every pair/exchange. */
	uint32_t lim = (Q1_MODE || Q2_MODE) ? (n < RING_N ? n : RING_N)
					    : (n < 40 ? n : 40);
	for (uint32_t i = 0; i < lim; i++) {
		struct capture_record *r = &ring[i];
		int32_t gap = (i > 0) ? (int32_t)(r->address_ticks - ring[i-1].end_ticks) : 0;
		printk("REC oseq=%u s0=0x%02x len=%u crc=%u rssi=%d pairid=%u txtag=%u "
		       "st=0x%02x addr=%u end=%u air_us=%d gap_us=%d\n",
		       r->observer_seq, r->hdr_s0, r->pdu_len, r->crc_ok, r->rssi,
		       r->gen_pairid, r->gen_txtag, r->status,
		       r->address_ticks, r->end_ticks,
		       (int)(r->end_ticks - r->address_ticks) * 10 / 160,
		       gap * 10 / 160);
	}
	printk("%s-DONE\n", MODE_NAME);
	return 0;
}
