/*
 * nRF54L15 raw-RADIO synthetic pair generator (Q1 go/no-go source).
 *
 * Emits BLE-format packet PAIRS on ch37 (1M) at hardware-scheduled END->START
 * gaps G in {150,100,70,52} us, so the nRF52832 observer can be qualified for
 * close-pair retention/discrimination. The A->B gap is timed entirely in
 * hardware (TIMER10 compare -> RADIO TXEN via DPPI); TX ramp-up cancels in
 * CC_B - CC_A = airtime_A + G. Each pair is self-timestamped (TX ADDRESS of B
 * minus TX END of A) as an independent ground truth vs the observer.
 *
 * nRF54L specifics (vs nRF52): CLOCK uses XOSTART; RADIO fast ramp-up is the
 * TIMING register default (no MODECNF0); RADIO is in the DPPIC10 domain, so we
 * pair it with NRF_TIMER10 (8 CC). Whitening DISABLED (matches observer Q1).
 * TIMER10 base PCLK = 32 MHz; prescaler=1 -> 16 MHz == observer TIMER0 units.
 */
#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>
#include <hal/nrf_radio.h>
#include <hal/nrf_timer.h>

/* ---- link params (MUST match observer Q1) ---- */
#define AA_Q1       0x71764129u
#define GEN_CRCINIT 0x555555u
#define GEN_CRCPOLY 0x100065Bu
#define GEN_FREQ    2            /* ch37 = 2400 + 2 MHz */
#define GEN_DPPI_EN (1u << 31)   /* publish/subscribe enable bit */

/* ---- pair shape (asymmetric lengths distinguish the two TX) ---- */
#define LEN_A       20
#define LEN_B       30
#define TXTAG_A     1
#define TXTAG_B     2

/* ---- spacing ladder (END->START gap G, us) + pairs per rung ---- */
static const uint16_t RUNGS[] = { 150, 100, 70, 52 };
#define PAIRS_PER_RUNG  200
#define INTERPAIR_MS    4

/* PHY: 1M (default) or 2M via -DGEN_PHY_2M=1. TIMER10 is 16 MHz (16 ticks/us).
 *   1M: 8 us/byte = 128 ticks/byte, preamble 1 byte  -> non-payload = 10 bytes
 *   2M: 4 us/byte =  64 ticks/byte, preamble 2 bytes  -> non-payload = 11 bytes
 * airtime = (preamble + AA4 + S0 1 + LEN 1 + payload + CRC3) bytes. */
#ifndef GEN_PHY_2M
#define GEN_PHY_2M 0
#endif
#define TICKS_PER_US    16u
#if GEN_PHY_2M
#define TICKS_PER_BYTE  64u
#define NONPAYLOAD_B    11u
#define RADIO_BLE_MODE  RADIO_MODE_MODE_Ble_2Mbit
#define RADIO_PLEN      RADIO_PCNF0_PLEN_16bit
#else
#define TICKS_PER_BYTE  128u
#define NONPAYLOAD_B    10u
#define RADIO_BLE_MODE  RADIO_MODE_MODE_Ble_1Mbit
#define RADIO_PLEN      RADIO_PCNF0_PLEN_8bit
#endif
#define AIRTIME_TICKS(len)  ((NONPAYLOAD_B + (len)) * TICKS_PER_BYTE)

static uint8_t bufA[64] __aligned(4);
static uint8_t bufB[64] __aligned(4);

/* TIMER10 CC map */
#define CC_TXA   0   /* compare -> TXEN (packet A) */
#define CC_TXB   1   /* compare -> TXEN (packet B) */
#define CC_ADDR  2   /* capture <- RADIO ADDRESS (A then B) */
#define CC_END   3   /* capture <- RADIO END (A then B) */
/* DPPIC10 channels */
#define CH_TX    0   /* CC_TXA/CC_TXB compare -> RADIO TXEN */
#define CH_ADDR  1   /* RADIO ADDRESS -> capture CC_ADDR */
#define CH_END   2   /* RADIO END -> capture CC_END */

static volatile uint8_t  phase;      /* 0: expect A END, 1: expect B END */
static volatile uint32_t t_Aend;     /* saved CC_END at A's END */
static volatile uint32_t gap_gen;    /* B.address - A.end (16MHz ticks) */
static volatile uint8_t  pair_done;

static void clock_hfxo_start(void)
{
	/* nRF54L: HFXO started via CLOCK TASKS_XOSTART (agent-confirmed). Cap
	 * trim is done by Zephyr SoC init. Poll bounded, then settle. */
	NRF_CLOCK->EVENTS_XOSTARTED = 0;
	NRF_CLOCK->TASKS_XOSTART = 1;
	for (volatile int i = 0; i < 200000 && !NRF_CLOCK->EVENTS_XOSTARTED; i++) { }
	k_msleep(3);   /* >= 1.65 ms startup-time-us, belt-and-suspenders */
}

static void build_pkt(uint8_t *buf, uint8_t len, uint16_t pairid, uint8_t txtag)
{
	buf[0] = 0x02;                 /* S0 (arbitrary PDU marker) */
	buf[1] = len;                  /* LENGTH field */
	buf[2] = (uint8_t)(pairid & 0xFF);
	buf[3] = (uint8_t)(pairid >> 8);
	buf[4] = txtag;
	for (int i = 5; i < 2 + len; i++) buf[i] = (uint8_t)i;
}

static void radio_configure(void)
{
	NRF_RADIO->MODE = RADIO_BLE_MODE;
	/* fast ramp-up is the TIMING-register reset default on nRF54L */
	NRF_RADIO->PCNF0 = (1UL << RADIO_PCNF0_S0LEN_Pos) |
			   (8UL << RADIO_PCNF0_LFLEN_Pos) |
			   (0UL << RADIO_PCNF0_S1LEN_Pos) |
			   (RADIO_PLEN << RADIO_PCNF0_PLEN_Pos);
	NRF_RADIO->PCNF1 = (RADIO_PCNF1_WHITEEN_Disabled << RADIO_PCNF1_WHITEEN_Pos) |
			   (RADIO_PCNF1_ENDIAN_Little << RADIO_PCNF1_ENDIAN_Pos) |
			   (3UL << RADIO_PCNF1_BALEN_Pos) |
			   (0UL << RADIO_PCNF1_STATLEN_Pos) |
			   (63UL << RADIO_PCNF1_MAXLEN_Pos);
	NRF_RADIO->BASE0 = (AA_Q1 << 8) & 0xFFFFFF00u;
	NRF_RADIO->PREFIX0 = (AA_Q1 >> 24) & 0xFF;
	NRF_RADIO->TXADDRESS = 0;
	NRF_RADIO->CRCCNF = (RADIO_CRCCNF_LEN_Three << RADIO_CRCCNF_LEN_Pos) |
			    (RADIO_CRCCNF_SKIPADDR_Skip << RADIO_CRCCNF_SKIPADDR_Pos);
	NRF_RADIO->CRCPOLY = GEN_CRCPOLY;
	NRF_RADIO->CRCINIT = GEN_CRCINIT;
	NRF_RADIO->FREQUENCY = GEN_FREQ;
	/* PHYEND is the true last-bit-on-air event (END = EasyDMA/mem-access
	 * done, which fires ~17 ticks later on nRF54L -- measured). Reference
	 * EVERYTHING to PHYEND, and pin the configurable TX PHYEND delay to 0. */
	NRF_RADIO->PHYENDTXDELAY = 0;
	/* auto-start TX after ramp; disable after each packet so A and B both
	 * ramp identically from DISABLED (ramp-up then cancels in CC_B-CC_A). */
	NRF_RADIO->SHORTS = (1UL << RADIO_SHORTS_READY_START_Pos) |
			    (1UL << RADIO_SHORTS_PHYEND_DISABLE_Pos);
	NRF_RADIO->EVENTS_PHYEND = 0;
	NRF_RADIO->EVENTS_ADDRESS = 0;
	nrf_radio_int_enable(NRF_RADIO, NRF_RADIO_INT_PHYEND_MASK);

	/* DPPI: RADIO PHYEND/ADDRESS publish; RADIO TXEN subscribe */
	NRF_RADIO->PUBLISH_PHYEND = CH_END | GEN_DPPI_EN;   /* CC_END <- PHYEND */
	NRF_RADIO->PUBLISH_ADDRESS = CH_ADDR | GEN_DPPI_EN;
	NRF_RADIO->SUBSCRIBE_TXEN = CH_TX | GEN_DPPI_EN;
}

static void timer10_configure(void)
{
	NRF_TIMER10->TASKS_STOP = 1;
	NRF_TIMER10->MODE = TIMER_MODE_MODE_Timer;
	NRF_TIMER10->BITMODE = TIMER_BITMODE_BITMODE_32Bit;
	NRF_TIMER10->PRESCALER = 1;   /* 32 MHz PCLK / 2 = 16 MHz == observer */
	/* captures subscribe to RADIO ADDRESS/END; compares publish to TXEN */
	NRF_TIMER10->SUBSCRIBE_CAPTURE[CC_ADDR] = CH_ADDR | GEN_DPPI_EN;
	NRF_TIMER10->SUBSCRIBE_CAPTURE[CC_END]  = CH_END | GEN_DPPI_EN;
	NRF_TIMER10->PUBLISH_COMPARE[CC_TXA] = CH_TX | GEN_DPPI_EN;
	NRF_TIMER10->PUBLISH_COMPARE[CC_TXB] = CH_TX | GEN_DPPI_EN;
}

static void dppi_enable(void)
{
	NRF_DPPIC10->CHENSET = (1u << CH_TX) | (1u << CH_ADDR) | (1u << CH_END);
}

ISR_DIRECT_DECLARE(radio_isr)
{
	if (NRF_RADIO->EVENTS_PHYEND) {
		NRF_RADIO->EVENTS_PHYEND = 0;
		if (phase == 0) {
			/* A's last bit on air: CC_END holds A's PHYEND (B not yet run) */
			t_Aend = NRF_TIMER10->CC[CC_END];
			NRF_RADIO->PACKETPTR = (uint32_t)bufB;  /* B DMA'd at its START */
			phase = 1;
		} else {
			/* B done: CC_ADDR holds B's ADDRESS (captured before B PHYEND) */
			uint32_t b_addr = NRF_TIMER10->CC[CC_ADDR];
			gap_gen = b_addr - t_Aend;   /* B.address - A.last-bit, mod 2^32 */
			NRF_RADIO->PACKETPTR = (uint32_t)bufA;
			pair_done = 1;
		}
	}
	return 1;
}

static int emit_pair(uint16_t pairid, uint16_t g_us)
{
	build_pkt(bufA, LEN_A, pairid, TXTAG_A);
	build_pkt(bufB, LEN_B, pairid, TXTAG_B);
	NRF_RADIO->PACKETPTR = (uint32_t)bufA;
	phase = 0; pair_done = 0;

	uint32_t cc_a = 32;   /* small start offset */
	uint32_t cc_b = cc_a + AIRTIME_TICKS(LEN_A) + (uint32_t)g_us * TICKS_PER_US;
	NRF_TIMER10->TASKS_STOP = 1;
	NRF_TIMER10->TASKS_CLEAR = 1;
	NRF_TIMER10->CC[CC_TXA] = cc_a;
	NRF_TIMER10->CC[CC_TXB] = cc_b;
	NRF_TIMER10->EVENTS_COMPARE[CC_TXA] = 0;
	NRF_TIMER10->EVENTS_COMPARE[CC_TXB] = 0;
	NRF_TIMER10->TASKS_START = 1;

	/* wait for B's PHYEND ISR (bounded) */
	for (int i = 0; i < 100000 && !pair_done; i++) { k_busy_wait(1); }
	NRF_TIMER10->TASKS_STOP = 1;
	return pair_done ? 0 : -1;   /* -1 = missed deadline (ISR never completed) */
}

int main(void)
{
	printk("\n== Q1 generator (nRF54L15) ==\n");
	printk("FICR INFO.PART=0x%08x DEVICEID=%08x%08x AA=0x%08x ch37 phy=%sM whiten=off\n",
	       (unsigned)NRF_FICR->INFO.PART,
	       (unsigned)NRF_FICR->INFO.DEVICEID[1], (unsigned)NRF_FICR->INFO.DEVICEID[0],
	       AA_Q1, GEN_PHY_2M ? "2" : "1");

	clock_hfxo_start();

	IRQ_DIRECT_CONNECT(RADIO_0_IRQn, 0, radio_isr, 0);
	irq_enable(RADIO_0_IRQn);

	radio_configure();
	timer10_configure();
	dppi_enable();

	/* warm-up: let the observer settle, and prove one pair before the run */
	k_msleep(1500);

	/* INTERLEAVED (round-robin) rung order removes any time-order/drift
	 * confound -- adjacent pairs use different gaps, so a rung is never a
	 * single contiguous time block (the reviewer's "randomize order"). The
	 * 150 us rung doubles as the unreduced-tIFS control.
	 * Results are BUFFERED and printed only after all pairs are emitted, so
	 * per-pair UART blocking does not slow emission (keeps the whole run
	 * comfortably inside the observer's capture window). */
	static uint16_t g_gap[PAIRS_PER_RUNG * 4];
	uint16_t pairid = 0;
	uint32_t total = 0, missed = 0;
	for (unsigned k = 0; k < PAIRS_PER_RUNG; k++) {
		for (unsigned r = 0; r < ARRAY_SIZE(RUNGS); r++) {
			if (emit_pair(pairid, RUNGS[r]) != 0) missed++;
			g_gap[pairid] = (uint16_t)gap_gen;
			pairid++; total++;
			k_msleep(INTERPAIR_MS);
		}
	}
	for (unsigned p = 0; p < total; p++) {
		printk("GEN pair=%u rung_us=%u lenA=%u lenB=%u gap_gen_ticks=%u\n",
		       p, RUNGS[p % ARRAY_SIZE(RUNGS)], LEN_A, LEN_B, g_gap[p]);
	}
	printk("GEN-DONE pairs=%u missed_deadline=%u\n",
	       (unsigned)total, (unsigned)missed);
	return 0;
}
