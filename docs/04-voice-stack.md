# 04 — Voice Stack

The full voice-assistant stack: device app → server → relay → AI (STT/LLM/TTS).

## On-device: the HermesOS app (`com.hermes.show5`)

- WebSocket voice client to hs2 (16 kHz mono PCM up, WAV down)
- Captures the mic continuously; streams frames to the server
- Plays TTS WAVs via `AudioTrack` (STREAM_ALARM at max volume; ducks STREAM_MUSIC
  during playback and restores it after)
- Shows the agent's live state as a UI overlay (reasoning + tool activity)
- Auto-starts via boot receiver; supervised by `hermeskeep`

## On-device: `hermeskeep` (init service, /data/local/hermeskeep.sh)

A root init service (`hermeskeep`) that re-arms device state every 4 s:

- **App keep-alive** — restarts `com.hermes.show5` the moment it dies
- **Speaker amps** — `Speaker_Amp_Switch`, `Headset_Speaker_Amp_Switch`,
  `Audio_Amp_L/R_Switch` On + `Speaker Volume A` 15 (the HAL resets them on every
  playback; the keeper re-arms them). ⚠️ The real physical gates on this MTK board
  are `Audio_Amp_L/R_Switch` — the others are NOT sufficient alone.
- **Wireless adb** — `service.adb.tcp.port=5555` does not reliably survive reboots;
  the keeper re-enforces it and verifies the actual LISTEN state via `/proc/net/tcp`
- **ALSA node repair** — recreates `/dev/snd/*` nodes if the boot's udev missed them

⚠️ 2026-09-01: the app-restart block's `killall mediaserver audioserver` was REMOVED —
it killed the whole audio stack every 4 s whenever the app was intentionally stopped.

## On hs2: `show5-server.py` (`~/show5/`)

The WebSocket bridge + supervisor:

- Serves the device's WS voice channel (`:8794`/`:8792`; the container is
  host-networked so localhost = hs2)
- Pushes TTS audio to the device (real path — parse JSON, extract b64)
- `POST /show5/test/play` — test-tone endpoint (⚠️ was double-wrapping JSON; fixed)
- Heal watchdog: detects mic-silent / app-dead and restarts the app via adb
  (⚠️ its `killall mediaserver audioserver` heal also kills active playback — only
  safe when the app is genuinely wedged)

## On hs2: the relay (`relay.py`, `:8792`)

Bridges the device's voice channel to the Hermes Gateway with instant responses and
background processing; streams reasoning/tool-progress SSE for the on-screen display.

## On pop-os: `voice.py`

The AI side (CUDA laptop):

- **STT:** faster-whisper (CUDA fp16, ~0.1 s), with VAD + high-pass/normalize pre-STT
  (distant-speech rumble was causing empty transcripts)
- **Wake:** fuzzy "hermes" wake word (greeting-gated + blocklist), custom OpenWakeWord
  option
- **LLM:** the Hermes Gateway (main agent, `:8642`, deepseek)
- **TTS:** kokoro (`:8001`, warm GPU ~0.06 s) with piper CPU fallback
- **ALEXA-MODE:** a client system message makes the agent answer like Alexa — one short
  sentence, <15 words (the Show display's preferred persona)
- Logs every utterance to `samples/`

## The audio paths (what plays where)

| Path | Hardware | Status |
|---|---|---|
| Speaker playback (all streams) | MAX98396 amp ← I2S0 ← MTK AFE ← HAL | ✅ fixed 2026-09-01 |
| Mic capture | TLV320AIC3101 ← I2C ← HAL | ✅ fixed (mic-bias commit) |
| BT A2DP sink (iPad music) | BT controller → sink → HAL | ⚠️ separate issue: codec negotiation (`Current Codec: None`) |

## ⚠️ Two gotchas that kill the mic (2026-09-01, both hit in one session)

1. **`pm disable-user` (or any disable/enable cycle) STRIPS the app's RECORD_AUDIO grant.**
   The HAL then refuses the capture with `getInputForAttr permission denied: recording not
   allowed for uid <uid>` → the app's AudioRecord init fails → the mic is "silent" → the
   heal watchdog restarts the app in a loop. **Fix: re-grant after any disable:**
   `adb shell pm grant com.hermes.show5 android.permission.RECORD_AUDIO`
   (and `SYSTEM_ALERT_WINDOW` for the overlay).
2. **The app gates the mic while ANY A2DP sink device is CONNECTED** — even idle, with
   nothing playing. A connected-but-silent phone shows as `BT streaming ON (mic gated)`
   every 2s in the server log. The server's `BT sink disconnected (pushed to app)` push
   does NOT override the app's own re-check. **Fix: disconnect the BT device**
   (`adb shell svc bluetooth disable && sleep 3 && svc bluetooth enable`) or turn BT off
   on the source device.

Symptom fingerprint: `voice channel up/down` flapping every ~12s + `HEAL: mic silent NNNNNs`
in the server log = one of these (or both).

## Adaptive TTS volume (ambient-matched, LOCKED 2026-09-01)

The TTS volume adapts to the room's noise floor (measured by the app's own mic):

```java
// MicService.java playWav():
// ambientLevel = rolling average of quiet frames' raw RMS (the room's noise floor)
float vol = Math.min(1f, Math.max(0.05f, ambientLevel / 11400f));
track.setVolume(vol);
```

- Quiet room (raw RMS ~50-100): **5%** (the floor)
- Noisy room (raw ~2000): ~18%
- Loud (raw ~5000+): ~44%; 11400+ → 100%

**History (why the floor used to be 85%):** the original formula had a 0.85 floor
(`max(0.85, ambient/5000)`) as a **workaround for the silent amp** (the MTK HAL seemed
to "mute below ~0.85" — actually the broken amp). After the speaker fix (kernel
`6e7d3e33`) the floor was removed and the curve tuned with the user: 0.15→0.10→0.05
and /4000→/5700→/11400. **5% / /11400 = user-confirmed "Perfect" with headphones
(2026-09-01).**

Notes:
- `ambientLevel` is the RAW pre-AGC RMS — the server's RMS display includes its VoiceDSP
  AGC boost, so the scales differ (~15-40×).
- The quiet-frame threshold is `raw < 2500` (was `raw < 44` — the room never hit it, so
  the ambient was stuck at the default). Known refinement: exclude near-zero frames
  (mic-freeze periods) with a lower bound so the ambient stays honest.

## Mic recovery after TTS (playend — FIXED 2026-09-01)

**Symptom:** after the TTS answered, the mic stayed dead ~30s (and sometimes the app
self-killed via the wedge counter).

**Root cause:** the server scheduled the post-TTS audio-HAL reset at a fixed
`call_later(8, ...)` — 8s AFTER the TTS push (a relic of the old app-self-restart
design). The app re-opened the mic into the still-wedged HAL, retried every 5s, and the
failure counter escalated to a self-kill.

**Fix (two parts):**
1. **App** (`MicService.java` playWav finally): sends `{"t":"playend"}` over the WS the
   moment playback ends; the post-playback retry is 2s (not 5s) for 20s after playback.
2. **Server** (`show5-server.py`): the `playend` message cancels the 8s fallback and
   schedules the HAL reset in **1s** (`_schedule_post_tts_reset(1)`); the 8s fallback
   remains in case the playend is lost.

**Result:** mic back in ~4s (measured: playend 14:25:43 → reset 1s → mic peaks 14:25:47).
The playend handler calls the module-level `_post_tts_reset` (scope bug fixed: it was
nested inside the push function and invisible to the voice channel).

## Mic recovery after TTS — FINAL design (2026-09-01, ~2s recovery)

The evolution: 30s → 4s → **~2s**.

1. **Pre-stop**: `playWav()` stops + releases the recorder BEFORE the playback —
   the concurrent play+record was what wedged the MTK HAL. No wedge = no recovery cascade.
2. **playend cancels the HAL reset**: the app sends `{"t":"playend"}` when playback ends;
   the server cancels the scheduled post-TTS `killall mediaserver audioserver` (the reset
   was the ~25s delay — the HAL restart outlasted the app's re-open wait, triggering the
   frozen-watchdog re-open loop). The new app recovers cleanly without any HAL kill.
3. **2s grace**: the mic thread waits ~2s after the playback ends before re-opening
   (the HAL needs a moment to release the output), then opens fresh — reads flow
   immediately.

Measured: playend 14:32:23 → mic peaks 14:32:25 — **2 seconds**.
The 8s reset fallback remains for legacy app builds that don't send playend.

## The 30s stall — playState stuck at PLAYING (FIXED 2026-09-01, measured)

**Symptom:** the mic stalled ~30s after each TTS — the recovery eventually worked but
way too late.

**Measurement (1s-resolution capture):** the TTS wav (~3s) finished, but the
AudioTrack's `playState` stayed at PLAYING — the wait loop
`while (PLAYING && elapsed < 30000)` spun the **full 30s cap** before the finally ran.
The playback head position was ALSO unreliable on this MTK HAL (never reached the end,
never stalled — both break conditions missed).

**Fix:** wait the WAV's actual duration instead of trusting the HAL state:
```java
int playMs = (int) (pcmLen * 1000L / (rate * 2)) + 800;   // duration + slack
while (track.getPlayState() == AudioTrack.PLAYSTATE_PLAYING
        && System.currentTimeMillis() - playStart < playMs) { sleep(50); }
```

**Measured after:** playend at ~4s (was 30s), mic back ~2s later, no frozen-watchdog,
no self-kill. Combined with the pre-stop + playend-cancels-reset, the full loop is:
TTS → ~4s play → playend → ~2s mic recovery → listening.
