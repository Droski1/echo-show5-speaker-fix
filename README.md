# Echo Show 5 (cronos) — Speaker Silence: Root Cause & Kernel Fix

**The MAX98396 speaker amp was being hardware-reset on every playback. The fix is 4 kernel patches + 1 server fix. YouTube plays fully now.**

This repo documents the complete investigation and fix for the Echo Show 5 (2nd Gen, codename `cronos`) running LineageOS 18.1, where **all speaker audio was silent** except the boot-time sounds — for no apparent reason. The device's boot sounds worked, but every app tone, alarm, YouTube video, and Bluetooth stream was dead silent.

## TL;DR

**Pin 35 (KPROW2) is shared between two consumers:**
1. The **MAX98396** speaker amplifier's hardware reset (`maxim,reset-gpio = <&pio 35 GPIO_ACTIVE_HIGH>`)
2. The **MTK codec's** external-amp control (`aud_pins_extamp_high/low` pinctrl states)

The MTK codec's `Ext_Speaker_Amp_Change(true)` pulses pin 35 **LOW→HIGH** on every speaker-path enable:

```c
AudDrv_GPIO_EXTAMP_Select(false);   // pin 35 LOW  → MAX98396 RESET!
udelay(1000);
AudDrv_GPIO_EXTAMP_Select(true);    // pin 35 HIGH
msleep(25);                          // "warm-up"
```

That LOW pulse **hardware-resets the amp mid-session**: all its registers clear to power-on defaults (NOVBAT=0, SPK_EN=0, GLOBAL_EN=0) and it refuses to re-enable — so **only the very first sound after each reboot was audible**.

**The fix:** make `AudDrv_GPIO_EXTAMP_Select()` a no-op (kernel commit `6e7d3e33`) so the amp's own driver exclusively owns the pin. Combined with three earlier patches (fresh reset at stream init, the datasheet power-up sequence, and keeping GLOBAL_EN active), the amp now powers up correctly and stays on.

## The 4 kernel commits

| Commit | File | What it fixes |
|---|---|---|
| `72b0243e` | `sound/soc/codecs/max98396.c` | Fresh hardware reset at stream init + verified GLOBAL_EN writes (the amp was ACK'ing I2C writes but ignoring them) |
| `61422f85` | `sound/soc/codecs/max98396.c` | Full datasheet power-up sequence: **NOVBAT=1 → SPK_EN=1 → GLOBAL_EN=1 LAST** (the board has NO battery; without NOVBAT the phantom VBAT UVLO blocks everything) |
| `aa42ad1d` | `sound/soc/codecs/max98396.c` | Keep GLOBAL_EN active across playbacks (the amp refuses to re-enable after a power-down) |
| `6e7d3e33` | `sound/soc/mediatek/mt_soc_audio_8163_amzn/AudDrv_Gpio.c` | **THE FIX**: no-op `EXTAMP_Select` — pin 35 no longer pulses the amp's reset |

Apply all four: [`patches/show5-speaker-fix-4-commits.patch`](patches/show5-speaker-fix-4-commits.patch) (kernel base: `62877797`, Amazon MT8163 lineage-18.1 tree).

## Also fixed along the way

- **Server bug:** the `/show5/test/play` endpoint double-wrapped the request JSON into the b64 field — every "verified" test tone was actually garbage. The app either threw `bad base-64` or played JSON bytes as PCM.
- **A diagnostic landmine:** `dumpsys media.audio_flinger` / `dumpsys audio` **SEGFAULT the vendor audio HAL** (`Device::debug()`) and crash-loop the whole audio stack on this device.

## Docs

- [01 — Root cause analysis](docs/01-root-cause.md) — the full 5-layer investigation
- [02 — Kernel fixes](docs/02-kernel-fixes.md) — the patches in detail, with the datasheet power-up sequence
- [03 — Build & flash workflow](docs/03-build-flash.md) — the proven kernel build/repack/flash pipeline
- [04 — Landmines & gotchas](docs/04-landmines.md) — things that will waste your time
- [05 — Timeline](docs/05-timeline.md) — the session log

## Verification (2026-09-01)

- ✅ Startup/boot sounds play after every reboot
- ✅ **YouTube plays fully** — user-confirmed: *"YOUTUBE PLAYS FULLY. HOLY FUCK IT FINALLY WORKS."*
- ✅ dmesg shows the power-up sequence landing (`NOVBAT readback=1` + `GLOBAL_EN readback=1`) and no further reset pulses

## Hardware / software context

- Device: Amazon Echo Show 5 Gen 2 (2021), codename `cronos`, C76N8S
- ROM: LineageOS 18.1 (Android 11), unofficial build
- Kernel: `4.9.337-g6e7d3e33` (Amazon MT8163 fork, `lineage-18.1` branch)
- Amp: **MAX98396** 20V digital-input Class-DG amplifier with I/V sense (I2C addr `2-003d`, reset on GPIO 35/KPROW2)
- Codec: MTK MT8163 internal codec (`mt_soc_codec_63xx`)
- Datasheet: MAX98396 (ADI/Maxim) — power-up sequence Table 1, write-access restrictions Table 11
