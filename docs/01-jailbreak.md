# 01 — Jailbreak (amonet → LineageOS 18.1)

The Echo Show 5 Gen 2 (`cronos`) is jailbreakable via the **amonet** exploitation chain
(the same family as the Fire TV / Echo Show 5 Gen 1 amonet). The device was converted
from Fire OS to LineageOS 18.1 with full root.

## Requirements

- Fire OS **6.5.7.1 / 6.5.7.3** (the exploit was patched in later OTAs — **keep the
  device OFF Wi-Fi until it's flashed** so it can't update)
- amonet package **v2.0.1+** (adds the USBDL unbrick port; v1.0.0 lacks it)
- A Linux host + a data-capable USB cable

## Procedure (summary)

1. **Keep the device offline** — the OTA patches the exploit.
2. Run amonet-cronos (the XDA package). It:
   - Exploits the bootrom, unlocks the bootloader
   - Installs TWRP recovery
   - Provides a USBDL unbrick path if anything goes wrong
3. Flash **LineageOS 18.1** (unofficial cronos build, no GApps) via TWRP
   (or via the amonet tools for the boot partition).
4. **Keep the original boot image** backed up — it's the rollback path.
5. Post-flash: `adb root`, install the HermesOS app, install the keeper, apply the
   kernel fixes (see [02 — Kernel work](02-kernel-work.md)).

## Recovery

- **Boot loop:** Volume-Down + power → fastboot → `fastboot flash boot <good.img>`
- **Unbrick:** the amonet USBDL port (v2.0.1+)

## Notes

- The Show 5 has **no battery** — it must stay plugged in.
- The device has **no Thread/Zigbee radio** (WiFi + BLE only) — it cannot act as a
  Thread border router itself.
- The full step-by-step lives in the `echo-show-jailbreak` Hermes skill.
