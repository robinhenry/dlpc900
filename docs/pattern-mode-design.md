# Design notes

How this client drives the DLPC900 for structured illumination in a microscope: the approach, what has been validated on hardware, the controller behaviours that shaped the code, and what remains. Section references are to the TI documents in this folder: the DLPC900 Programmer's Guide (DLPU018J), the Dual DLPC900 EVM guide (DLPU102) and the DLPC900 datasheet (DLPS037H).

The board used here is a Dual DLPC900 EVM driving a DLP670S DMD (2716×1600, 4.35 M mirrors) on application firmware 6.2.0. The code is the authoritative list of what is implemented: `src/dlpc900/device.py` for the command set, `README.md` for the CLI.

## Goal and approach

The goal is structured illumination: high-speed patterns streamed from the GPU, each **synced 1:1 to a camera capture**, with **flattened illumination intensity** across the field.

The chosen approach is **multi-bit Video Pattern Mode** (operating mode `2`, DLPU018 §2.4.1), the only mode that gives both requirements at once:

- **Per-pattern camera sync.** Each LUT entry fires a `TRIG_OUT` pulse, so the camera captures exactly that pattern.
- **Flattened intensity.** Patterns are grayscale (N-bit), not binary, so the flat-field correction is applied via PWM duty cycle.

**Bit depth is the speed-versus-flatness knob.** Each 24-bit video frame carries 24 bit-planes; an N-bit pattern consumes N of them, so a frame carries 24/N patterns. At the 60 Hz DisplayPort timing that is 180 patterns/s at 8-bit and 1,440/s at 1-bit; the 30 Hz HDMI timing halves both. 8-bit is the flat-field grayscale case; 4 to 6 bits is faster and usually flattens well enough.

Rejected alternatives: **Video Mode** (no per-pattern sync), **Pre-Stored Pattern** (patterns must be flashed), **Pattern On-The-Fly** (limited by USB bandwidth).

**Where things stand.** Plain VIDEO mode (`dmd stream`) is validated end to end and is the precondition for Video Pattern Mode. The Video Pattern Mode path (`run_pattern_sequence()`, the LUT encoding, the frame-filling exposure rule) is implemented in the library and unit-tested, but it is not exposed in the CLI and has not yet been validated live with a camera. See [Planned work](#planned-work).

## Data plane and control plane

- **Video link (data plane).** The pattern *pixels*, as bit-planes packed into 24-bit RGB frames: GPU → IT6535 receiver → DLPC900, over HDMI or DisplayPort.
- **USB (control plane, this client).** The *schedule*: operating mode, input configuration, gamma, the Pattern Display LUT (order, timing, triggers), start and stop.

The LUT does **not** contain image data. Each entry points at a bit-plane of the incoming frame and says how long to show it, at what bit depth, with what trigger.

| Layer | Responsibility |
|--|--|
| Optics + calibration (once) | Measure the illumination profile → per-pixel correction mask |
| GPU (per frame) | Render `pattern × mask` into packed bit-planes, output at native resolution |
| This client (USB) | Mode, input config, gamma off, pattern LUT, triggers, start/stop |
| Video link | Carries the pattern pixels |

### Flat-field correction

The DMD shows one image at a time; there is no hardware correction layer. Correction is a per-pixel **multiplicative mask** applied to each pattern **in the GPU** (`sent = pattern × mask`), every frame. It requires grayscale (PWM), hence multi-bit patterns rather than binary. The mask is calibrated once; applying it is a per-frame GPU multiply. The client never holds the mask or pushes pixels.

### Gamma

Gamma is disabled (`0x1A3B`, byte 0 bit 0 = 0) so that pixel value maps linearly to duty cycle, which the mask arithmetic depends on. This is equivalent to selecting the linear (γ = 1.0) table but carries less state. Confirmed accepted on firmware 6.2.0.

## Video input

### EDIDs

The IT6535 receiver advertises its supported timing to the GPU through an EDID, one PROM each for HDMI and DisplayPort. The factory HDMI EDID advertises a 165 MHz timing with generous blanking (348 px horizontal, 292 lines vertical); that timing is the one that froze Video Pattern Mode in early testing, and both PROMs have been reflashed with reduced-blanking native-resolution timings supplied by TI. [`../edid-files/README.md`](../edid-files/README.md) has the full table.

| Input | EDID | Timing | Status |
|--|--|--|--|
| HDMI | `LCR9000_HDMI_EDID_2716_30Hz.txt` | 2716×1600 @ 29.95 Hz, 139.8 MHz | working |
| DP | `LCR9000_DP_EDID_2716_60Hz.txt` | 2716×1600 @ 59.92 Hz, 269.89 MHz | working, full-screen video confirmed on the DMD |

Flashing uses TI's Windows GUI (DLPU102 §3.13, a batch file plus jumper J3 for HDMI or J4 for DP); the HDMI and DP procedures differ in jumpers and cable state. It cannot be done from this client. Always confirm a flash landed by re-dumping the host-side EDID rather than trusting the tool. Bytes 0x36 and 0x37 are the pixel clock, little-endian, in units of 10 kHz:

```bash
xxd -p /sys/class/drm/card*-DP-*/edid    # 0x696D = 269.89 MHz (DP)
                                          # 0x369C = 139.80 MHz (HDMI)
                                          # 165 MHz = still the factory EDID
```

### Resolution and refresh options

| Option | Resolution | Hz | 1-bit pat/s | 8-bit pat/s | Pixel mode | Status |
|--|--|--|--|--|--|--|
| A | 2716×1600 | 30 | 720 | 90 | single | HDMI. Working; kept as a fallback on its own PROM |
| B | 2048×1200 | 60 | 1,440 | 180 | single | Not pursued; C gives the same rate at full resolution |
| C | 2716×1600 | 60 | 1,440 | 180 | dual | DisplayPort. **In use** (`dmd stream` default) |

C's gain over A is twice the pattern rate at the same resolution; over B it is 1.77× more mirrors at the same rate. All rows sit under the DLP670S 8-bit minimum-exposure ceiling (~247/s), so they are link-limited, not DMD-limited.

Feasibility of C against the DLPC900 datasheet (DLPS037H): single-pixel maximum clock 175 MHz (§5.11), dual-pixel maximum 141 MHz (§5.12), minimum blanking 80 px horizontal and ~370 µs vertical (§5.16). 2716×1600 @ 60 Hz with minimum reduced blanking is roughly 2796×1638×60 ≈ 274.8 Mpixel/s, a dual-pixel clock of ~137.4 MHz against the 141 MHz limit (~2.6 % margin). The datasheet's own dual-pixel example (1080p @ 120 Hz, 138.4 MHz) is the same ballpark. The rest of the chain clears easily: ~275 MHz into the IT6535 against 340 MHz for HDMI 1.4, and ~6.6 Gbit/s on the cable against 10.2. The thin margin held in practice.

Over a streamed video link the pattern rate is bounded by the link, roughly native resolution at 60 Hz, so ~1,440 1-bit patterns/s. The DLP670S peak of 9,523 Hz (~41 Gbit/s) is reachable only in pre-stored (flash) mode, a different path entirely.

### Port and clock routing

The parallel-port routing (`0x1A03`, `set_port_clock()`) is a property of the video timing, not a choice. HDMI at 139.8 MHz is under the 175 MHz single-pixel limit, so the IT6535 delivers one pixel per clock and the routing is `PORT1_SINGLE` (`0x00`, the reset value). DisplayPort at 269.89 Mpixel/s is over that limit, so the IT6535 emits two pixels per clock at ~135 MHz and the DLPC900 must consume both: `PORT12_DUAL` (`0x02`). `dmd stream` sets the routing per source and does not expose it as an option, because the other value is simply broken for that timing. On the dual-controller EVM, the FPGA that splits data to both controllers ships programmed.

**Two traps, both confirmed on hardware:**

- **The sync bits cannot tell you whether the routing is right; only the image can.** `port2_syncs_valid` reads False even under a confirmed-working dual-pixel link, because parallel-port status comes from the primary controller only (DLPU102 §3.7). The wrong routing shows as a half-width, centred picture, never as a lock failure.
- **A changed routing does not take effect while the source is already locked.** The controller keeps its latched configuration and reports "locked in 0.0 s". `prepare_video_input()` therefore powers the IT6535 down for 0.5 s before reconfiguring, which forces a real re-acquisition (~2.2 s). Powering it straight back up with no pause leaves the link down indefinitely. The host sees the drop as a monitor unplug, so it is done only when the routing actually changes.

Video lock is observable only in a mode that consumes the parallel input (VIDEO or VIDEO_PATTERN). In PRE_STORED_PATTERN the sync bits stay false because patterns come from flash.

## Controller hazards

The DLPC900's command interface can wedge: USB stays enumerated but every subsequent read returns no response. Only the **SW1 hardware reset** or a power-cycle recovers it; a software reset (`0x0200 = 2`) travels the same hung path and does not. Everything in this section exists to keep the client off the paths that wedge it. The full chronology is in [History](#history).

### Status reads in video modes

**VIDEO mode: polling is fine.** After the reduced-blanking EDID was flashed, 93,422 status reads across six fresh VIDEO-mode entries produced zero wedges, with a worst-case latency of 3.8 ms, both paced through `device.read()` (~48/s) and unpaced with raw HID framing (479/s). `wait_for_video_lock()` polls main status (`0x1A0C`) until the source locks, and `dmd stream` reads the mode back to confirm it is streaming. A forced re-acquisition locks in ~2.2 s (0.8 s if the link is merely re-entering VIDEO mode), against 3.6 s on the factory timing.

The causal story is unproven. The original wedge was reproduced over the kernel hidraw path with hidapi and libusb bypassed, which places the fault in the controller firmware below anything the EDID should influence, so "the EDID fixed it" is a hypothesis. The evidence is strong against a *per-read* hazard and weaker against a *per-entry* one, since only six entries were soaked.

**VIDEO_PATTERN mode: still read-free.** None of the above covers it, and the original hang lived there (a `0x1A1B` mode read right after the transition; the guide itself warns that read is unreliable for ~300 ms). `configure_pattern_sequence()` and `start_patterns()` are writes only. Confirm the sequence is running by external observation (a scope on `TRIG_OUT_1`, the camera) until that path is retested with a recovery plan at hand.

Reads remain fully reliable in PRE_STORED_PATTERN, the power-on state.

### The power-mode command

**Never read soon after writing Power Mode (`0x0200`).** Two SW1 resets on 2026-08-21 pinned this down. Both wedges followed Return to Normal (`0x0200 = 0`) with reads a short time later: first a 0.2 s poll, then, after "fixing" it, a 0.5 s settle plus sparse single reads. The second wedge happened with the DMD **already awake**, so Return to Normal is not the harmless no-op it looks like; it appears to kick an internal re-init, and reads during that hang the command interface. Only the version that blind-waits `WAKE_SETTLE_S` (5 s) and then reads *once* has ever worked.

This does **not** mean reads are dangerous in general. The soak above, and `dmd stream`'s own lock poll immediately after writing `0x1A1B = VIDEO`, are clean across dozens of runs. The hazard is specific: reads shortly after a power-mode command, plus the `0x1A1B` read right after entering VIDEO_PATTERN.

Consequences for the CLI: waking is an explicit `dmd wake`, never folded into `dmd show` or `dmd stream`, and those commands must not touch `0x0200` at all (a unit test enforces this). While in Standby the guide says Return to Normal is the only accepted command, so `dmd standby` sends its write and reads nothing back. The safest resume path avoids the command entirely: `dmd standby` parks the mirrors, then power-cycle, and the controller boots clean to its out-of-box state.

### Parking and standby

Standby (`0x0200 = 1`) parks the micromirrors, disables the input interfaces and powers down part of the DMD (DLPU018 §2.3.1.1). Parking is what protects the array when it is idle, and TI requires it before a planned power-off. The park precondition takes about two minutes, during which nothing may be sent; `dmd standby` waits that out locally with a countdown. The standalone DMD Park command is deprecated by TI and is not used; parking happens only through Standby.

## Internal test pattern

The DLPC900 draws its own test patterns (`0x1203`), so `dmd show` needs no video link, GPU, LUT or camera; it is the fixed reference for optical alignment.

**Order matters:** enter VIDEO mode first, then select the test-pattern generator as the input source, then the pattern. Two reasons. The `0x1A1B` mode write re-initialises the video pipeline, so selecting the source first lets it fall back to the parallel input and you get the video image instead of the pattern. And per DLPU018 Table A-2 the pattern-select commands are not accepted in VIDEO_PATTERN or the flash pattern modes, which is where an acquisition run leaves the controller.

`dmd show black` is a zeroed solid field, not pattern value 10 ("no pattern"). Value 10 idles the generator rather than clearing the array, so the mirrors keep holding the last image; it reads as a no-op and is deliberately not exposed.

## Planned work

### Trigger topology (DMD as master, free-running)

The mirrors must be settled before illuminating and capturing, and only the DMD knows when, so the DMD is the timing master:

```
DMD TRIG_OUT_1  ──▶  Camera TRIG_IN       (camera exposes during a valid pattern)
Camera strobe   ──▶  LED TRIG_IN          (LED on only during integration)
DMD TRIG_IN_1/2 ──   unconnected          (free-running; started over USB)
DMD TRIG_OUT_2  ──   spare
```

Order per pattern: mirrors flip → settle → `TRIG_OUT_1` high → camera exposes with the LED on → next pattern. The `TRIG_OUT_1` rising-edge delay should be set to push the camera trigger past mirror settle. Bring-up tip: leave the LED continuously on first, prove the DMD-to-camera sync, then add the strobe-to-LED gating.

### Frame-filling exposures

In Video Pattern Mode the sequencer must consume incoming frames at the rate they arrive. The LUT's total on-time has to equal the frame period, or the sequencer drifts against the video and the controller flickers or freezes. A TI E2E forum thread on a near-identical microscopy setup confirms this: at 30 Hz (≈33 ms), three 8-bit patterns need ~11,000 µs each, not the ~1,500 µs that produced the frozen display. `PatternSequence.frame_filling()` computes exposures this way, folding the rounding remainder into the last entry. The same thread reported that the TI GUI polls status in video mode without hanging, consistent with the wedge being situational rather than fundamental, and supplied the native-resolution EDIDs.

### Not yet implemented

- **`TRIG_OUT_1` configuration (`0x1A1D`).** The camera-sync signal the whole design rests on. `run_pattern_sequence()` is free-running today, so pattern display is not yet tied to camera capture.
- **`TRIG_IN` support (`0x1A35`, `0x1A36`)** and the triggered-pattern dark-time workaround, only if the design moves off free-running.
- **Exposure and dark-time validation** against the minimum exposure times (DLPU018 Table 2-109).
- **System status decode (`0x1A0B`).** Hardware (`0x1A0A`) and main (`0x1A0C`, including the sync-lock bits) are decoded.
- **A `dmd` subcommand for Video Pattern Mode**, sharing the DP/HDMI source handling with `dmd stream`.
- **Live validation.** GPU rendering bit-plane frames, a camera, and a scope on `TRIG_OUT_1`: enter VIDEO_PATTERN via `run_pattern_sequence()`, configure the trigger, start, confirm `sequencer_running`, and scope the triggers against camera frames. Keep SW1 and the power switch within reach.

Anything that pushes pixels is, by design, the GPU's job and will not be added here.

## Out of scope

LED control, Pre-Stored and On-The-Fly pattern modes, image orientation and optical inversion (deferred to calibration if ever needed), DLPA200 and DMD-communication fault decoding (rejected by this firmware), and the flat-field pixel arithmetic, which lives in the GPU.

## History

A dated log of the findings that shaped the sections above, kept so the reasoning can be re-checked.

**First live VIDEO_PATTERN attempt: the original hang.** Sequence: set input → VIDEO mode (locked in 3.6 s on the factory EDID, so the input path worked) → set VIDEO_PATTERN → wait 500 ms → read `0x1A1B` → every subsequent read returned no response. USB stayed enumerated but the firmware stopped answering. A software reset (`0x0200 = 2`) could not recover it. SW1 did, and the controller booted clean to out-of-box. The code at the time jumped straight to VIDEO_PATTERN; it was reworked into `prepare_video_input()`, which stops at VIDEO mode, plus `run_pattern_sequence()`, which does the VIDEO → lock wait → VIDEO_PATTERN → LUT → start progression with writes only.

**Early wedges were non-deterministic.** Status reads in VIDEO or VIDEO_PATTERN mode wedged the controller four times early on, then thirteen runs in a row came back clean with no fix applied. Three attempted fixes (writes-only mode entry, a 0.5 s settle before the first read, 5 ms command pacing) each "worked" only until the next hang. The wedge was reproduced over the kernel hidraw path with hidapi and libusb bypassed, placing the fault in the DLPC900 firmware rather than the host USB layer. Read-timeout length was ruled out by A/B test: reads answer in milliseconds, so the timeout is never exercised.

**Factory EDID froze Video Pattern Mode.** The 165 MHz factory HDMI timing with 348 px / 292 line blanking was the one in use when Video Pattern Mode froze. Both PROMs were reflashed with TI's reduced-blanking native-resolution files and verified by re-dumping the host-side EDID.

**Soak test after the reduced-blanking HDMI EDID (139.8 MHz).**

| Phase | Reads | Rate | Result |
|--|--|--|--|
| Paced, via `device.read()` | 7,145 over 5 fresh VIDEO entries | ~48/s | clean |
| Unpaced, raw HID framing | 86,277 in 180 s | 479/s | clean |

93,422 reads across six VIDEO-mode entries, zero wedges, worst latency 3.8 ms. The earlier impression that the controller "tolerates sparse reads better than fast hammering" did not reproduce either.

**2026-08-21: two SW1 resets traced to the power-mode command.** Described in full under [The power-mode command](#the-power-mode-command). This is what made `dmd wake` a separate, blind-waiting command and kept `0x0200` out of every other code path.

**DisplayPort at 60 Hz confirmed.** With TI's DP EDID flashed and `PORT12_DUAL` routing, `dmd stream` locks and full-screen video renders correctly across the array, matching the ~137 MHz dual-pixel prediction. The half-width centred image under the wrong routing, and the latched-routing trap, were both found during this bring-up.
