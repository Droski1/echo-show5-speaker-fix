# 07 — Smart Home: Lights, Thread & Matter (2026-09-01)

The room's smart-home status + the Thread/Matter border-router investigation.

## The lights (ALL in Home Assistant already)

| Light | Entity | Integration | Control |
|---|---|---|---|
| Govee curtain lights ("Desk Wall Curtans") | `light.smart_curtain_lights` | govee (local API) | ✅ working via `light_ctl.py` |
| Nanoleaf Shapes 461F ("Wall Gorrilah") | `light.bedroom_wall_gorrilah` + 18 per-panel entities | nanoleaf + nanoleaf_panels | ✅ working (direct local API, 16 effects) |
| TP-Link window lights (HS103) | `light.*` (tplink) | tplink | ✅ in HA |

**The unified control tool** (`~/light_ctl.py` on hs2 — the agent + the Show voice both use it):
- Reads the HA long-lived token from `~/.config/ha/token` (never embedded, never in chat)
- HA entities by friendly name: `on/off/dim/color/ct/effect`
- Nanoleaf direct (local API, no HA): `nanoleaf on/off/brightness/color/ct/effect` (the 16 effects)
- The voice profile's system message teaches the agent the tool (via SSH to hs2)

## The Thread/Matter investigation (PAUSED — the plan on file)

**The room's Thread network:** `AMZN-Thread-ec75` — Amazon's OpenThread network (channel 15,
PAN 0xec75). The **Echo Show 8 Gen 2 was the Thread border router** (it has the 802.15.4
radio + Amazon's OTBR stack). The Nanoleaf bulbs + desk RGB strip + the Shapes panel all
live on this network.

**Findings:**
- ⚠️ **The Echo Show 5 Gen 2 has NO Thread/802.15.4 radio** — verified at the kernel level
  (modules: only `mt76x8_wlan` + `mt76x8_bt`; no 802.15.4/88MZ100 nodes in cronos.dtsi;
  the I2C/SPI inventory shows only the audio/touch devices). The Show 8 Gen 2 is the one
  with the Thread radio. **The Show 5 cannot be a Thread border router.**
- The Show 8 still exists (other room, staying there) — the AMZN mesh is alive but its BR
  role can't move to the Show 5.
- **matter-server + the HA Matter integration are running** (`:5580`, integration
  configured) but **0 nodes commissioned** (no Thread radio for the matter-server).

**The Thread dataset (for an OTBR — from the Amazon research):**
```
Network: AMZN-Thread-ec75, channel 15, PAN 0xec75
Network key: a0e84c4aeee9ac6c1cd8eb6f02145b36
Ext PAN: 22cb37addaba0a24, PSKc: b76f084dee955ee6dd9e431aab0e30cc
```
(Full TLV construction + the set_thread_dataset command: the `ha-matter-thread` skill.)

## The paths forward (any one):

1. **TP-Link Deco as the Thread BR** (the user's pick): the newer Decos (X50/X55/X60+)
   have a built-in Thread border router + Matter hub — the bulbs re-pair to the Deco's
   network via the Deco app, then the matter-server tries `commission_with_code`
   (network_only) — works if the Deco exposes the Thread devices to the LAN (open BR).
   ⚠️ Model number + the hs2↔Deco subnet routing still to check.
2. **OTBR USB dongle** (~$15, nRF52840 or ESP32-H2): the guaranteed Matter path — the
   OTBR joins the EXISTING AMZN network (inject the dataset — no device resets needed!),
   then the matter-server commissions with the bulbs' Matter pairing codes.
3. **Bluetooth dongle: NO** — Thread ≠ Bluetooth (802.15.4 vs BLE — different radios).

## Status

- ✅ The lights are controllable today (me + the Show voice) via `light_ctl.py`
- 🕐 Thread/Matter commissioning PAUSED — the plan + the credentials on file here
