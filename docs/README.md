# Reference documents

Texas Instruments guides for the DLPC900 controller (our board is a DLPC900
driving a DLP670S DMD).

| File | TI doc | Contents |
|--|--|--|
| `dlpc900-programmers-guide.pdf` | DLPU018J | **USB/I²C command protocol** — the reference for this client. Status commands in §2.1, firmware/version in §2.1.4–2.1.5, fault status in §3. |
| `lightcrafter-dual-dlpc900-evm-guide.pdf` | DLPU102 | Dual-DLPC900 EVM user's guide (hardware, connectors, jumpers). |
| `dlpc900-datasheet.pdf` | DLPS037H | Controller datasheet. Pixel-clock limits: **175 MHz single-pixel (§5.11), 141 MHz dual-pixel (§5.12)**; min blanking (§5.16). |
| `dlp670s-datasheet.pdf` | — | DMD datasheet. 2716×1600 array; pattern rates. |

Status-command register/bit definitions used by `device.py` come from DLPU018
§2.1 (Tables 2-2, 2-6, 2-8, 2-10).

## Design notes

- [`pattern-mode-design.md`](pattern-mode-design.md) — how we drive the DMD
  (multi-bit Video Pattern Mode, per-pattern sync, flat-field correction) and
  what the client still needs to implement.

## EDID

[`../edid-files/README.md`](../edid-files/README.md) — the EDID files for the DMD's HDMI/DP input,
which one is flashed, and how to reflash (TI's Windows tool + the J3 jumper).
