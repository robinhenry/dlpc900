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

Implemented: discovery/connect, `read`/`write` primitives, status
(hardware/system/main), identity (firmware/type), power mode get/set + reset,
operating mode get/set (incl. `VIDEO_PATTERN`), timing constants.

Implemented (minimal slice — DMD-master, free-running):
- `PatternEntry` / `PatternSequence` + LUT Definition encoding (`0x1A34`).
- `configure_pattern_sequence()` — stop → Definition ×N → Configuration (`0x1A31`).
- `set_trigger_out_1()` (`0x1A1D`) — the camera sync signal.
- `start_patterns()` / `pause_patterns()` / `stop_patterns()` (`0x1A24`).
- `LedSelect.NONE` default (external illumination).
- `set_it6535_power()` (`0x1A01`), `set_input_source()` (`0x1A00`).
- `set_gamma()` / `disable_gamma()` (`0x1A3B`).
- `set_port_clock()` (`0x1A03`) — see the board-specific note below.
- `prepare_video_pattern_mode()` — one-call setup: stop → receiver → input
  source → port/clock → gamma-off → VIDEO_PATTERN, in the required order.
  Defaults to dual-pixel. Follow with `configure_pattern_sequence()` →
  `set_trigger_out_1()` → `start_patterns()`.

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
   this client.*
3. **Port/clock** — `set_port_clock(DataPort.PORT12_DUAL)` → `0x1A03 = 0x02`
   (Dual Pixel, Port 1-2, **Clock 1** — Clock 2/3 are not wired on the EVM).
4. FPGA that splits data to both controllers ships programmed.

Over HDMI streaming the pattern rate is bandwidth-limited to ~native@60 →
~1,440 1-bit patterns/s (~180/s at 8-bit). The DLP670S peak (9,523 Hz, ~41 Gbit/s)
is only reachable in **pre-stored (flash) mode**, not HDMI — a different path.

### Is 2716×1600 @ 60 Hz (full-res dual-pixel) possible? YES.

Confirmed against DLPC900 datasheet (DLPS037H): single-pixel max clock 175 MHz
(§5.11), dual-pixel max 141 MHz (§5.12), min blanking 80 px H / ~370 µs V (§5.16).
2716×1600@60 with minimum reduced blanking ≈ 2796×1638×60 ≈ 274.8 Mpix/s →
**dual-pixel clock ≈ 137.4 MHz < 141 MHz limit** (~2.6% margin). The datasheet's
own dual-pixel example (1080p@120 = 138.4 MHz) is the same ballpark. Whole chain
clears: IT6535 input ~275 MHz < 340 (HDMI 1.4), cable ~6.6 Gbit/s < 10.2.

Requires: a **custom EDID** with reduced-blanking timing (that DTD as *preferred*,
fixed checksum) + `set_port_clock(DataPort.PORT12_DUAL)` (`0x02`). Thin margin →
must use near-minimum blanking and validate lock empirically (revert to the
backup EDID if it doesn't hold). Stock TI EDIDs are all single-pixel; none offer
this — it must be crafted.

### Stock EDID files (docs/edid/from-sw-bundle/)

- `LCR670S_HDMI_EDID.txt` = the factory EDID (= current): 2560×1600@30 +
  2716×1600@30, both single-pixel.

### Resolution / refresh options (decision reference)

| Option | Resolution | fps | 1-bit pat/s | 8-bit pat/s | Pixel mode | Effort |
|--|--|--|--|--|--|--|
| **A** (running now) | 2716×1600 | 30 | 720 | 90 | single | none — current EDID |
| **B** | 2048×1200 | 60 | 1440 | 180 | single | free reflash (`LCR670S_9000_..._HDMI`) |
| **C** ★ chosen target | 2716×1600 | 60 | 1440 | 180 | dual | custom EDID + Windows flash |

- **60 Hz alone is free** (B, single-pixel reduced-res). **C's exclusive gain** is
  full resolution *at* 60 Hz — vs B: same rate, **1.77× more micromirrors**; vs A:
  **2× rate**, same resolution.
- 8-bit is the flat-field/grayscale case (÷8 vs 1-bit); all rows sit under the
  DLP670S 8-bit min-exposure ceiling (~247/s), so they're HDMI-limited.
- **Decision:** target **C** when full-res-and-60 Hz is actually needed (and the
  optics resolve 2716×1600 at the sample). For now run **A** — zero effort. C is
  deferred (custom EDID + Windows flash); feasibility confirmed above (~137 MHz
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

## Live validation (deferred)

The pattern slice can't be meaningfully tested without the HDMI source feeding
bit-planes + a camera, and switching to VIDEO_PATTERN Mode disturbs device
state — so it's unit-tested only for now.

### ⚠️ UNRESOLVED: status reads during video-active modes intermittently hang the controller

**Symptom:** while in VIDEO or VIDEO_PATTERN mode, a status **read** sometimes
wedges the DLPC900's command interface — USB stays enumerated but every
subsequent read returns "no response". A fresh USB reconnect does not help; only
the **SW1 hardware reset** recovers (software reset can't — same hung path).

**It is intermittent.** The exact same sequence (`prepare_video_input` → read
`main_status` in VIDEO mode) *worked* on two early runs and *hung* on four later
ones. Because it is non-deterministic, three fixes were tried and **all failed**:
1. "Don't read during the VIDEO_PATTERN transition" (writes-only entry, Table 5-1) — still hung, on a VIDEO-mode read.
2. "Settle 0.5 s before the first read" — still hung.
3. "Pace commands 5 ms apart (like TI's batch files)" — still hung.

**Root cause = DLPC900 firmware, NOT the USB layer (confirmed).** Tested the same
wedge scenario (VIDEO mode + hammered `main_status` reads) over the **kernel
hidraw path** (raw `/dev/hidraw8`, bypassing hidapi/libusb) — **it wedged too**
(writes then time out, `errno 110`). So switching USB backend does not help. The
DLPC900 firmware (dlp670S_6.2.0) intermittently wedges when it gets **status
reads while processing video**; it is fine in PRE_STORED, and tolerates slow
sparse reads better than fast hammering. Matches the TI E2E thread
"**DLPC900: Video Pattern Mode Frozen**" (403 for WebFetch; open in a browser).

Ruled-out (wrong) theories, each disproved by a later hang or A/B test:
read-during-VIDEO_PATTERN-transition; settle-before-read; 5 ms command pacing;
libusb backend; **and the read-timeout (2 s→10 s) — A/B tested: reads are always
~11 ms so the timeout is never exercised, and 2 s survives 5/5 just like 10 s.**
The wedge is **genuinely intermittent** (identical configs, opposite outcomes:
4 early hangs, then 13 clean runs in a row). Root cause remains unknown; none of
the client-side changes provably fix it.

**Accepted design stance: treat VIDEO-mode status reads as unreliable and avoid
depending on them.** Enter modes via writes, wait blind, and confirm via
**external observation (scope on `TRIG_OUT_1`, camera)** — not USB status polling.
Reads are reliable only in PRE_STORED. Pacing + generous timeout are kept as
harmless hygiene, not as a fix.

**From the E2E thread (near-identical microscopy setup):**
- The TI **GUI polls status in video mode without hanging** → our command-hang is
  specific to our client's USB read protocol, NOT a fundamental limit. Likely
  differences to try: incrementing sequence numbers + response matching, drain
  stale responses on timeout, a much longer read timeout (waits are fine).
- **Pattern exposure must match the frame time.** At 30 Hz (≈33 ms), 3 patterns →
  **~11000 µs each** (not our 1500 µs, which is far too short and causes
  frozen/flicker behavior).
- **2716×1600 needs a special EDID that TI provides on request** (via E2E) — so
  Option C's EDID exists; request it rather than only crafting one.

**Design implication — go read-free while video-active.** The robust bring-up
does **no status reads** in VIDEO/VIDEO_PATTERN: prepare (writes) → *fixed blind
wait* for lock → configure + start (writes) → restore to PRE_STORED (writes) →
read only there. Confirmation of lock / `sequencer_running` must come from
**external observation** (scope on `TRIG_OUT_1`, camera), not USB polling.
**5 SW1 resets — stop hardware retries until we have the E2E answer or a scope.**

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
- Rework `prepare_video_pattern_mode()` — its straight jump to VIDEO_PATTERN is
  too aggressive.
- Always have a **hardware recovery plan (SW1 / power-cycle)** ready before any
  VIDEO_PATTERN test.

### Bring-up test (once the rig + safe sequence are ready)

GPU rendering bit-plane frames + camera + scope on `TRIG_OUT_1`; enter
VIDEO_PATTERN via the safe sequence, `configure_pattern_sequence()`,
`set_trigger_out_1()`, `start_patterns()`, confirm `sequencer_running` and scope
the triggers against camera frames.
