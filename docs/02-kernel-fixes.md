# 02 — Kernel Fixes

All four commits are against the Amazon MT8163 kernel fork, branch `lineage-18.1`.
Apply as a single patch: `patches/show5-speaker-fix-4-commits.patch` (base `62877797`).

## Commit `72b0243e` — verified GLOBAL_EN writes + fresh amp reset at stream init

The amp was observed to ACK every I2C write while keeping registers at power-on defaults
(GLOBAL_EN stuck at 0 = output stage disabled = total silence). This patch adds:

- A **fresh hardware reset cycle** at `max98396_init_setup` (GPIO low 50 ms → high 20 ms) so
  the amp starts every stream from a clean state.
- A **verified GLOBAL_EN write** with read-back + dmesg logging, plus a fallback to the
  write-only alternate register `0x2110` if `0x210F` doesn't stick.

## Commit `61422f85` — full datasheet power-up sequence (NOVBAT → SPK_EN → GLOBAL_EN)

Implements the MAX98396 datasheet's Table 1 power-up order inside the DAC event:

1. `NOVBAT=1` (0x20A0) — no-battery board config (the phantom VBAT UVLO blocks everything)
2. `SPK_EN=1` (0x20AF) — speaker output enable
3. `GLOBAL_EN=1` (0x210F) — master enable, LAST, with read-back verification + up to 3
   retries (5–10 ms apart), each logged to dmesg

## Commit `aa42ad1d` — keep GLOBAL_EN active across playbacks

The POST_PMD handler wrote `GLOBAL_EN=0` on every stream close; afterwards the amp refused
to re-enter the active state (EN write ignored). The power-down write is removed — the amp
stays active and the idle draw is negligible.

## Commit `6e7d3e33` — no-op `EXTAMP_Select` (the actual root-cause fix)

Pin 35 (KPROW2) is shared between the MAX98396's reset and the MTK codec's external-amp
control. `Ext_Speaker_Amp_Change()` pulsed it LOW on every speaker-path enable, hardware-
resetting the amp mid-session. `AudDrv_GPIO_EXTAMP_Select()` now returns immediately
(`(void)bEnable; return 0;`) — pin 35 belongs to the amp driver alone.

```c
int AudDrv_GPIO_EXTAMP_Select(int bEnable)
{
	/* 2026-09-01: NO-OP. Pin 35 (KPROW2) is BOTH the MTK codec's extamp
	 * control AND the MAX98396 speaker amp's reset (maxim,reset-gpio).
	 * The codec pulses it low on every speaker-path enable, hardware-
	 * resetting the amp mid-session (registers clear to defaults, the amp
	 * refuses to re-enable -> total silence after the first sound). The
	 * amp's own driver owns the pin now. */
	(void)bEnable;
	return 0;
#if 0
	/* original body retained as dead code for reference */
	...
#endif
}
```

## The MAX98396 datasheet cheat-sheet

| Register | Addr | Purpose | Write restriction |
|---|---|---|---|
| Software Reset | 0x2000 | SW reset (enters software shutdown) | write-only |
| Amp Supply Control | 0x20A0 | **NOVBAT** = no-battery mode | dynamic |
| AMP enables | 0x20AF | **SPK_EN** / SPK_FB_EN | ENL (locked unless EN=0) |
| PCM RX enable | 0x205E | PCM_RX_EN | ENL |
| Global Enable | 0x210F | **EN** — master enable, set LAST | EN (writable in software shutdown only) |

Key datasheet facts:
- "In the software shutdown state, all device registers can be programmed without restriction."
- "Write access to some critical global enable restricted bit fields (ENL) is locked out by
  the hardware when the device is not in the software-shutdown state. Attempting to change
  these has no effect (read access is still allowed)."
- "The device cannot transition from the software shutdown state to the active state until
  PVDD and VBAT are all above their UVLO thresholds." — hence NOVBAT first on a battery-less board.
- Datasheet: [farnell.com/datasheets/3204109.pdf](https://www.farnell.com/datasheets/3204109.pdf)

## Server fix (non-kernel)

`~/show5/show5-server.py` — `POST /show5/test/play` now extracts `b64` from the JSON body
instead of double-wrapping the whole body into the b64 field.
