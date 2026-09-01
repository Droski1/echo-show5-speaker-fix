# 04 — Landmines & Gotchas

Things that will waste hours of your life on this device. Read before debugging.

## 🚨 NEVER `dumpsys media.audio_flinger` or `dumpsys audio`

On the LineageOS 18.1 build for cronos, both commands call the vendor audio HAL's
`Device::debug()` — which **SEGFAULTs**:

```
F libc    : Fatal signal 11 (SIGSEGV), code 1 (SEGV_MAPERR), fault addr 0x0
             in tid 8201 (audio.service), pid 8201 (audio.service)
#01 pc 000144ab  android.hardware.audio@2.0-impl.so (Device::debug(...)+46)
```

This crash-loops `audioserver` → the whole audio stack bounces → **any active playback dies**.
For hours we thought the stack was unstable — we were crashing it ourselves with every
diagnostic dump. Use `ps`, `tinymix`, and `logcat` instead.

## The regmap debugfs "registers" file shows the CACHE, not the hardware

```bash
# this reads the cache (lies)
cat /sys/kernel/debug/regmap/2-003d/registers
# this reads real hardware
echo 1 > /sys/kernel/debug/regmap/2-003d/cache_bypass
cat /sys/kernel/debug/regmap/2-003d/registers
echo 0 > /sys/kernel/debug/regmap/2-003d/cache_bypass
```

The amp ACK'd writes while the hardware ignored them — the cache recorded them as "written".
Always flip `cache_bypass` before trusting a readback.

## The keeper's app-restart killall

The on-device "keeper" script re-arms adb + the mixer state every 4 s. Its app-restart block
ran `killall mediaserver audioserver` whenever the app was dead — with the app intentionally
disabled, that killed the audio stack **every 4 seconds**. The killall is now removed.

## The server's heal loop

The voice-assistant server's watchdog (`HEAL: mic silent`) ran `killall mediaserver
audioserver` every ~5 minutes. Also kills active playback. Only safe when the app is
genuinely wedged.

## Wireless adb is unreliable

`service.adb.tcp.port=5555` does not reliably persist across reboots. The keeper's
`adb_tcp_on()` must verify the actual LISTEN state via `/proc/net/tcp` (port `15B3`),
not just the property, and restart adbd when needed. If fully locked out: re-enable
Developer options → Wireless debugging on-screen, or use USB.

## Duplicate adb servers

A stale 13-hour-old `adb -L tcp:5037 fork-server` made the device invisible to adb.
Kill ALL adb server processes before diagnosing:

```bash
pkill -f "adb.*fork-server" || true
adb kill-server; adb start-server
```

## `adb root` does not persist across reboots

Re-run `adb root` after every reboot before touching the regmap debugfs or the mixer.

## The test-tone double-wrap

The server's `/show5/test/play` used to double-wrap the JSON body into the b64 field —
"verified" tones were garbage. If tones ever fail with `bad base-64`, check the server
is extracting `b64` from the parsed JSON body, not wrapping the raw body.
