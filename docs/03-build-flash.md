# 03 — Build & Flash Workflow

The proven pipeline for building, repacking, and flashing a kernel on the Show 5 (cronos).

## Toolchain

- Tree: the Amazon MT8163 kernel fork (`lineage-18.1` branch)
- Compiler: **Linaro GCC 6.3.1** `aarch64-linux-gnu` — REQUIRED for this 4.9 tree
  (`gcc-linaro-6.3.1-2017.02-x86_64_aarch64-linux-gnu`); plus bison/flex/bc/m4 on PATH

## Build

```bash
export ARCH=arm64
export CROSS_COMPILE=/path/to/gcc-linaro-6.3.1/bin/aarch64-linux-gnu-
make cronos_defconfig    # or use the existing .config
make -j12 Image dtbs KCFLAGS="<missing-header include list>"
```

Incremental builds take ~6 seconds after the first full build. The `Image` is ~15.3 MB.

The kernel source uses a handful of non-exported headers; the build needs this KCFLAGS
include list (substitute your tree path):

```
-I<K>/drivers/mmc/host -I<K>/drivers/misc/mediatek/btcvsd -I<K>/drivers/misc/mediatek/leds
-I<K>/drivers/staging/android/ion -I<K>/drivers/misc/mediatek/rtc/mt6323 -I<K>/drivers/thermal
-I<K>/drivers/misc/mediatek/uart/mt8163 -I<K>/drivers/misc/mediatek/video/mt8163/dispsys
-I<K>/drivers/misc/mediatek/video/mt8163/videox -I<K>/drivers/misc/mediatek/video/include
```

## Repack the boot image

The boot.img contains the kernel + a DTBs tail (11 board-variant device trees) + the ramdisk.

```bash
# unpack once (from the LOS zip's boot.img)
python3 boot_repack.py unpack lineage-boot.img bootimg/
# after rebuilding Image:
python3 boot_repack.py repack bootimg/ arch/arm64/boot/Image boot-new.img
```

**The kernel load address MUST stay `0x40080000`** — verify the header after repacking
(`boot-new.img` should be ~8.3 MB).

## Flash

The user runs the flash themselves (their standing preference for block-device writes).
Backup first, then:

```bash
# backup the current boot
adb shell "dd if=/dev/block/by-name/boot of=/data/local/tmp/boot-current.img bs=1M"
adb pull /data/local/tmp/boot-current.img boot-current-backup.img

# push the new boot
adb push boot-new.img /data/local/tmp/boot-new.img
adb shell md5sum /data/local/tmp/boot-new.img

# flash + reboot (run as user)
adb shell "dd if=/data/local/tmp/boot-new.img of=/dev/block/by-name/boot bs=1M && sync && echo FLASH_OK"
adb reboot
```

## Verify after boot

```bash
adb shell cat /proc/version          # expect 4.9.337-g<your-commit>
adb shell dmesg | grep -E "NOVBAT|GLOBAL_EN"   # power-up sequence logs
```

Healthy boot looks like:

```
max98396 2-003d: NOVBAT 0x20A0 write ret=0 readback=1
max98396 2-003d: GLOBAL_EN 0x210F try0 ret=0 readback=1
```

## Rollback / recovery

- Volume-Down + power → fastboot
- `fastboot flash boot <good-boot.img>`
- Keep the pre-saga boot backed up (`boot-current-backup.img`) and the original LOS boot
  (`lineage-boot.img` from the ROM zip)
