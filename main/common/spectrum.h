/* Shared on-device spectrum protocol. Acquisition callbacks return normalized
 * IQ10 words (I in bits 0..9, Q in 10..19) in stable CPU-owned memory. */
#pragma once
#include <stdbool.h>
#include <stdint.h>
#define SPECTRUM_INTEGER_POWER (CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C6)
typedef bool (*spectrum_acquire_fn)(unsigned n, unsigned rate, const uint32_t **words, unsigned *elapsed_us);
bool spectrum_command(const char *line, unsigned frequency_mhz, spectrum_acquire_fn acquire);

bool spectrum_fft_init(void);

/* Snapshot and continuous backends run exclusively. Reuse their large buffers
 * on SRAM-constrained targets; acquiring this workspace invalidates the cached
 * snapshot window so a later transport switch rebuilds it. */
typedef struct {
    int16_t *fft, *window;
    float *power;
    uint8_t *frame;
} spectrum_workspace_t;
spectrum_workspace_t spectrum_workspace(void);

/* C5/C6 snapshots use integer powers; the C6 ring shares their union storage. */
typedef struct {
    uint64_t *sum;
    uint32_t *maximum;
} spectrum_integer_workspace_t;
spectrum_integer_workspace_t spectrum_integer_workspace(void);
