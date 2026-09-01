# Hermes Show 5 — Echo Show 5 → Hermes Assistant Display

**Jailbreaking an Amazon Echo Show 5 (Gen 2, `cronos`), fixing Amazon's broken audio
bring-up at the kernel level, and turning it into a voice-controlled Hermes assistant.**

Status (2026-09-01): **speaker audio FIXED** (it was being hardware-reset on every
playback — see below), mics FIXED, voice stack operational, everything documented here.

## Why this repo exists

The Show 5's speaker produced **no sound at all** on LineageOS except the boot sounds —
every app tone, alarm, YouTube video, and Bluetooth stream was silent. The investigation
took 5 layers to peel, and the final root cause was a **single shared pin**:

**Pin 35 (KPROW2) is both the MAX98396 speaker amp's hardware reset AND the MTK codec's
external-amp enable. The codec pulsed it LOW on every speaker-path enable — hardware-
resetting the amp mid-session, so it refused to re-enable. Only the first sound after
each reboot ever played.**

The fix is 4 kernel patches (the killer: no-op `EXTAMP_Select`, commit `6e7d3e33`).
YouTube plays fully. Full story: [docs/03-root-cause.md](docs/03-root-cause.md).

## Project layout

```
docs/
  00-project-overview.md   ← the architecture (device ↔ server ↔ AI)
  01-jailbreak.md          ← amonet → TWRP → LineageOS 18.1
  02-kernel-work.md        ← mic fix + speaker fix + build/flash pipeline
  03-root-cause.md         ← the 5-layer speaker investigation
  04-voice-stack.md        ← app, keeper, server, relay, STT/TTS
  05-landmines.md          ← diagnostic traps (dumpsys SEGFAULT, cache lies, …)
  06-timeline.md           ← the debugging session log
patches/
  show5-speaker-fix-4-commits.patch   ← the full speaker fix (base 62877797)
```

## Quick answers

| Question | Answer |
|---|---|
| Why was the speaker silent? | Pin 35 shared: the MTK codec's extamp pulse = the amp's reset. See [03](docs/03-root-cause.md) |
| What's the fix? | Kernel commit `6e7d3e33` (no-op `EXTAMP_Select`) + 3 supporting commits. Patch in [`patches/`](patches/) |
| Why were the mics silent? | AIC3101 mic bias never enabled + PGA muted. Commit `62877797`. See [02](docs/02-kernel-work.md) |
| How do I build/flash the kernel? | [docs/02-kernel-work.md](docs/02-kernel-work.md) |
| What breaks if I touch the audio HAL? | `dumpsys media.audio_flinger`/`dumpsys audio` SEGFAULT it. See [05](docs/05-landmines.md) |
| How does the voice assistant work? | [docs/04-voice-stack.md](docs/04-voice-stack.md) |

## Kernel fix commits

| Commit | What |
|---|---|
| `62877797` | Mic fix: AIC3101 bias + PGA unmute (15.5 dB) + CI workflow |
| `72b0243e` | Amp: fresh hardware reset at stream init + verified GLOBAL_EN writes |
| `61422f85` | Amp: datasheet power-up order (NOVBAT=1 → SPK_EN=1 → GLOBAL_EN=1 LAST) |
| `aa42ad1d` | Amp: keep GLOBAL_EN active across playbacks |
| `6e7d3e33` | **THE SPEAKER FIX**: no-op `EXTAMP_Select` (pin-35 reset pulse) |

Repo: `Droski1/android_kernel_amazon_mt8163` (Amazon MT8163 fork, `lineage-18.1`).
