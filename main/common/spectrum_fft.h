/* Rounded Q15 radix-2 FFT, with bounded transport-service points.
 * Same butterflies and bit-reversed twiddles as the ESP-DSP ANSI kernel. */
#pragma once
#include <stdint.h>
#if CONFIG_IDF_TARGET_ESP32C5
enum { SPECTRUM_FFT_POLL_BUTTERFLIES = 128 };
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
static inline void spectrum_fft_poll(int16_t *x, unsigned n, const int16_t *w, void (*poll)(void *),
                                     void *context) {
    unsigned work = 0;
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
                work += 4;
                if ((work & (SPECTRUM_FFT_POLL_BUTTERFLIES - 1u)) == 0) poll(context);
            }
            break;
        }
        for (unsigned group = 0; group < groups; group++) {
            int32_t wr = w[2 * group], wi = w[2 * group + 1];
            int16_t *a = x + group * half * 4, *b = a + half * 2;
            for (unsigned offset = 0; offset < half; offset++, a += 2, b += 2) {
                spectrum_fft_butterfly(a, b, wr, wi);
                if ((++work & (SPECTRUM_FFT_POLL_BUTTERFLIES - 1u)) == 0) poll(context);
            }
        }
    }
}
