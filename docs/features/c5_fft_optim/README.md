# ESP32-C5 FFT optimization

## Summary

Increase snapshot FFT throughput by reusing C6 integer-power arithmetic, processing multiple windows per acquisition, and improving scalar FFT/output scheduling. At 80 MSps, mean throughput reached about 3,318 FFT/s at 256 points and 373 FFT/s at 2048 points, versus the original 432 and 173 FFT/s. The serial format, RF configuration and fixed-point FFT results are preserved; integer mean encoding can differ from the float reference by one existing 0.5 dB wire step.

## Test setup

- Hardware: ESP32-C5HF4 revision v1.0, 240 MHz, 4 MiB flash, 48 MHz crystal, native USB Serial/JTAG.
- Toolchain: ESP-IDF commit `25fe69f946311abdaf9ad56591f25fedbc20ac98`, Python 3.12.13 and the repository's ESP-DSP submodule.
- Firmware: `sdkconfig.defaults.esp32c5`; 256/512/1024/2048 points at 80/40/20/10/8/4 MSps, both detectors.
- Controlled settings: 2412 MHz, manual gain 40, LPF AUTO, ALPF AUTO, DC tracker enabled; two-second matrix requests.

FFT/s averages in-run `SPS1` telemetry, excluding output drain. These are live-RF comparisons, not synthetic cycle benchmarks. The follow-up baseline was rebuilt from `466a705`; its embedded revision reported `unknown`, but its source was checked against that commit.

Activate the pinned SDK, then build from the repository root:

```sh
git submodule update --init --recursive
idf.py -B build-c5-local -DIDF_TARGET=esp32c5 \
  -DSDKCONFIG=build-c5-local/sdkconfig \
  -DSDKCONFIG_DEFAULTS=sdkconfig.defaults.esp32c5 \
  -DC5_SPECTRUM_PROFILE=OFF -DC5_SNAPSHOT_WINDOWS=0 \
  -DC5_FFT_POLL_BUTTERFLIES=512 build
```

A five-second 80 MSps, 2048-point mean request with statistics is `SPEC 5000 1 1 0 0 2048 1`. Rate codes 0-5 select 80/40/20/10/8/4 MSps; detector 0 is mean and 1 is maximum. See [On-chip spectra](../../spectrum.md) for the protocol.

## Initial state

Every FFT acquired a separate snapshot and accumulated mean power using software floating point. At 80 MSps, the vendor capture call took about 1,684 us for 256 samples versus 1,882 us for 16,380 samples, making repeated acquisition setup expensive.

| Sample rate | FFT points | Initial mean FFT/s |
|---:|---:|---:|
| 80 MSps | 256 | 431.9 |
| 80 MSps | 512 | 362.4 |
| 80 MSps | 1024 | 271.7 |
| 80 MSps | 2048 | 173.3 |
| 4 MSps | 256 | 428.1 |
| 4 MSps | 2048 | 153.0 |

## Flashing procedure and recovery

Disconnect other serial clients, identify the C5 with `INFO`, and set `PORT` to its current native-USB path. Keep a verified flash backup and use the C5 build's arguments: its bootloader is at `0x2000`, not the C6's `0x0`.

```sh
(cd build-c5-local && python -m esptool --chip esp32c5 -p "$PORT" \
  -b 460800 --before default-reset --after hard-reset write-flash @flash_args)
```

If startup fails, enter download mode with BOOT/RESET and restore the backup or rebuild a known-good image. Do not weaken SRAM guards. `ERR spectrum_memory` means the required fast-memory allocation failed; total free heap alone does not guarantee a suitable block.

## Changes tested and impact

### 1. Multi-window snapshots and integer powers - retained

**Impact:** Together with initial FFT scheduling changes, the first pass reached 3,042 FFT/s at 256 points and 343 FFT/s at 2048 points, at 80 MSps.

**Change:** Amortize acquisition setup and reuse C6 exact 64-bit sums and integer log-domain normalization.

**Details:** The existing RF buffer holds 63/31/15/7 complete windows. Request-local offsets preserve window order, gain metadata and stop checks; no extra raw buffer is allocated. Fractional mean powers are retained.

### 2. Static power workspace - rejected

**Impact:** The larger union failed the RF-aperture linker guard; it was not flashed.

**Change:** Attempt static allocation of the 16 KiB power workspace.

**Details:** The retained implementation instead allocates DMA-capable internal SRAM once. C5 BSS remains below `0x40820000`; allocation failure is reported without changing RF ownership checks.

### 3. Stage profiling - diagnostic only

**Impact:** Identified FFT computation/bookkeeping as the largest remaining cost, followed by acquisition and encoding.

**Change:** Add optional cycle counters queried through `SPECPROFILE?`.

**Details:** `C5_SPECTRUM_PROFILE=ON` enables diagnostics without inserting packets into SPEC output. FFT totals include callbacks; `fft_tx` is their measured FIFO-write subset, not another additive stage. Production measurements use profiling OFF.

### 4. Smaller snapshot batches - rejected

**Impact:** At 80 MSps/256 points, depth 32 lost 5-7%, depth 16 about 20%, and depth 8 about 36-37% versus capacity.

**Change:** Limit the windows processed per acquisition.

**Details:** Retain `C5_SNAPSHOT_WINDOWS=0` for maximum fitting capacity. Limits above capacity have no effect.

### 5. FFT and output scheduling - retained

**Impact:** Alongside encoding changes, the follow-up improved all 48 rate/size/detector cases by another 5.4-10.8%.

**Change:** Hoist twiddles, stream pointers, preserve the fused final stages, unroll four butterflies, and replace per-butterfly polling counters with bounded chunks.

**Details:** Callbacks stop when output completes. Repeated comparisons favored 512-butterfly C5 service points over 128 by roughly 0-3.6%; other targets retain 32 while output is pending. Checking for empty output only at FFT entry had negligible benefit.

### 6. Encoding specialization - retained

**Impact:** Diagnostic 2048-bin packing cost fell from about 328 to 311 us per completed mean FFT, and 297 to 283 us for maximum detection.

**Change:** Use a 256-byte constant bit-reversal table and separate detector loops.

**Details:** These are combined encoding gains, not an isolated table benchmark. No additional SRAM workspace or reduced-precision powers are used.

### 7. FFT placement in IRAM - rejected

**Impact:** Consumed 1,280 extra static SRAM bytes without measurable gain.

**Change:** Run the FFT from internal instruction RAM instead of cached flash.

**Details:** The 2048-bin mean FFT measured about 1,732 us in IRAM versus 1,728 us from flash. The experiment and its option were removed.

## Conclusion

Separate controlled comparisons produced these mean-detector results:

| Sample rate | FFT points | First-pass FFT/s | Final FFT/s |
|---:|---:|---:|---:|
| 80 MSps | 256 | 3042.3 | 3318.1 |
| 80 MSps | 512 | 1475.4 | 1603.6 |
| 80 MSps | 1024 | 713.3 | 762.3 |
| 80 MSps | 2048 | 342.9 | 372.7 |
| 4 MSps | 256 | 2613.0 | 2787.6 |
| 4 MSps | 2048 | 284.9 | 309.6 |

The final validation passed 64 firmware/tool host tests, C5/C6 builds, 260 spectrum requests and 36 raw captures. Checks covered exact FFT outputs, encoding tolerance, CRCs, counts, stops, backpressure and allocation failures; no transport drops occurred. The broader suite had one unrelated wideband CLI test failing in two subcases. C6 hardware and UART were not retested during C5 tuning.

Capture still has gaps: coverage is `FFT/s * FFT_points / sample_rate`, not packet rate. Continuous capture, lean register triggering and hand-written RISC-V FFTs were not implemented; they require separate hardware/numeric validation. This cleanup preserves the summarized results and reusable scripts, not raw measurement archives.

## Files modified

- `main/common/spectrum.c`, `spectrum.h`, `spectrum_math.h`: shared C6 arithmetic, C5 workspace and snapshot processing.
- `main/common/spectrum_fft.h`: exact scalar FFT and bounded service scheduling.
- `main/CMakeLists.txt`: profiling and C5 tuning options.
- `tests/test_snapshot_spectrum.py`, `tests/test_spectrum_math.py`: numeric and lifecycle regressions.
- `tests/test_profile_c5_fft.py`: benchmark/profiler transport checks.

## Scripts

These are examples for later use, not hardware tests run during documentation cleanup. Run from the repository root with `pyserial` and exclusive serial access; use new output paths.

- [profile_c5_fft.py](profile_c5_fft.py): repeated throughput comparisons; `--profile` additionally requires a diagnostic image. Uses the shared [C5/C6 matrix script](../c6_fft_optim/check_c6_fft.py) and `tools/check_spectrum.py` rather than duplicating their protocol code.

```sh
python docs/features/c6_fft_optim/check_c6_fft.py --port "$PORT" \
  --milliseconds 2000 --stress --output /tmp/c5-new-report.json
python docs/features/c5_fft_optim/profile_c5_fft.py --port "$PORT" \
  --rates 0 5 --repeats 2 --output /tmp/c5-new-comparison.json
```

For stage counters, rebuild and flash with `-DC5_SPECTRUM_PROFILE=ON`, then add `--profile` to the comparison command. Rebuild and flash with profiling OFF afterward; CMake caches these settings.
