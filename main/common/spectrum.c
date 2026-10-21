/* Portable on-chip spectra for targets without a validated continuous backend.
 * C5 snapshots supply multiple complete FFT windows; other targets acquire one.
 * Indices use elapsed wall time plus offsets within a snapshot; flag bit 3
 * explicitly marks the gaps between acquisitions. */
#include "spectrum.h"
#include "spectrum_dc.h"
#include "spectrum_math.h"
#include "spectrum_fft.h"
#include "spectrum_stats.h"
#include "esp_cpu.h"
#if CONFIG_IDF_TARGET_ESP32C5
#include "esp_heap_caps.h"
#endif
#include "burst_serial.h"
#include "dsps_fft2r.h"
#include "esp_rom_crc.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <inttypes.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

#define MAX_FFT      2048u
#define HEADER_BYTES 28u
#ifndef C5_SNAPSHOT_WINDOWS
#define C5_SNAPSHOT_WINDOWS 0
#endif
#if C5_SPECTRUM_PROFILE
static struct {
    uint64_t acquire, window, fft, dc, power, pack, tx, fft_tx;
    unsigned n, ffts, frames;
} profile;
#define SPEC_TIME(stage, ...) do { \
    uint32_t profile_start = esp_cpu_get_cycle_count(); \
    __VA_ARGS__; \
    profile.stage += (uint32_t)(esp_cpu_get_cycle_count() - profile_start); \
} while (0)
#else
#define SPEC_TIME(stage, ...) do { __VA_ARGS__; } while (0)
#endif
#if CONFIG_IDF_TARGET_ESP32C2
/* Quiescent during RF capture; keep heap/stacks in the other SRAM banks. */
#define SPEC_STORAGE __attribute__((section(".c2_spectrum"), aligned(16)))
#else
#define SPEC_STORAGE
#endif
static int16_t fft_data[2 * MAX_FFT] __attribute__((aligned(16))) SPEC_STORAGE;
static int16_t window[MAX_FFT] SPEC_STORAGE;
/* C5/C6 sums are exact through 65535 full-scale FFTs. The C6 ring reuses
 * this storage; the float view remains the workspace for other targets. */
typedef union {
    float mean[MAX_FFT];
#if SPECTRUM_INTEGER_POWER
    uint64_t sum[MAX_FFT];
#endif
    uint32_t maximum[MAX_FFT];
} spectrum_power_t;
#if CONFIG_IDF_TARGET_ESP32C5
/* C5 static sections nearly reach RF SRAM. Keep the larger union in fast
 * DMA-capable heap memory, never RTC/low-power SRAM. */
static spectrum_power_t *power_storage;
#define powers (*power_storage)
#else
static spectrum_power_t powers SPEC_STORAGE;
#endif
static uint8_t frame[HEADER_BYTES + MAX_FFT + 4] SPEC_STORAGE;
static unsigned setup_n;
static bool ready;
spectrum_workspace_t spectrum_workspace(void) {
#if CONFIG_IDF_TARGET_ESP32C5
    if (!spectrum_fft_init()) return (spectrum_workspace_t){0};
#endif
    setup_n = 0;
    return (spectrum_workspace_t){fft_data, window, powers.mean, frame};
}

#if SPECTRUM_INTEGER_POWER
spectrum_integer_workspace_t spectrum_integer_workspace(void) {
#if CONFIG_IDF_TARGET_ESP32C5
    if (!spectrum_fft_init()) return (spectrum_integer_workspace_t){0};
#endif
    return (spectrum_integer_workspace_t){powers.sum, powers.maximum};
}
#endif

static int16_t twiddles[MAX_FFT] __attribute__((aligned(16))) SPEC_STORAGE;
bool spectrum_fft_init(void) {
#if CONFIG_IDF_TARGET_ESP32C5
    if (!power_storage)
        power_storage = heap_caps_malloc(sizeof(*power_storage),
                                         MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA | MALLOC_CAP_8BIT);
    if (!power_storage) return false;
#endif
    if (!ready) ready = dsps_fft2r_init_sc16(twiddles, MAX_FFT) == ESP_OK;
    return ready;
}

static const unsigned rates[] = {
#if CONFIG_IDF_TARGET_ESP32H2
    0,        0,        0,        0,      0, 0,
    16000000, 32000000, 10666667, 6400000
#elif CONFIG_IDF_TARGET_ESP32 || CONFIG_IDF_TARGET_ESP32S2 || CONFIG_IDF_TARGET_ESP32C2 ||                   \
    CONFIG_IDF_TARGET_ESP32C3 || CONFIG_IDF_TARGET_ESP32C6
    80000000, 40000000, 0,       0,
    0,        0,        16000000
#else
    80000000, 40000000, 20000000, 10000000,
    8000000,  4000000
#endif
};
static unsigned rate_hz(unsigned code) {
    return code < sizeof(rates) / sizeof(rates[0]) ? rates[code] : 0;
}
static bool send_text(const char *s) {
    return burst_serial_send(s, strlen(s));
}
static void put16(unsigned at, uint16_t v) {
    frame[at] = v;
    frame[at + 1] = v >> 8;
}
static void put32(unsigned at, uint32_t v) {
    put16(at, v);
    put16(at + 2, v >> 16);
}
static void put64(unsigned at, uint64_t v) {
    put32(at, v);
    put32(at + 4, v >> 32);
}
static void capabilities(void) {
    send_text("SPECINFO {\"continuous\":false,\"transports\":["
#if CONFIG_IDF_TARGET_ESP32C2
              "\"UART\""
#else
              "\"USB\",\"UART\""
#endif
              "],\"profiles\":[");
    bool first = true;
    for (unsigned r = 0; r < sizeof(rates) / sizeof(rates[0]); r++)
        if (rates[r]) {
            for (unsigned n = 256; n <= MAX_FFT; n *= 2) {
                char text[96];
                snprintf(text, sizeof(text), "%s[%u,%u,%u,1,1,%u]", first ? "" : ",", rates[r], r, n,
#if CONFIG_IDF_TARGET_ESP32C61 || CONFIG_IDF_TARGET_ESP32C6
                         n == 256 && burst_serial_port() != BURST_SERIAL_UART
#else
                         0u
#endif
                );
                send_text(text);
                first = false;
            }
        }
    send_text("]}\n");
}

typedef struct {
    size_t pending, sent;
    uint32_t *frames;
    int64_t progress;
} spectrum_output_t;
static bool pump_spectrum(void *context) {
    spectrum_output_t *out = context;
    if (!out->pending) return false;
    size_t written;
    SPEC_TIME(tx, written = burst_serial_try_send(frame + out->sent, out->pending - out->sent));
    out->sent += written;
    if (written) out->progress = esp_timer_get_time();
    if (out->sent == out->pending) {
        out->pending = out->sent = 0;
        (*out->frames)++;
    }
    return out->pending != 0;
}

static inline unsigned spectrum_bin(unsigned j, unsigned log2n) {
    static const uint8_t reverse8[256] = {
        0, 128, 64, 192, 32, 160, 96, 224, 16, 144, 80, 208, 48, 176, 112, 240,
        8, 136, 72, 200, 40, 168, 104, 232, 24, 152, 88, 216, 56, 184, 120, 248,
        4, 132, 68, 196, 36, 164, 100, 228, 20, 148, 84, 212, 52, 180, 116, 244,
        12, 140, 76, 204, 44, 172, 108, 236, 28, 156, 92, 220, 60, 188, 124, 252,
        2, 130, 66, 194, 34, 162, 98, 226, 18, 146, 82, 210, 50, 178, 114, 242,
        10, 138, 74, 202, 42, 170, 106, 234, 26, 154, 90, 218, 58, 186, 122, 250,
        6, 134, 70, 198, 38, 166, 102, 230, 22, 150, 86, 214, 54, 182, 118, 246,
        14, 142, 78, 206, 46, 174, 110, 238, 30, 158, 94, 222, 62, 190, 126, 254,
        1, 129, 65, 193, 33, 161, 97, 225, 17, 145, 81, 209, 49, 177, 113, 241,
        9, 137, 73, 201, 41, 169, 105, 233, 25, 153, 89, 217, 57, 185, 121, 249,
        5, 133, 69, 197, 37, 165, 101, 229, 21, 149, 85, 213, 53, 181, 117, 245,
        13, 141, 77, 205, 45, 173, 109, 237, 29, 157, 93, 221, 61, 189, 125, 253,
        3, 131, 67, 195, 35, 163, 99, 227, 19, 147, 83, 211, 51, 179, 115, 243,
        11, 139, 75, 203, 43, 171, 107, 235, 27, 155, 91, 219, 59, 187, 123, 251,
        7, 135, 71, 199, 39, 167, 103, 231, 23, 151, 87, 215, 55, 183, 119, 247,
        15, 143, 79, 207, 47, 175, 111, 239, 31, 159, 95, 223, 63, 191, 127, 255,
    };
    return ((unsigned)reverse8[j & 255u] << 8 | reverse8[j >> 8]) >> (16 - log2n);
}

static inline void accumulate_spectrum(unsigned n, unsigned det) {
#if SPECTRUM_INTEGER_POWER
    if (det) {
        for (unsigned j = 0; j < n; j++) {
            uint32_t p = spectrum_complex_power(fft_data[2 * j], fft_data[2 * j + 1]);
            if (p > powers.maximum[j]) powers.maximum[j] = p;
        }
    } else {
        for (unsigned j = 0; j < n; j++)
            powers.sum[j] += spectrum_complex_power(fft_data[2 * j], fft_data[2 * j + 1]);
    }
#else
    for (unsigned j = 0; j < n; j++) {
        uint32_t p = spectrum_complex_power(fft_data[2 * j], fft_data[2 * j + 1]);
        if (det) {
            if (p > powers.maximum[j]) powers.maximum[j] = p;
        } else
            powers.mean[j] += (float)p;
    }
#endif
}

/* Encoding consumes the completed batch; transmission owns frame[] while
 * the next batch accumulates independently in powers. */
static size_t pack_spectrum(unsigned n, unsigned log2n, unsigned det, unsigned merged, unsigned frames,
                            uint64_t index, uint8_t gain) {
    memcpy(frame, "SPC1", 4);
    put32(4, frames);
    put64(8, index);
    put32(16, n * merged);
    put16(20, merged);
    frame[22] = 8 | (det ? 1 : 0);
    frame[23] = gain;
    put16(24, 0);
    frame[26] = log2n;
    frame[27] = 2;
    if (det) {
        for (unsigned j = 0; j < n; j++)
            frame[HEADER_BYTES + spectrum_bin(j, log2n)] = spectrum_power_code(powers.maximum[j]);
    } else {
#if SPECTRUM_INTEGER_POWER
        const uint32_t mean_log2 = spectrum_mean_log2(merged);
        for (unsigned j = 0; j < n; j++)
            frame[HEADER_BYTES + spectrum_bin(j, log2n)] = spectrum_sum_power_code(powers.sum[j], mean_log2);
#else
        const float mean_scale = 1.0f / merged;
        for (unsigned j = 0; j < n; j++)
            frame[HEADER_BYTES + spectrum_bin(j, log2n)] = spectrum_mean_power_code(powers.mean[j] * mean_scale);
#endif
    }
    put32(HEADER_BYTES + n, esp_rom_crc32_le(0, frame, HEADER_BYTES + n));
#if SPECTRUM_INTEGER_POWER
    memset(&powers, 0, n * (det ? sizeof(powers.maximum[0]) : sizeof(powers.sum[0])));
#else
    memset(&powers, 0, n * sizeof(float));
#endif
    return HEADER_BYTES + n + 4;
}

bool spectrum_command(const char *line, unsigned frequency_mhz, spectrum_acquire_fn acquire) {
#if C5_SPECTRUM_PROFILE
    if (!strcmp(line, "SPECPROFILE?")) {
        char text[384];
        snprintf(text, sizeof(text),
                 "SPECPROFILE {\"n\":%u,\"ffts\":%u,\"frames\":%u,"
                 "\"acquire\":%" PRIu64 ",\"window\":%" PRIu64 ",\"fft\":%" PRIu64
                 ",\"dc\":%" PRIu64 ",\"power\":%" PRIu64 ",\"pack\":%" PRIu64
                 ",\"tx\":%" PRIu64 ",\"fft_tx\":%" PRIu64 "}\n",
                 profile.n, profile.ffts, profile.frames, profile.acquire, profile.window,
                 profile.fft, profile.dc, profile.power, profile.pack, profile.tx, profile.fft_tx);
        send_text(text);
        return true;
    }
#endif
    unsigned dc_mode;
    char dc_extra;
    if (!strcmp(line, "DC?")) {
        char text[16];
        snprintf(text, sizeof(text), "DC %u\n", spectrum_dc_mode);
        send_text(text);
        return true;
    }
    if (sscanf(line, "DC %u %c", &dc_mode, &dc_extra) == 1 && dc_mode < 2) {
        spectrum_dc_mode = dc_mode;
        send_text("OK\n");
        return true;
    }
    if (!strcmp(line, "SPECINFO?")) {
        capabilities();
        return true;
    }
    if (strncmp(line, "SPEC ", 5)) return false;
    unsigned ms, stride, units, det, rate, n, stats = 0;
    char extra;
    int fields =
        sscanf(line, "SPEC %u %u %u %u %u %u %u %c", &ms, &stride, &units, &det, &rate, &n, &stats, &extra);
    if ((fields != 6 && fields != 7) || stats > 1 || ms > 86400000u || stride != 1 || !units || units > 8 ||
        det > 1 || !rate_hz(rate) || n < 256 || n > MAX_FFT || (n & (n - 1))) {
        send_text("ERR spec_args\n");
        return true;
    }
    spectrum_fft_init();
    if (!ready) {
        send_text("ERR spectrum_memory\n");
        return true;
    }
    if (setup_n != n) {
        for (unsigned j = 0; j < n; j++) window[j] = (int16_t)lrintf(16383.5f * (1 - cosf(2 * M_PI * j / n)));
        setup_n = n;
    }
    unsigned log2n = 0;
    while ((1u << log2n) < n) log2n++;
    char text[192];
    snprintf(text, sizeof(text), "SPEC %u %u %u %u\n", n, rate_hz(rate), n, frequency_mhz);
    if (!send_text(text)) return true;
    int64_t start = esp_timer_get_time(), yield_at = start;
    uint32_t frames = 0, ffts = 0, status = 0;
    uint64_t pairs = 0;
    bool stopped = false;
#if C5_SPECTRUM_PROFILE
    memset(&profile, 0, sizeof(profile));
    profile.n = n;
#endif
    spectrum_dc_t dc = {0};
    spectrum_stats_t telemetry;
    spectrum_stats_init(&telemetry);
    unsigned merged = 0;
    uint8_t gain = 0;
    uint64_t index = 0;
    spectrum_output_t out = {.frames = &frames, .progress = start};
#if CONFIG_IDF_TARGET_ESP32C5
    /* chip.h exposes 16380 words plus four overrun canaries in RF SRAM.
     * RF stays stopped while the windows are consumed, so no prefetch buffer
     * or bank-ownership protocol is needed. State is local to this request. */
    unsigned snapshot_windows = 16380u / n;
    if (C5_SNAPSHOT_WINDOWS && snapshot_windows > C5_SNAPSHOT_WINDOWS)
        snapshot_windows = C5_SNAPSHOT_WINDOWS;
    const unsigned snapshot_n = snapshot_windows * n;
    const uint32_t *snapshot = NULL;
    unsigned snapshot_at = snapshot_n;
    uint64_t snapshot_index = 0;
#endif
#if SPECTRUM_INTEGER_POWER
    memset(&powers, 0, n * (det ? sizeof(powers.maximum[0]) : sizeof(powers.sum[0])));
#else
    memset(&powers, 0, n * sizeof(float));
#endif
    do {
        if (burst_serial_stop_requested()) {
            stopped = true;
            break;
        }
        uint32_t busy_start = esp_cpu_get_cycle_count();
        const uint32_t *words;
        unsigned elapsed;
#if CONFIG_IDF_TARGET_ESP32C5
        if (snapshot_at == snapshot_n) {
            snapshot_index = (uint64_t)(esp_timer_get_time() - start) * rate_hz(rate) / 1000000u;
            bool acquired;
            SPEC_TIME(acquire, acquired = acquire(snapshot_n, rate, &snapshot, &elapsed));
            if (!acquired) {
                status = 1;
                break;
            }
            snapshot_at = 0;
        }
        words = snapshot + snapshot_at;
        if (!merged) index = snapshot_index + snapshot_at;
        snapshot_at += n;
#else
        if (!merged) index = (uint64_t)(esp_timer_get_time() - start) * rate_hz(rate) / 1000000u;
        if (!acquire(n, rate, &words, &elapsed)) {
            status = 1;
            break;
        }
#endif
        if (!merged) gain = words[0] >> 20;
        SPEC_TIME(window, for (unsigned j = 0; j < n; j++) {
            int32_t i = ((int32_t)(words[j] << 22) >> 22), q = ((int32_t)(words[j] << 12) >> 22);
            /* IQ10 times a nonnegative Q15 Hann fits int16 without clipping. */
            fft_data[2 * j] = (int16_t)((i * window[j]) >> 9);
            fft_data[2 * j + 1] = (int16_t)((q * window[j]) >> 9);
        });
#if C5_SPECTRUM_PROFILE
        uint64_t fft_tx_start = profile.tx;
#endif
        SPEC_TIME(fft, spectrum_fft_poll(fft_data, n, dsps_fft_w_table_sc16,
                                         out.pending ? pump_spectrum : NULL, &out));
#if C5_SPECTRUM_PROFILE
        profile.fft_tx += profile.tx - fft_tx_start;
#endif
        SPEC_TIME(dc, spectrum_dc_apply(&dc, fft_data, n));
        SPEC_TIME(power, accumulate_spectrum(n, det));
        ffts++;
        pairs += n;
        merged++;
        pump_spectrum(&out);
        if (out.pending && esp_timer_get_time() - out.progress > 3000000) {
            status = 7;
            break;
        }
        if (!out.pending) {
            if (stats) spectrum_stats_emit(&telemetry, n, rate_hz(rate), ffts, 0, 0, 0, 0, burst_serial_send);
            SPEC_TIME(pack, out.pending = pack_spectrum(n, log2n, det, merged, frames, index, gain));
            merged = 0;
            out.progress = esp_timer_get_time();
            pump_spectrum(&out);
        }
        telemetry.busy += (uint32_t)(esp_cpu_get_cycle_count() - busy_start);
        if (merged == 65535) break; /* bound the wire FFT count if the host stalls */
        if (esp_timer_get_time() - yield_at >= 20000) {
            vTaskDelay(1);
            yield_at = esp_timer_get_time();
        }
    } while (!ms || esp_timer_get_time() - start < (int64_t)ms * 1000);
    /* Drain in order; a partial final batch never contains an earlier frame. */
    if (out.pending) {
        if (burst_serial_send(frame + out.sent, out.pending - out.sent))
            frames++;
        else
            status = 7;
    }
    if (!status && merged) {
        size_t size;
        SPEC_TIME(pack, size = pack_spectrum(n, log2n, det, merged, frames, index, gain));
        if (burst_serial_send(frame, size))
            frames++;
        else
            status = 7;
    }
#if C5_SPECTRUM_PROFILE
    profile.ffts = ffts;
    profile.frames = frames;
#endif
    snprintf(text, sizeof(text), "SPECEND %u 0 %u %" PRIu64 " %" PRIi64 " 0 0 %u 0 0 %u %u\n",
             (unsigned)status, (unsigned)ffts, pairs, esp_timer_get_time() - start, (unsigned)frames,
             (unsigned)ffts, stopped);
    send_text(text);
    return true;
}
