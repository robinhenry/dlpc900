# EDID files

The video timing the DLPC900's IT6535 receiver advertises to the GPU, as TI GUI
batch files. HDMI and DP have separate EDID PROMs and are flashed separately.

| File | Advertises | Pixel mode | Source | Use it for |
|--|--|--|--|--|
| `LCR670S_HDMI_EDID.txt` | 2560×1600@30 + 2716×1600@30 (165 MHz) | single | factory (shipped on board) | **Baseline / restore.** This is the EDID pre-installed on the device. But it's also the timing that freezes and bugs in Video Pattern Mode. |
| `LCR9000_HDMI_EDID_2716_30Hz.txt` | 2716×1600@30 (140 MHz) | single | TI forum via Idris | Native resolution of 2716x1600 (exact same as number of mirrors), HDMI single-pixel, reduced-blanking. This is what has been proven to work on the first microscope. |
| `LCR9000_DP_EDID_2716_60Hz.txt` | 2716×1600@59.92 (269.89 MHz) | dual | TI forum via Idris | DisplayPort at native resolution and 60 Hz — double the HDMI path's pattern-rate ceiling. Tested and working. |

See the following OneNote pages for more information:
- Microscope1 MS11 (original notes form Idris)
- Microscope2 MS3 (notes about 2nd microscope from Robin)
