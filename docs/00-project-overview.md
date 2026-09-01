# Hermes Show 5 — Project Overview

The **Hermes Show 5** project turns an Amazon Echo Show 5 (2nd Gen, codename `cronos`)
into a voice-controlled Hermes assistant display — and, along the way, fixes Amazon's
broken audio bring-up so the device can actually make sound.

**Status (2026-09-01):** ✅ Speaker audio FIXED (kernel), ✅ voice stack operational,
✅ documented in this repo.

## The hardware

| | |
|---|---|
| Device | Amazon Echo Show 5 Gen 2 (2021), `cronos`, C76N8S |
| SoC | MediaTek MT8163 (4× A53), 2 GB RAM |
| Screen | 5.5" 960×480 |
| ROM | LineageOS 18.1 (Android 11), unofficial, no GApps |
| Kernel | `4.9.337` (Amazon MT8163 fork, branch `lineage-18.1`) |
| Speaker amp | **MAX98396** 20V Class-DG, I2C `2-003d`, reset on GPIO 35 (KPROW2) |
| Codec | MTK MT8163 internal (`mt_soc_codec_63xx`) + TLV320AIC3101 (mics) |
| Radio | WiFi + BLE only (no Thread/Zigbee) |

## The architecture

```
┌─────────────── Echo Show 5 (cronos) ───────────────┐
│  LineageOS 18.1 + patched kernel (mic + speaker)   │
│  ┌───────────────┐   ┌──────────────────────────┐  │
│  │ HermesOS app  │   │ hermeskeep (init svc)     │  │
│  │ WS voice client│   │ app keep-alive, amps, adb│  │
│  │ mic → PCM 16k  │   │ ALSA node repair         │  │
│  │ TTS ← WAV      │   └──────────────────────────┘  │
│  │ UI overlay     │                                 │
│  └───────┬───────┘                                 │
└──────────┼──────────────────────────────────────────┘
           │ WebSocket (PCM 16k + TTS frames)
┌──────────▼─────────────────────────────────────────┐
│ hs2: show5-server.py (:8792/:8794)                 │
│  WS bridge, TTS push, heal watchdog (adb),         │
│  /show5/test/play tone endpoint                    │
└──────────┬─────────────────────────────────────────┘
           │ relay (:8792) + Hermes Gateway SSE
┌──────────▼─────────────────────────────────────────┐
│ pop-os: voice.py                                   │
│  faster-whisper STT (CUDA) → Hermes → kokoro TTS   │
│  wake word, VAD, ALEXA-MODE responses              │
└────────────────────────────────────────────────────┘
```

- **The device** runs the HermesOS app (WS voice client), a system-level keeper
  (`hermeskeep`, re-arms adb/amps/app state every 4 s), and the patched kernel.
- **hs2** runs the voice server (WebSocket bridge + heals) and the Hermes Gateway.
- **pop-os** runs the heavy AI: faster-whisper STT, the Hermes API call, kokoro TTS.
- The Gateway streams `reasoning_content` + tool-progress SSE so the Show can display
  the agent's live thought process (💭 + 🛠), one line at a time.

## Docs

| Doc | Covers |
|---|---|
| [01 — Jailbreak](docs/01-jailbreak.md) | amonet → TWRP → LineageOS 18.1 |
| [02 — Kernel work](docs/02-kernel-work.md) | the mic fix + the speaker fix + build/flash pipeline |
| [03 — Speaker root cause](docs/03-root-cause.md) | the full 5-layer investigation |
| [04 — Voice stack](docs/04-voice-stack.md) | app, server, keeper, relay, STT/TTS |
| [05 — Landmines](docs/05-landmines.md) | diagnostic traps (dumpsys SEGFAULT, cache lies, …) |
| [06 — Timeline](docs/06-timeline.md) | the debugging session log |

## Repo history

- **2026-09-01** — created: the speaker-silence root cause + fix docs and patches,
  then expanded to the full project overview.
