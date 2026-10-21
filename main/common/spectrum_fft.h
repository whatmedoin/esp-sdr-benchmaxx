/* Rounded Q15 radix-2 FFT, with bounded transport-service points.
 * Same butterflies and bit-reversed twiddles as the ESP-DSP ANSI kernel. */
#pragma once
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#if CONFIG_IDF_TARGET_ESP32C5
#ifndef C5_FFT_POLL_BUTTERFLIES
#define C5_FFT_POLL_BUTTERFLIES 512
#endif
enum { SPECTRUM_FFT_POLL_BUTTERFLIES = C5_FFT_POLL_BUTTERFLIES };
#else
enum { SPECTRUM_FFT_POLL_BUTTERFLIES = 32 };
#endif
static inline void spectrum_fft_butterfly(int16_t *a, int16_t *b, int32_t wr, int32_t wi) {
    int32_t ar = a[0], ai = a[1], br = b[0], bi = b[1];
    uint32_t tr = (uint32_t)(wr * br) + (uint32_t)(wi * bi);
    uint32_t ti = (uint32_t)(wr * bi) - (uint32_t)(wi * br);
    uint32_t rr = (uint32_t)(ar * 32767) + 32767u, ri = (uint32_t)(ai * 32767) + 32767u;
    a[0] = (int16_t)((rr + tr) >> 16);
    a[1] = (int16_t)((ri + ti) >> 16);
    b[0] = (int16_t)((rr - tr) >> 16);
    b[1] = (int16_t)((ri - ti) >> 16);
}
/* A false callback result disables further service work for this transform. */
static inline void spectrum_fft_poll(int16_t *x, unsigned n, const int16_t *w, bool (*poll)(void *),
                                     void *context) {
    unsigned budget = SPECTRUM_FFT_POLL_BUTTERFLIES;
    for (unsigned half = n / 2, groups = 1; half; half /= 2, groups *= 2) {
        if (half == 2) {
            /* Independent four-sample groups: retain both stages' rounding
             * while avoiding another full pass and short-loop setup. */
            for (unsigned at = 0; at < n; at += 4) {
                int16_t *a = x + 2 * at;
                spectrum_fft_butterfly(a, a + 4, w[at / 2], w[at / 2 + 1]);
                spectrum_fft_butterfly(a + 2, a + 6, w[at / 2], w[at / 2 + 1]);
                spectrum_fft_butterfly(a, a + 2, w[at], w[at + 1]);
                spectrum_fft_butterfly(a + 4, a + 6, w[at + 2], w[at + 3]);
                if (poll) {
                    budget -= 4;
                    if (!budget) {
                        if (!poll(context)) poll = NULL;
                        budget = SPECTRUM_FFT_POLL_BUTTERFLIES;
                    }
                }
            }
            break;
        }
        for (unsigned group = 0; group < groups; group++) {
            int32_t wr = w[2 * group], wi = w[2 * group + 1];
            int16_t *a = x + group * half * 4, *b = a + half * 2;
            unsigned left = half;
            while (left) {
                unsigned count = !poll || left < budget ? left : budget;
                unsigned j = 0;
                for (; j + 4 <= count; j += 4, a += 8, b += 8) {
                    spectrum_fft_butterfly(a, b, wr, wi);
                    spectrum_fft_butterfly(a + 2, b + 2, wr, wi);
                    spectrum_fft_butterfly(a + 4, b + 4, wr, wi);
                    spectrum_fft_butterfly(a + 6, b + 6, wr, wi);
                }
                for (; j < count; j++, a += 2, b += 2)
                    spectrum_fft_butterfly(a, b, wr, wi);
                left -= count;
                if (poll) {
                    budget -= count;
                    if (!budget) {
                        if (!poll(context)) poll = NULL;
                        budget = SPECTRUM_FFT_POLL_BUTTERFLIES;
                    }
                }
            }
        }
    }
}
