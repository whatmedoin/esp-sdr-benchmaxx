# Receiver benchmarks — ESP32-C5, ESP32-C6, ESP32-S3

CW power-accuracy and sensitivity measurements of four ESP32 boards. Each board was driven from a
signal generator through a nominal 10 dB pad (phase 1) and a nominal 50 dB pad (phase 2), with manual
gain only, the on-chip mean FFT, and the receive LO tuned 2 MHz below the generator frequency.

Each board directory holds one per-capture table, the aggregated results, the frozen decisions, the
band report and the replay checks, together with the plots and the full measurement report.

## Boards

| Directory | Chip | DUT serial | Firmware revision | FFT profile | Phase 1: gain / offset | Phase 2: gain | Headline |
|---|---|---|---|---|---|---|---|
| `esp32s3/` | ESP32-S3 (#1) | `A0:F2:62:F3:9D:AC` | `f86da818…` | 80 MS/s, 2048 bins | 20 / −6.823643 dB | 80 | 99/99 points within ±2 dB, worst 0.986 dB; CW thresholds −79…−53 dBm (2400–2500/10 MHz), −85…−73 dBm (2200–2600/50 MHz) |
| `esp32s3_2/` | ESP32-S3 (#2) | `A0:F2:62:F3:A1:8C` | `9b61e746…` | 80 MS/s, 2048 bins | 20 / −4.175441 dB | 79 | 31/33 working-band captures pass, worst 1.022 dB; 16/20 extended frequencies confirmed |
| `esp32c5/` | ESP32-C5 | `D0:CF:13:E5:A7:9C` | `466a705d…` (dirty) | 80 MS/s, 2048 bins | 20 / −4.188307 dB | 65 | 13/20 extended frequencies confirmed, none at 2000/2050 and 2750–2950 MHz |
| `esp32c6/` | ESP32-C6 | `58:E6:C5:1D:DE:F8` | `9b61e746…` | 80 MS/s, 2048 bins | 21 / −3.699680 dB | 30 | 20/23 tiered frequencies confirmed, none at 2460, 2900, 2950 MHz |

Phase 1 = nominal 10 dB pad (DUT input −50…−10 dBm); Phase 2 = nominal 50 dB pad (−90…−50 dBm).
All dBm values are **nominal** (generator setting minus nominal pad); there was no power meter,
pad-vs-frequency calibration or cable-loss characterisation. Gain indices are not dB.

## Files

In each of `esp32c5/`, `esp32c6/`, `esp32s3_2/`:

| File | Content |
|---|---|
| `README.md` | full report for the board |
| `plots/*.png` | gain search, working band, selection verify, extended band, pad overlap, FFT profiles, sensitivity |
| `measurements.csv` | per-capture metrics for every phase, both pads, source-on and source-off |
| `sensitivity.csv` | confirmed CW threshold per RF with the empirical weaker failed level |
| `pad_overlap.csv` | 10 dB vs 50 dB reading comparison at −50 dBm |
| `fft_profiles.csv` | per-profile tone accuracy, image rejection, frame/FFT counts (**no throughput data**) |
| `raw_adc.csv` | source-off raw ADC snapshots: rail-hit fractions, AC RMS, peak count |
| `selection.json` | `phase1`, `phase2` decision documents + `lo_calibration` (fitted model, or the "no correction" note) |
| `summary.json` | `phase1`, `phase2` band statistics, integrity and limits |
| `validation.json` | `phase1`, `phase2` replay outcomes, checks and final instrument state |
| `lo_calibration.csv` | (C5 only) LO offset vs RF, per-capture |
| `usable_range_10db.csv` | (C5 only) ±2 dB frequency intervals |

`esp32s3/` (board #1) has a slightly different file set:

| File | Content |
|---|---|
| `measurements.csv` | per-capture table, both pads |
| `sensitivity.csv`, `gain_search.csv`, `gain_overlap.csv` | threshold per RF, extended gain curve, pad-overlap comparison |
| `adc_background.csv` | source-off raw ADC statistics |
| `selection.json` | `phase1`, `phase1_levels`, `phase2` decisions |
| `summary.json` | `phase1`, `phase2` band statistics and validated ranges |
| `plots/` | gain search (both pads), working band, extended band, sensitivity |

## `measurements.csv`

One row per capture.

| Column | Meaning |
|---|---|
| `phase` | acquisition phase or session name (phase 1 = 10 dB pad, phase 2 = 50 dB pad) |
| `utc` | capture timestamp |
| `attenuator_db` | nominal pad in use |
| `gain_index` | manual gain index |
| `rf_mhz`, `lo_mhz` | commanded RF and receive LO |
| `source_enabled` | generator RF state; `False` marks a source-off control capture |
| `nominal_input_dbm` | nominal DUT input (generator setting minus pad) |
| `generator_readback_dbm` | generator level read back at the point |
| `stage` | adaptive scan stage (`coarse`, `refine`, `confirm`, `recovery`) |
| `tone_dbfs` | integrated CW estimate relative to a Hann full-scale tone |
| `snr_db` | tone-to-local-noise ratio of the capture; **empty for C6 and S3#2 phase-2 rows**, which record only the minimum block SNR |
| `block_min_snr_db` | worst of the five consecutive time blocks |
| `block_std_db`, `block_peak_to_peak_db` | spread of the five block powers; empty for C6 and S3#2 phase-2 rows |
| `freq_error_khz` | measured tone frequency minus commanded RF |
| `on_off_excess_db` | in-band power above the matching source-off control |
| `gates_ok` | all acceptance gates met |
| `gain_meta_mismatch` | fraction of frames whose gain metadata disagreed with the commanded index |
| `frames`, `ffts` | frames and completed FFTs in the capture |
| `generator_errors` | generator error-queue entries at the point |

A gain/centre/input label can appear more than once — as a source-on point, as its source-off control,
or as a repeat capture. `utc` and `source_enabled` identify the individual capture.

## Limits

- **Nominal power only.** Generator level accuracy, pad tolerance, connectors and mismatch are inside
  the reported error; there is no traceable absolute input power.
- **Processed metrics only.** `validation.json` records the outcome of the replay checks; the raw
  spectra are not included, so the checks cannot be re-executed from these files.
- **Sensitivity is setup-limited** known-frequency CW detectability (15 dB SNR per block, ≥10 dB on/off,
  ≤150 kHz error, three passing captures), not a noise figure, Wi-Fi BER/PER or an intrinsic limit.
- **`fft_profiles.csv` is accuracy only.** It contains no FFT/s or coverage numbers; those are in
  `docs/features/{c5,c6}_fft_optim/` and `docs/features/fft_optim/` (the latter is **ESP32-S31**, not S3).
  The C6 profile capture predates the C6 FFT optimisation, so its frame/FFT counts must not be read as
  post-optimisation performance.
