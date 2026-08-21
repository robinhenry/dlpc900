# EDID files

EDIDs for the DMD's HDMI/DisplayPort input — the video timing the DLPC900's
IT6535 receiver advertises to the GPU. Flashing is done with **TI's DLPC900 EDID
tool on Windows** (note: needs the J3/HDMI EDID-write jumper).

**Key note:** the DLPC900's Video Pattern Mode freezes on the factory
2716×1600@30 timing (165 MHz clock, huge blanking). Idris experienced this on the first microscope, and got help on some TI forum website, where they provided us with new EDID files (the `LCR9000_*` ones). See MS11 on the Microscope1 notebook for more information.

These new `LCR9000_*` files advertise the same resolutions with a reduced-blanking timing (~140 MHz) that locks properly.

There are 2 options:
- HDMI, single-pixel, 30Hz
- DP, dual-pixel, 60Hz: currently untested, but the forum people provided files for this. Note that 60Hz is only achievable with DP + dual-pixel, since it requires a ~270MHz, which is above the single-pixel limit of ~175MHz.

## The files

| File | Advertises | Pixel mode | Source | Use it for |
|--|--|--|--|--|
| `LCR670S_HDMI_EDID.txt` | 2560×1600@30 + 2716×1600@30 (165 MHz) | single | factory (shipped on board) | **Baseline / restore.** This is the EDID pre-installed on the device. But it's also the timing that freezes Video Pattern Mode. |
| `LCR9000_HDMI_EDID_2716_30Hz.txt` | 2716×1600@30 (140 MHz) | single | TI forum via Idris | Native resolution of 2716x1600 (exact same as number of mirrors), HDMI single-pixel, reduced-blanking. This is what has been proven to work on the first microscope. |
| `LCR9000_DP_EDID_2716_2560_60Hz.txt` | 2716×1600@60 + 2560×1600@60 (270 MHz) | dual | TI forum via Idris | **Potential upgrade** DisplayPort + dual-pixel. It might enable us to move to 60Hz at native resolution (2716x1600). But not yet tested. |

## Which to use

Always confirm a flash operation worked by re-dumping the live EDID on Linux
(`/sys/class/drm/card*-HDMI-*/edid`) and checking the pixel clock changed.
