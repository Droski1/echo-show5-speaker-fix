# 01 — Root Cause Analysis

**Symptom:** total speaker silence on ALL audio (app tones, alarms, YouTube, Bluetooth streaming) — except the boot-time startup/unlock sounds which played fine. The speaker hardware was never dead; the amp was being killed by its own support software.

This investigation took multiple layers to peel. All five were real, and all five needed fixing. The last one was the actual killer.

---

## Layer 1 — The test tones were fake (server bug)

The `POST /show5/test/play` endpoint on the voice-assistant server built its websocket payload like this:

```python
w.write(ws_frame(1, json.dumps({"t": "audio", "b64": body.decode()}).encode()))
```

`body` was the **entire raw request body** — i.e. the whole JSON `{"t":"audio","b64":"..."}` got wrapped *again* into a new `b64` field. The app then either threw `bad base-64`, or worse, played the JSON bytes as if they were PCM. AudioFlinger showed a "playback track" — so every "verified" tone looked successful while being pure garbage. **The user had never heard a real test tone.**

**Fix:** parse the JSON body and extract `b64` (also accept raw base64 bodies).

**Lesson:** a visible playback track in AudioFlinger does NOT mean audible audio.

## Layer 2 — The amp ACK'd I2C writes but ignored them

The MAX98396 (I2C `2-003d`) read back correctly, but every register write was ACK'd and dropped. The regmap **cache** recorded the writes, but the **hardware** stayed at power-on defaults. The debugfs `registers` file shows the *cache*, which lied to us for hours.

- `GLOBAL_EN` (0x210F) = 0 — the master enable never set
- `SPK_EN` (0x20AF) = 0 — the speaker output stage never enabled
- `NOVBAT` (0x20A0) = 0 — the amp thought a battery existed (this board has none)

**Fix (commit `72b0243e`):** a fresh hardware reset cycle (GPIO low 50 ms → high 20 ms) at `max98396_init_setup`. After that, writes landed in hardware (verified by flipping `cache_bypass` to 1 and re-reading).

**Lesson:** always verify hardware state with `/sys/kernel/debug/regmap/2-003d/cache_bypass`; the regmap cache is not the truth.

## Layer 3 — The datasheet power-up sequence (NOVBAT before EN)

The MAX98396 datasheet (ADI/Maxim, "Software Shutdown State" + Table 1 + Table 11) defines the required power-up order:

1. **NOVBAT=1** (0x20A0) — "no battery" mode. The Show 5 has **no battery** (it's a wall-powered display). The board ties the amp's VBAT pin accordingly; if the amp still believes VBAT exists, its VBAT UVLO never clears and **the device cannot transition to the active state**.
2. **SPK_EN=1** (0x20AF) — speaker output enable.
3. **GLOBAL_EN=1** (0x210F) — master enable, **LAST**. EN is writable only while in software shutdown; the ENL-class bits (SPK_EN, PCM_RX_EN, …) are hardware-locked unless EN=0.

**Fix (commit `61422f85`):** the DAC event now writes the full sequence with read-back verification and up to 3 retries, logging each attempt to dmesg.

## Layer 4 — GLOBAL_EN must stay active

After the first playback (startup sounds — finally audible!), every later playback failed: `GLOBAL_EN try0/1/2 readback=0`. The driver's POST_PMD wrote `GLOBAL_EN=0` on stream close, and the amp then **refused to re-enter the active state**.

**Fix (commit `aa42ad1d`):** removed the `GLOBAL_EN=0` write from POST_PMD. The amp stays active between playbacks; idle draw is negligible.

## Layer 5 — 🔥 THE KILLER: pin 35 is shared (amp reset vs codec extamp)

`mt_soc_codec_63xx.c`'s `Ext_Speaker_Amp_Change(true)` — invoked on EVERY speaker-path enable:

```c
AudDrv_GPIO_EXTAMP_Select(false);   // pin 35 LOW
udelay(1000);
AudDrv_GPIO_EXTAMP_Select(true);    // pin 35 HIGH
msleep(25);                          // "warm-up"
```

**Pin 35 (KPROW2) is BOTH:**
- the MAX98396's hardware reset (`maxim,reset-gpio = <&pio 35 GPIO_ACTIVE_HIGH>` — active-low reset), and
- the MTK codec's external-amp enable (`aud_pins_extamp_high/low` pinctrl states drive pin 35)

So every time the speaker path enabled, the MTK codec **pulsed the amp's hardware reset LOW**, wiping the amp's registers back to defaults mid-session. The next DAC-event writes then failed (the NOVBAT/ENL state was gone again), and the amp stayed silent. The first sound after each reboot survived because it raced ahead of the first complete enable/close cycle.

**Fix (commit `6e7d3e33`):** `AudDrv_GPIO_EXTAMP_Select()` is now a no-op. Pin 35 is owned exclusively by the MAX98396 driver and stays HIGH (reset released) for the life of the device.

---

## Why the boot sounds worked (and everything else didn't)

The boot/unlock sounds ran in the first moments after boot, when the amp had been freshly initialized and the speaker-path enable sequence hadn't yet run its full close/reopen cycle. Once the first playback closed, the next enable pulsed the reset pin and the amp never recovered. That single asymmetry sent the whole investigation down many wrong paths ("the amp works!", "it's the stream!", "it's the app!").
