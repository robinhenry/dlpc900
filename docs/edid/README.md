# EDID files

EDIDs for the DMD's HDMI/DisplayPort input — the video timing the DLPC900's
IT6535 receiver advertises to the GPU. Flashing is done with **TI's DLPC900 GUI
on Windows** (Batch Files → Load Batch File → Execute All, DLPU102 §3.13). HDMI
and DP have **separate PROMs behind separate jumpers** — J3 for HDMI, J4 for DP
(install one, remove the other, power-cycle around it) — so reflashing one
cannot break the other. For DP, unplug *both* video cables before powering up.

**Key note:** the DLPC900's Video Pattern Mode freezes on the factory
2716×1600@30 timing (165 MHz clock, huge blanking). Idris experienced this on the first microscope, and got help on some TI forum website, where they provided us with new EDID files (the `LCR9000_*` ones). See MS11 on the Microscope1 notebook for more information.

These new `LCR9000_*` files advertise the same resolutions with a reduced-blanking timing (~140 MHz) that locks properly.

Both are now flashed and working:
- **HDMI, 30 Hz** (`dmd stream-hdmi`) — 139.8 MHz, single-pixel.
- **DP, 60 Hz** (`dmd stream-dp`) — 269.89 MHz, full resolution. Confirmed by
  streaming full-screen video onto the DMD.

Note on "dual-pixel": 269.89 MHz is indeed above the ~175 MHz single-pixel limit
**for one controller**, but this is a dual-controller board — the splitter FPGA
gives each DLPC900 half the array (~135 Mpix/s), so the port routing stays
`PORT1_SINGLE` (`0x1A03 = 0x00`). `port2_syncs_valid` reads False whichever
routing is set; it is not fed on this board.

## The files

| File | Advertises | Pixel mode | Source | Use it for |
|--|--|--|--|--|
| `LCR670S_HDMI_EDID.txt` | 2560×1600@30 + 2716×1600@30 (165 MHz) | single | factory (shipped on board) | **Baseline / restore.** This is the EDID pre-installed on the device. But it's also the timing that freezes Video Pattern Mode. |
| `LCR9000_HDMI_EDID_2716_30Hz.txt` | 2716×1600@30 (140 MHz) | single | TI forum via Idris | Native resolution of 2716x1600 (exact same as number of mirrors), HDMI single-pixel, reduced-blanking. This is what has been proven to work on the first microscope. |
| `LCR9000_DP_EDID_2716_60Hz.txt` | 2716×1600@59.92 (269.89 MHz) | single per controller | TI forum via Idris | **In use.** DisplayPort at native resolution and 60 Hz — double the HDMI path's pattern-rate ceiling. Tested and working. |

## Which to use

`dmd stream-dp` (60 Hz, full resolution) unless you need the HDMI path;
`dmd stream-hdmi` (30 Hz) is the fallback and stays available on its own PROM.

Always confirm a flash worked by re-dumping the live EDID and checking the pixel
clock changed:

```bash
uv run scripts/check_edid.py                   # decode the live link
uv run scripts/check_edid.py --file <file>     # decode a batch file offline
```
