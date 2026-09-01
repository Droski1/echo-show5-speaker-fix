# 02 — Kernel Work

Two kernel fixes have been made to the Amazon MT8163 fork (`lineage-18.1` branch):

1. **The mic fix** (commit `62877797`) — the mics were silent on LineageOS
2. **The speaker fix** (commits `72b0243e` → `6e7d3e33`) — the amp was silent

Both are described here; the speaker root cause has its own deep-dive
([03 — Speaker root cause](03-root-cause.md)).

## The mic fix (commit `62877797`)

**Symptom:** the Show's mics (TLV320AIC3101, I2C `0-0018`) were completely silent —
the voice assistant heard nothing.

**Root cause:** the AIC3101's **mic bias** was never enabled and the record PGA was
**muted** (the stock Amazon firmware enabled them; the LineageOS driver bring-up didn't).

**Fix:** in the `tlv320aic310x` driver — enable the mic bias rail and unmute the PGA
with a ~15.5 dB gain. The kernel also carries the CI workflow
(`.github/workflows/build-kernel.yml`) so any future fix builds in GitHub Actions.

**Verification:** `tinycap` shows real samples; the voice assistant's STT works.

## The speaker fix (4 commits)

**Symptom:** total speaker silence except boot sounds — the MAX98396 amp was being
**hardware-reset on every playback** via the shared pin 35. See
[03 — Speaker root cause](docs/03-root-cause.md) for the full story.

| Commit | File | Fix |
|---|---|---|
| `72b0243e` | `sound/soc/codecs/max98396.c` | Fresh hardware reset at stream init + verified GLOBAL_EN writes |
| `61422f85` | `sound/soc/codecs/max98396.c` | Datasheet power-up order: **NOVBAT=1 → SPK_EN=1 → GLOBAL_EN=1 LAST** |
| `aa42ad1d` | `sound/soc/codecs/max98396.c` | Keep GLOBAL_EN active across playbacks |
| `6e7d3e33` | `sound/soc/mediatek/mt_soc_audio_8163_amzn/AudDrv_Gpio.c` | **THE FIX**: no-op `EXTAMP_Select` (pin-35 amp-reset pulse) |

Apply: `patches/show5-speaker-fix-4-commits.patch` (base `62877797`).

## Build & flash pipeline

### Toolchain
- **Linaro GCC 6.3.1** `aarch64-linux-gnu` (REQUIRED for this 4.9 tree) + bison/flex/bc/m4

### Build
```bash
export ARCH=arm64
export CROSS_COMPILE=/path/to/gcc-linaro-6.3.1/bin/aarch64-linux-gnu-
make -j12 Image dtbs KCFLAGS="<missing-header include list>"
```
Incremental builds: ~6 s. `Image` ≈ 15.3 MB. The KCFLAGS list (substitute `<K>`):
```
-I<K>/drivers/mmc/host -I<K>/drivers/misc/mediatek/btcvsd -I<K>/drivers/misc/mediatek/leds
-I<K>/drivers/staging/android/ion -I<K>/drivers/misc/mediatek/rtc/mt6323 -I<K>/drivers/thermal
-I<K>/drivers/misc/mediatek/uart/mt8163 -I<K>/drivers/misc/mediatek/video/mt8163/dispsys
-I<K>/drivers/misc/mediatek/video/mt8163/videox -I<K>/drivers/misc/mediatek/video/include
```

### Repack the boot image
```bash
python3 boot_repack.py unpack lineage-boot.img bootimg/   # once
python3 boot_repack.py repack bootimg/ arch/arm64/boot/Image boot-new.img
```
**Kernel load address MUST stay `0x40080000`.**

### Flash (user-run — block-device writes are the user's call)
```bash
adb shell "dd if=/dev/block/by-name/boot of=/data/local/tmp/boot-current.img bs=1M"   # backup
adb push boot-new.img /data/local/tmp/boot-new.img
adb shell "dd if=/data/local/tmp/boot-new.img of=/dev/block/by-name/boot bs=1M && sync && echo FLASH_OK"
adb reboot
```

### Verify
```bash
adb shell cat /proc/version                 # 4.9.337-g<commit>
adb shell dmesg | grep -E "NOVBAT|GLOBAL_EN"  # power-up sequence logs
```
Healthy: `NOVBAT ... readback=1` + `GLOBAL_EN ... try0 ... readback=1`.

### Rollback
Volume-Down + power → fastboot → `fastboot flash boot <good.img>` (backups:
`boot-current-backup.img` on the host, `lineage-boot.img` from the ROM zip).
