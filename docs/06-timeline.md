# 05 — Timeline

The full debugging session, 2026-09-01 (with pre-history from Aug 31).

## Aug 31 — the mystery begins

- Echo Show 5 (cronos) on LineageOS 18.1: speaker produces NO sound for app tones, alarms,
  BT streaming. Boot sounds work. "Verified" test tones via the voice-assistant server
  appear to play (AudioFlinger shows tracks) but nothing is audible.

## Sep 1 — the investigation

- **Amp register forensics:** the MAX98396 (2-003d) shows all registers at power-on
  defaults. Writes are ACK'd but ignored; the regmap cache hides it (cache_bypass reveals
  the truth). GLOBAL_EN=0, SPK_EN=0, NOVBAT=0.
- **Server bug found:** `/show5/test/play` double-wraps the JSON body into the b64 field —
  every test tone since Aug 31 was garbage. Fixed.
- **First real tone delivered** (16 kHz mono WAV → HAL → PCM 23/MAX98396): still silent.
- **The datasheet:** NOVBAT → SPK_EN → GLOBAL_EN power-up order; ENL write locks;
  no-battery board needs NOVBAT=1.
- **Kernel fix round 1** (`72b0243e`): fresh reset + verified GLOBAL_EN writes. Rebuilt,
  repacked, flashed. Startup sounds still play; everything after dies.
- **Kernel fix round 2** (`61422f85`): full power-up sequence with read-back + retries.
  Startup sounds play; later playbacks fail with `GLOBAL_EN readback=0`.
- **Kernel fix round 3** (`aa42ad1d`): keep GLOBAL_EN active across playbacks.
  Still dies after the first sound.
- **The breakthrough:** dmesg shows ALL registers back at defaults after the first
  playback — the amp is being hardware-reset. Fault registers are clean. The reset pin
  (pin 35/KPROW2) is shared with the MTK codec's extamp control.
- **Kernel fix round 4** (`6e7d3e33`): `AudDrv_GPIO_EXTAMP_Select()` → no-op.
- **✅ VERIFIED:** YouTube plays fully. Startup sounds play after every reboot.
  dmesg shows NOVBAT=1 + GLOBAL_EN=1 landing with no further reset pulses.

## Side discoveries

- `dumpsys media.audio_flinger` / `dumpsys audio` SEGFAULT the vendor audio HAL
  (`Device::debug()`) — the audio stack was being crash-looped by our own diagnostics.
- The keeper's app-restart `killall mediaserver audioserver` + the server's 5-minute heal
  both killed active playback.
- Duplicate adb servers (one stale, 13 h old) made the device invisible.

## Current deployment state (end of session)

- Kernel `4.9.337-g6e7d3e33` on the device
- HermesOS voice app: **disabled** (`pm disable-user`) — re-enable for the voice assistant
- Show5 server: **stopped** — restart via `bash ~/show5/restart.sh`
- Keeper on-device: patched (killall removed)
- Known separate issue: BT A2DP sink codec negotiation (`Current Codec: None`) on the
  iPad music path
