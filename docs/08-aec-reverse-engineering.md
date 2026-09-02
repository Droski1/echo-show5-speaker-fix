# 08 — AEC Reverse-Engineering: What Worked, What Didn't (2026-09-01)

The full saga of forcing Amazon's AEC out of the stock HAL on the Echo Show 5 (LineageOS).

## The goal
Wake word + voice over the BT stream (YouTube video audio through the A2DP sink),
without the STT transcribing the video's speech.

## WHAT WORKED ✅

### 1. Mic streams THROUGH the BT (no more gating)
Removed both BT mic-gates from the app (the kernel handles play+record since the
speaker fixes) + pinned the AudioRecord to the built-in mic (`setPreferredDevice`).
Verified: mic peaks flow while the iPad streams.

### 2. The HAL's AEC engine forced ON (the reverse-engineering!)
The LineageOS vendor carries Amazon's REAL HAL (`audio.primary_amazon.mt8163.so`).
Six surgical ARM32 patches force the AEC chain on:

| # | Symbol | Patch | Effect |
|---|---|---|---|
| 1 | `GetForceAECRecState` | return 1 | the BES/AEC state forced |
| 2 | `GetForceMagiASRState` | return 1 (nop the mvneq) | input source forced to CUSTOMIZATION2 |
| 3 | `GetBesRecScene` | return 1 | `besrecord_scene=1` (was -1) |
| 4 | `CheckBesRecordBypass` | return 0 | the BES preprocess runs |
| 5 | `createCaptureHandler`'s NORMAL branch | BL → `AudioALSACaptureHandlerAEC` ctor | **the capture runs through the AEC engine** |
| 6 | `AudioALSACaptureDataProviderEchoRef::open`'s enable-branch | NOP the beq | **the echo reference (48k/2ch DL1 capture) starts** |

Deploy = `mount --bind /data/local/tmp/aec.so /vendor/lib/hw/...` + `setprop ctl.restart audioserver`
(reboot = instant undo). Verified live: `AudioALSACaptureHandlerAEC: +open()` +
`EchoRef +readThread()` + `ASP/EchoReferenceWithExternalSync: OnWriterCreated (48000/2ch)`.

**Result: the music's steady hum dropped ~10x in the mic (4k-17k peaks → 1.1k-1.7k).**

### 3. The OpenWakeWord gate (the transcription-killer)
`hermes_v0.1.onnx` gates the STT: `prob < 0.45` → the utterance is dropped
(`skip: no wake - external audio filtered`) instead of transcribed.

### 4. Amazon-style ducking (wake-triggered)
The duck fires on the WAKE confirmation (not the speech-start). The server pushes
`{t:"duck"}` at the wake; the app ducks the media volume.

### 5. Wake-aware silence window
2.5s of silence after a wake (1.0s otherwise) — the user's pauses after "Hermes"
no longer truncate the request.

### 6. MUSIC MODE (the sure-fire wake-over-music)
The app rides the media volume at ~25% whenever the BT stream is active — the
software wake ALWAYS hears the user.

## WHAT DIDN'T WORK ❌

### 1. The `AECOn` / `ForceAECRec` audio parameters
The MTK keys are BARE flags but the AudioManager appends `=` — the HAL leaves them
as "remain". The params can't reach the HAL's setters via the app.

### 2. The `AUDIO_SOURCE_CUSTOMIZATION1` (1011) raw source
The AudioFlinger validates the source range on API 30 — 1011 is rejected
(`dead IAudioRecord` retry loop). `audio.hw.bypass` doesn't help.

### 3. The ResidualEchoSuppressor added to the ASR path
The `Frequency Masking Lite RES` (a VOIP-chain algorithm) MUFFLED the user's voice
(the high-frequency masking ate the speech). Reverted — Amazon's ASR path uses the
SerialARA for the residual, not the RES.

### 4. The AEC's reference alignment (the garble)
The 48k/2ch reference vs the 16k mono mic + the path delay → the adaptive filter
can't converge cleanly → residual garble + crackle. The AEC cancels the bulk of the
music but the VIDEO'S SPEECH (the residual) still leaks into the mic at ~14k-21k
peaks when the volume's up.

### 5. The wake-gate's `_WOKE` bypass (the CURRENT bug)
The utterance-gate checks `not globals().get("_WOKE")` — the streaming ONNX sets
`_WOKE=True` on ANY "hermes"-like audio (including the video's!), which SKIPS the
gate → the STT transcribes the video word-for-word again. THE FIX: the gate must
ALWAYS run the ONNX on the utterance, ignoring `_WOKE`.

## The current state (18:20, measured)
- The video's speech transcribed word-for-word (the `_WOKE` bypass!)
- The mic peaks 14k-21k (the video at full volume — the music-mode duck not engaging?)
- The AEC engine + reference live (the handler + the EchoRef thread confirmed)

## The next fix (in progress)
1. The wake-gate: always-run the ONNX (drop the `_WOKE` check).
2. Verify the music-mode duck (the app's btMusic detection + the volume write).


## The admin panel: the full AEC cockpit (2026-09-01, same session)
- **AEC subtraction view**: RAW mic (tinycap dev 8, pre-AEC) vs OUTPUT (the app's
  stream, the AEC'd) vs REFERENCE (BT + the duck state) — live bars, every 2s.
- **NINE tuning sliders** (live-adjustable via POST /admin/set, pushed to the app):
  wake threshold, talk gate, VAD sensitivity, silence (no-wake), silence (after
  wake), max utterance, wake pre-roll, duck depth, mic gain. Each has a per-slider
  reset (⭯) + a RESET ALL.
- **Live-responsive needles**: the sliders show the real-time data (the RMS rides
  the talk/vad sliders, the wake-prob rides the wake slider, the utterance length
  rides the silence sliders, the media volume rides the duck slider).
- **The waveform**: streams the VAD-path PCM continuously (the LISTEN toggle only
  gates the audible monitor, not the drawing).
- **Slider sync**: the server pushes the current tunables on the admin's WS connect,
  so a refresh shows the device's ACTUAL settings (no more phantom defaults).

## The current voice-loop state (21:02, measured)
- The wake-gate: ALWAYS runs the ONNX (the _WOKE bypass is dead) — the video's
  speech gets `skip: no wake` and is never transcribed.
- The AEC engine + the echo reference live (the handler + the EchoRef thread).
- The music-mode duck rides the BT stream at ~25% (the A2DP's absolute volume).
- The residual video-speech still leaks into the mic at ~14k-21k peaks (the AEC's
  reference alignment) — the wake-gate + the duck + the sliders are the defenses.
