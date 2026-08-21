# Pattern mode — design notes

Design decisions for driving the DMD in our microscope. Design-only; captures
the plan agreed before implementation. References are to the DLPC900
Programmer's Guide (DLPU018J) in this folder.

## Goal

Structured illumination: high-speed patterns streamed from the GPU, each
**synced 1:1 to a camera capture**, with **flattened illumination intensity**
across the field.

## Chosen approach: multi-bit Video Pattern Mode

Video Pattern Mode (operating mode `2`, §2.4.1) is the only mode that gives us
both requirements at once:

- **Per-pattern camera sync** — each LUT entry fires a `TRIG_OUT` pulse, so the
  camera captures exactly that pattern.
- **Flattened intensity** — patterns are **grayscale (N-bit)**, not binary, so
  the flat-field correction is applied via PWM duty cycle.

**Bit depth is the speed↔flatness knob.** Each 24-bit HDMI frame carries 24
bit-planes; an N-bit pattern consumes N of them, so patterns/frame = 24/N. Full
8-bit ≈ 180 patterns/s @ 60 Hz HDMI; 4–6 bit is faster and usually flattens
fine. Higher HDMI refresh scales all rates up.

Rejected alternatives: **Video Mode** (no per-pattern sync), **Pre-Stored**
(needs flashing), **Pattern On-The-Fly** (USB-bandwidth-limited).

## HDMI vs USB — two planes

- **HDMI (data plane):** the pattern *pixels*, as bit-planes packed into 24-bit
  RGB frames from the GPU → IT6535 receiver → DLPC900.
- **USB (control plane, this client):** the *schedule* — mode, input config,
  gamma, the Pattern Display LUT (order/timing/triggers), start/stop.

The LUT does **not** contain image data; each entry points at a bit-plane of the
incoming HDMI frame and says how long to show it, at what bit depth, with what
trigger.

## Division of labor

| Layer | Responsibility |
|--|--|
| Optics + calibration (once) | Measure illumination profile → per-pixel correction mask |
| GPU (per frame) | Render `pattern × mask` into packed bit-planes, output over HDMI at native resolution |
| This client (USB) | Mode, input config, gamma-linear, pattern LUT + triggers + start/stop |
| HDMI | Carries pattern pixels |

The client never holds the mask or pushes pixels — that is the GPU's job.

## Flat-field correction

- The DMD shows **one image at a time** — there is no hardware "correction
  layer." Correction is a per-pixel **multiplicative mask** combined with each
  pattern **in the GPU** (`sent = pattern × mask`), every frame.
- Requires grayscale (PWM) → hence multi-bit patterns, not pure binary.
- The mask is **calibrated once**; applying it is a per-frame GPU multiply.

## Gamma

Disable gamma (command `0x1A3B`, byte 0 bit 0 = 0) so `value → duty cycle` is
linear and predictable for the mask math. Equivalent to enabling the Linear
(γ=1.0) table, but simpler and less state. Supported on our firmware (6.2.0).

## Client status

The code is the authoritative list: `src/dmd_client/device.py` for command
coverage, `README.md` for the CLI surface. Mirroring it here is what went stale
last time, so only the gaps that matter to *this* design are tracked:

**Not implemented:**
- `TRIG_OUT_1` configuration (`0x1A1D`) — the camera sync signal this whole
  design rests on. `run_pattern_sequence()` is free-running today, so pattern
  display is not yet tied to camera capture.
- System status decode (`0x1A0B`). Hardware (`0x1A0A`) and main (`0x1A0C`,
  incl. the sync-lock bits) are decoded.
- Anything that pushes pixels — by design, that is the GPU's job.

**Port/clock routing (`0x1A03`).** Board- and video-timing-specific (single- vs
dual-pixel depends on the HDMI pixel clock). **Validated on our board: `0x00`
(port 1, single pixel) — the GPU's HDMI signal locks with this value**
(`external_source_locked=True`, `port1_syncs_valid=True`) in VIDEO mode, so no
change is needed for the current timing. If a future higher-resolution timing
needs dual-pixel, capture the value by configuring the working mode and reading
`0x1A03` back.

**HDMI lock is only observable in a mode that consumes the parallel input**
(VIDEO / VIDEO_PATTERN). In PRE_STORED_PATTERN the sync bits stay false because
patterns come from flash, not HDMI.

### Dual-pixel (full-resolution) path

DMD is **DLP670S: 2716×1600** (4.35 M mirrors). Native @ 60 Hz needs a pixel rate
(~260 Mpixel/s) above the **single-pixel limit (~175 MHz)**, so full resolution
**requires dual-pixel** (single-pixel caps native at ~40 Hz or forces a lower
resolution — which is why our current lock is single-pixel).

To enable dual-pixel:
1. **Cable** — native@60 ≈ ~8 Gbit/s (less than 4K UHD's ~12.5), so a genuine 4K
   HDMI cable suffices; DisplayPort or Premium-High-Speed HDMI for margin.
2. **EDID** — program the EVM to advertise the native timing so the GPU outputs
   it (DLPU102 §3.13; GUI batch file + jumper J3/HDMI or J4/DP). *Not doable from
   this client.* TI supplied the DP dual-pixel 60 Hz file
   (`edid-files/LCR9000_DP_EDID_2716_60Hz.txt`); it is flashed and working.
3. **Port/clock** — `PORT12_DUAL` (`0x1A03 = 0x02`) is required for the DP
   timing, confirmed on the mirrors: at 269.89 Mpix/s the IT6535 cannot clock a
   single-pixel bus (~270 MHz, over the 175 MHz limit), so it emits two pixels
   per clock at ~135 MHz and the DLPC900 must consume both. `dmd stream` sets
   the routing per source (DP dual, HDMI single) — it follows from the timing,
   so it is not exposed as an option; the other value is simply broken.

   **Two traps here, both cost us time.** (a) `port2_syncs_valid` reads False
   even under a confirmed-working dual-pixel link — parallel-port status comes
   from the primary controller only (DLPU102 §3.7) — so the sync bits cannot
   tell you whether the routing is right; **only the image can**. The wrong
   routing shows as a half-width, centered picture, never as a lock failure.
   (b) A changed `data_port` does not take effect while the source is already
   locked: the controller keeps its latched configuration and reports "locked in
   0.0 s". `prepare_video_input()` now powers the IT6535 down for 0.5 s first to
   force a real re-acquisition (~2.2 s). Powering it straight back up with no
   pause leaves the link down indefinitely.
4. FPGA that splits data to both controllers ships programmed.

Over HDMI streaming the pattern rate is bandwidth-limited to ~native@60 →
~1,440 1-bit patterns/s (~180/s at 8-bit). The DLP670S peak (9,523 Hz, ~41 Gbit/s)
is only reachable in **pre-stored (flash) mode**, not HDMI — a different path.

### Is 2716×1600 @ 60 Hz (full-res) possible? YES — confirmed on hardware.

Confirmed against DLPC900 datasheet (DLPS037H): single-pixel max clock 175 MHz
(§5.11), dual-pixel max 141 MHz (§5.12), min blanking 80 px H / ~370 µs V (§5.16).
2716×1600@60 with minimum reduced blanking ≈ 2796×1638×60 ≈ 274.8 Mpix/s →
**dual-pixel clock ≈ 137.4 MHz < 141 MHz limit** (~2.6% margin). The datasheet's
own dual-pixel example (1080p@120 = 138.4 MHz) is the same ballpark. Whole chain
clears: IT6535 input ~275 MHz < 340 (HDMI 1.4), cable ~6.6 Gbit/s < 10.2.

Requires: a reduced-blanking EDID (that DTD as *preferred*, fixed checksum) +
`set_port_clock(DataPort.PORT12_DUAL)` (`0x02`). Thin margin → validate lock
empirically (revert to `LCR670S_HDMI_EDID.txt` if it doesn't hold). No crafting
needed: TI supplied the file via E2E — `LCR9000_DP_EDID_2716_60Hz.txt`,
DisplayPort, untested.

### EDID files

See [`../edid-files/README.md`](../edid-files/README.md) for the full table. Both LCR9000 EDIDs are
flashed (separate PROMs, J3 = HDMI, J4 = DP) and verified live by re-dumping the
host-side EDID:

| Input | EDID | Timing | Status |
|--|--|--|--|
| HDMI | `LCR9000_HDMI_EDID_2716_30Hz.txt` | 2716×1600@29.95, 139.8 MHz | working |
| DP | `LCR9000_DP_EDID_2716_60Hz.txt` | 2716×1600@59.92, 269.89 MHz | **working — full-screen video confirmed on the DMD** |

Both replaced the factory 165 MHz timing (348 px / 292 line blanking), which is
the one that froze Video Pattern Mode on the first microscope.

Flashing is TI's Windows GUI, DLPU102 §3.13 — the HDMI and DP procedures differ
(different jumpers, different cable states). Always confirm a flash landed by
re-dumping the host-side EDID rather than trusting the tool: bytes 0x36-0x37 of
the EDID are the pixel clock, little-endian, in units of 10 kHz.

```bash
xxd -p /sys/class/drm/card*-DP-*/edid    # 0x696D = 269.89 MHz (DP)
                                          # 0x369C = 139.80 MHz (HDMI)
                                          # 165 MHz = still the factory EDID
```

### Resolution / refresh options (decision reference)

| Option | Resolution | fps | 1-bit pat/s | 8-bit pat/s | Pixel mode | Effort |
|--|--|--|--|--|--|--|
| **A** | 2716×1600 | 30 | 720 | 90 | single | HDMI, EDID flashed — works |
| **B** | 2048×1200 | 60 | 1440 | 180 | single | superseded by C |
| **C** ★ **running now** | 2716×1600 | 60 | 1440 | 180 | single per controller | DP, EDID flashed — works |

- **60 Hz alone is free** (B, single-pixel reduced-res). **C's exclusive gain** is
  full resolution *at* 60 Hz — vs B: same rate, **1.77× more micromirrors**; vs A:
  **2× rate**, same resolution.
- 8-bit is the flat-field/grayscale case (÷8 vs 1-bit); all rows sit under the
  DLP670S 8-bit min-exposure ceiling (~247/s), so they're HDMI-limited.
- **Decision: C is achieved** — TI's DP EDID flashed, `dmd stream` locks, and
  full-screen video renders correctly across the array. A (HDMI/30 Hz) remains
  available as a fallback on its own PROM. Feasibility was predicted above (~137 MHz
  dual-pixel < 141 limit).

Still needed:
1. `TRIG_IN` support (`0x1A35`/`0x1A36`) + the triggered-pattern dark-time
   workaround — only if we move off free-running.
2. Exposure/dark-time validation against minimum exposure times (Table 2-109).
3. Live validation once the HDMI source + camera are wired (see below).

## Out of scope

LED control, DMD park (TI-deprecated), Pre-Stored & On-The-Fly modes, image
orientation / optical-invert (defer to calibration if ever needed),
DLPA200/DMD-comm faults (rejected by our firmware). Flat-field pixel math lives
in the GPU, not here.

## Trigger topology (DMD-master, free-running)

The mirrors must be settled before illuminating/capturing, and only the DMD
knows when — so the DMD is the timing master.

```
DMD TRIG_OUT_1  ──▶  Camera TRIG_IN       (camera exposes during valid pattern)
Camera strobe   ──▶  LED TRIG_IN          (LED on only during integration)
DMD TRIG_IN_1/2 ──   unconnected          (free-running; start via USB)
DMD TRIG_OUT_2  ──   spare
```

Order per pattern: mirrors flip → settle → `TRIG_OUT_1` high → camera exposes +
LED on → next. Use `set_trigger_out_1(rising_delay_us=...)` to push the camera
trigger past mirror settle. Bring-up tip: leave the LED continuously on first,
prove DMD→camera sync, then add strobe→LED gating.

## Internal test pattern (`dmd show`)

The DLPC900 draws these itself (`0x1203`), so they need no HDMI, GPU, LUT or
camera sync — the alignment reference. **Order matters:** VIDEO mode *first*,
then input source -> test-pattern generator, then the pattern. Two reasons: the
`0x1A1B` write re-initializes the video pipeline, so selecting the source first
lets it fall back to the parallel input (you get the HDMI image, not the
pattern); and per DLPU018 Table A-2 the pattern-select commands are not accepted
in VIDEO_PATTERN or the flash pattern modes, which is where an acquisition run
leaves the controller.

`dmd show black` is a zeroed solid field, not pattern value 10 ("no pattern").
Value 10 idles the generator rather than clearing the array, so the mirrors keep
holding the last image — it reads as a no-op, and is deliberately not exposed.

## Live validation (deferred)

The pattern slice can't be meaningfully tested without the HDMI source feeding
bit-planes + a camera, and switching to VIDEO_PATTERN Mode disturbs device
state — so it's unit-tested only for now.

### Status reads during video-active modes — resolved for VIDEO, open for VIDEO_PATTERN

**History.** Status **reads** in VIDEO or VIDEO_PATTERN mode used to wedge the
DLPC900's command interface: USB stayed enumerated but every subsequent read
returned "no response", and only the **SW1 hardware reset** recovered it (a
software reset uses the same hung path). It hit 4 times early, then 13 runs in a
row came back clean with no fix applied — so it was never deterministic, and
three attempted fixes (writes-only mode entry, a 0.5 s settle before the first
read, 5 ms command pacing) each "worked" only until it hung again. It was
reproduced over the **kernel hidraw path** with hidapi/libusb bypassed, which
placed the fault in the DLPC900 firmware rather than our USB layer. Ruled out by
A/B test: read timeout length (reads answer in ms, so it is never exercised).

**Status now: does not reproduce in VIDEO mode.** Retested after flashing the
reduced-blanking EDID (`LCR9000_HDMI_EDID_2716_30Hz.txt`, 139.8 MHz):

| Phase | Reads | Rate | Result |
|--|--|--|--|
| Paced, via `device.read()` | 7,145 over 5 fresh VIDEO entries | ~48/s | clean |
| Unpaced, raw HID framing | 86,277 in 180 s | 479/s | clean |

**93,422 reads across 6 VIDEO-mode entries, zero wedges**, worst latency 3.8 ms.
The old "tolerates sparse reads better than fast hammering" observation did not
reproduce either. Note the causal story is still unproven — the wedge was
reproduced below the level EDID influences, so "the EDID fixed it" is a
hypothesis. The evidence is strong against a *per-read* hazard and weak against a
*per-entry* one (only 6 entries).

**Practical stance:**
- **VIDEO mode — polling is fine.** `wait_for_video_lock()` polls main status
  (`0x1A0C`) until the source locks, and `dmd stream` reads the mode back to
  confirm it is streaming. A forced re-acquisition locks in **~2.2 s** (0.8 s if
  the link is merely re-entering VIDEO mode), against 3.6 s on the factory
  timing.
- **VIDEO_PATTERN mode — still read-free.** Nothing above covers it, and the
  original hang lived there (a `0x1A1B` read right after the transition). Enter
  by writes, and confirm via **external observation** (scope on `TRIG_OUT_1`,
  camera) until that path is retested.
- **Power Mode (`0x0200`) — never read soon after it.** Two SW1 resets on
  2026-08-21 pinned this down. Both wedges followed Return to Normal
  (`0x0200 = 0`) with reads a short time later: first a 0.2 s poll, then — after
  "fixing" it — a 0.5 s settle plus sparse single reads. The second wedge
  happened with the DMD **already awake**, so Return to Normal is *not* the
  harmless no-op it looks like; it appears to kick an internal re-init, and
  reads during that hang the command interface. Only the version that blind-waits
  `WAKE_SETTLE_S` (5 s) and then reads *once* has ever worked.

  What this does **not** mean is "reads are dangerous". The 93k-read soak above,
  and `dmd stream`'s own lock poll right after writing `0x1A1B = VIDEO`, are both
  clean across dozens of runs. The hazard is specific: reads shortly after a
  *power-mode* command, plus the original `0x1A1B` read right after entering
  VIDEO_PATTERN.

  Consequence for the CLI: waking is an explicit `dmd wake`, never folded into
  `dmd show` / `dmd stream`. Those commands must not touch `0x0200` at all — a
  unit test enforces it. The safest resume path avoids the command entirely:
  `dmd standby` parks the mirrors (writes only), then power-cycle; the controller
  boots clean to OOB.
- Keep a hardware recovery plan (SW1 / power-cycle) available before testing the
  pattern path. Reads remain fully reliable in PRE_STORED.

**From the TI E2E thread (near-identical microscopy setup):**
- The TI **GUI polls status in video mode without hanging** — consistent with the
  wedge being situational rather than a fundamental limit.
- **Pattern exposure must match the frame time.** At 30 Hz (≈33 ms), 3 patterns →
  **~11000 µs each** (not 1500 µs, which is far too short and causes the
  frozen/flicker behavior). This is what `PatternSequence.frame_filling()` does.
- **2716×1600 needs a special EDID** — TI supplied it; see `edid-files/README.md`.

### (original) VIDEO_PATTERN entry can hang the controller

A first live attempt hung the DLPC900's command interface: sequence was set
input → VIDEO mode (**locked in 3.6 s** — input path works) → set VIDEO_PATTERN
→ wait 500 ms → **read `0x1A1B`** → all reads then returned "no response". USB
stayed enumerated but the firmware stopped answering commands; a software reset
(`0x0200=2`) could not recover it (same hung command path). **Recovery required a
hardware reset (SW1 button).** After SW1 it booted clean to OOB.

Guardrails for the next attempt:
- **Do not read status during/right after the VIDEO_PATTERN transition** (the
  guide warns the mode read is unreliable for ~300 ms; in practice the read hung
  the firmware). Configure the LUT and start the sequence with writes only, and
  read back only once it's running.
- The correct entry order (VIDEO → sync-lock → VIDEO_PATTERN → LUT → start) needs
  verifying against a known-good reference (GUI/E2E) before retrying.
- ~~Rework `prepare_video_pattern_mode()` — its straight jump to VIDEO_PATTERN is
  too aggressive.~~ **Done:** it is now `prepare_video_input()`, which stops at
  VIDEO mode; `run_pattern_sequence()` does the VIDEO → lock wait →
  VIDEO_PATTERN → LUT → start progression.
- Always have a **hardware recovery plan (SW1 / power-cycle)** ready before any
  VIDEO_PATTERN test.

### Bring-up test (once the rig + safe sequence are ready)

GPU rendering bit-plane frames + camera + scope on `TRIG_OUT_1`; enter
VIDEO_PATTERN via the safe sequence, `configure_pattern_sequence()`,
`set_trigger_out_1()`, `start_patterns()`, confirm `sequencer_running` and scope
the triggers against camera frames.
