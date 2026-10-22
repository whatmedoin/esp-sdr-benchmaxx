# ESP32-C6 FFT optimization

## Summary

Increase C6 FFT throughput by processing more complete windows before RF-bank reclamation and replacing software floating-point mean accumulation with exact integer sums. Controlled mean throughput improved by 32-67%, reaching about 779/974/1083 FFT/s at 80/40/16 MSps for 256 points. The serial format, RF configuration and FFT/window/DC arithmetic are unchanged; mean encoding can differ from the float reference by one existing 0.5 dB wire step.

## Test setup

- Hardware: ESP32-C6 revision v0.1, 160 MHz, native USB Serial/JTAG.
- Toolchain: ESP-IDF commit `25fe69f946311abdaf9ad56591f25fedbc20ac98`, Python 3.12.13 and the repository's ESP-DSP submodule.
- Firmware: `sdkconfig.defaults.esp32c6`; 256/512/1024/2048 points at 80/40/16 MSps, both detectors.
- Controlled settings: 2412 MHz, manual gain 40, ALPF AUTO, DC tracker enabled. Baseline requests lasted two seconds; optimized requests lasted three.

Continuous processing is limited to 256 bins over native USB; larger transforms use snapshots. FFT/s averages in-run `SPS1` telemetry, excluding drain. Coverage is calculated from FFT/s rather than the truncated per-mille field. These are live-RF comparisons, not synthetic cycle benchmarks.

Activate the pinned SDK, then build from the repository root:

```sh
git submodule update --init --recursive
idf.py -B build-c6-local -DIDF_TARGET=esp32c6 \
  -DSDKCONFIG=build-c6-local/sdkconfig \
  -DSDKCONFIG_DEFAULTS=sdkconfig.defaults.esp32c6 build
```

A five-second 80 MSps, 256-point mean request with statistics is `SPEC 5000 1 1 0 0 256 1`. Rate codes 0/1/6 select 80/40/16 MSps; detector 0 is mean and 1 is maximum. See [On-chip spectra](../../spectrum.md) for the protocol.

## Initial state

Continuous capture already worked, but the scalar backend attempted too few windows per completed bank and used software floating point for mean powers.

| Sample rate | FFT points | Initial mean FFT/s | Initial coverage |
|---:|---:|---:|---:|
| 80 MSps | 256 | 589 | 0.188% |
| 40 MSps | 256 | 649 | 0.415% |
| 16 MSps | 256 | 650 | 1.040% |
| 80 MSps | 512 | 545 | 0.349% |
| 80 MSps | 1024 | 260 | 0.333% |
| 80 MSps | 2048 | 130 | 0.333% |

## Flashing procedure and recovery

Disconnect other serial clients, identify the C6 with `INFO`, and set `PORT` to its current native-USB path. Keep a verified flash backup. Use the C6 build's arguments: its bootloader is at `0x0`, not the C5/S31's `0x2000`.

```sh
(cd build-c6-local && python -m esptool --chip esp32c6 -p "$PORT" \
  -b 460800 --before default-reset --after hard-reset write-flash @flash_args)
```

The tested board supported the esptool stub and RTS reset. A ROM-only backup read stopped early; a stub-assisted 2 MiB read succeeded. An unchanged image built with the S31 SDK (`26dd2948`) failed to reach the command interface, so use the pinned C6 SDK. If startup fails, enter download mode with BOOT/RESET and restore the backup or rebuild a known-good image; do not weaken SRAM guards.

## Changes tested and impact

### 1. Multi-window bank processing - retained

**Impact:** Improved continuous bank utilization; combined with integer powers, 256-bin mean throughput rose from 589/649/650 to 779/974/1083 FFT/s at 80/40/16 MSps.

**Change:** Track every complete window in each completed bank rather than making one attempt per bank.

**Details:** Copy, FFT, accumulation and encoding remain bounded between polls. The warmed copy budget includes bank selection. Complete private inputs survive reclamation; uncopied complete windows count as abandoned, while partial tails do not.

### 2. Integer mean accumulation and normalization - retained

**Impact:** Together with scheduling, snapshot mean throughput improved by 33-40% at 80 MSps across 512-2048 points.

**Change:** Accumulate exact unsigned 64-bit squared powers and normalize in the integer log domain.

**Details:** Compute the batch-count logarithm once per frame. Fractional averages and weak powers are preserved without per-bin float operations; regression error stays within one 0.5 dB wire step. Separate mean/max accumulation loops avoided intermediate max-detector regressions.

### 3. Shared typed power workspace - retained

**Impact:** Limited static RAM growth to 7,176 bytes while retaining exact sums.

**Change:** Reuse the snapshot power union for continuous spectra instead of adding independent arrays.

**Details:** Mean and maximum use correctly typed views. BSS ended at `0x4081fa90`, below the reserved RF aperture at `0x40820000`; linker guards passed. FFT/window initialization may still use floating point before acquisition.

## Conclusion

Final controlled mean-detector results were:

| Sample rate | FFT points | Final FFT/s | Change | Final coverage |
|---:|---:|---:|---:|---:|
| 80 MSps | 256 | 779 | +32.3% | 0.249% |
| 40 MSps | 256 | 974 | +50.0% | 0.623% |
| 16 MSps | 256 | 1083 | +66.7% | 1.733% |
| 80 MSps | 512 | 761 | +39.6% | 0.487% |
| 80 MSps | 1024 | 363 | +39.4% | 0.465% |
| 80 MSps | 2048 | 173 | +33.1% | 0.443% |

Continuous max detection improved by 4.7/11.5/75.4% at 80/40/16 MSps. Snapshot maximum detection was essentially unchanged or faster; the largest negative difference was 0.07%.

At the time of the C6 work, all 62 host tests, 24 USB matrix cases, 110 further stop/finite/long/backpressure requests and 18 raw captures passed. Checks covered arithmetic, bank ownership, CRCs, metadata and final counts; no drops or bank failures occurred, and maximum lateness was 23 samples. Later shared C5 tuning was host/build-checked on C6, not physically remeasured; the table describes the original C6 optimization run.

Coverage is `FFT/s * FFT_points / sample_rate`, not packet rate. Abandoned work now counts complete skipped FFT windows, not samples or one attempt per bank. Capture remains compute-limited; UART, other chips and the RF ghost-band issue were not validated by these measurements. This cleanup preserves summarized results and the reusable script, not raw measurement archives.

## Files modified

- `main/common/ring_scalar.h`, `ring_capture.c`: scheduling, retirement, copy budgeting and workspace integration.
- `main/common/spectrum_math.h`: integer logarithms and fractional mean encoding.
- `main/common/spectrum.c`, `spectrum.h`: snapshot sums and shared typed storage.
- `tests/test_c6_fft.py`: arithmetic, ownership and ordering regressions.
- `tests/test_snapshot_spectrum.py`: snapshot mean/max coverage.

## Scripts

These are examples for later use, not hardware tests run during documentation cleanup. Run from the repository root with `pyserial`, exclusive serial access and a new output path.

- [check_c6_fft.py](check_c6_fft.py): controlled C6 or C5 matrix, CRC/count checks, optional stop/backpressure stress and raw captures. Uses the shared protocol helpers in `tools/check_spectrum.py`; its historical name is retained.

```sh
python docs/features/c6_fft_optim/check_c6_fft.py --port "$PORT" \
  --milliseconds 3000 --stress --output /tmp/c6-new-report.json
```
