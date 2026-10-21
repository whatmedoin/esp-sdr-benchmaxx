"""C6 integer mean encoding and multi-window scalar bank scheduling."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('cc'), 'Host C compiler unavailable')
class C6FFT(unittest.TestCase):
    def run_c(self, source):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'check.c'
            path.write_text(source)
            exe = Path(tmp) / 'check'
            subprocess.run(['cc', '-std=gnu11', '-O2', '-Wall',
                            '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                            '-I' + str(ROOT / 'main/common'), str(path), '-lm', '-o', str(exe)], check=True)
            subprocess.run([str(exe)], check=True, timeout=30)

    def test_integer_mean_precision_and_full_scale_batches(self):
        self.run_c(r'''
#include <assert.h>
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include "spectrum_math.h"
static void check(uint64_t sum, unsigned count) {
    uint8_t code = spectrum_sum_power_code(sum, spectrum_mean_log2(count));
    double mean = (double)sum / count;
    int expected = mean < 1 ? 0 : (int)lrint(20 * log10(mean));
    if (expected > 255) expected = 255;
    assert(abs((int)code - expected) <= 1);
    assert(abs((int)code - spectrum_mean_power_code((float)mean)) <= 1);
}
int main(void) {
    const unsigned counts[] = {1,2,3,7,8,17,255,256,257,1023,4095,32768,65534,65535};
    for (unsigned c = 0; c < sizeof(counts)/sizeof(*counts); c++) {
        unsigned count = counts[c];
        check(0, count);
        check(1, count);
        check(count - 1, count);
        check(count, count);
        check(count + 1, count);
        check(3ull * count / 2, count);
        check((UINT64_C(1) << 31) * count, count);
        for (uint64_t power = 1; power <= (UINT64_C(1) << 31); power = power * 103 / 100 + 1)
            check(power * count, count);
    }
    /* Probe code boundaries and weak fractional powers, not only integers. */
    for (unsigned code = 0; code < 190; code++)
        for (int offset = -10; offset <= 10; offset++)
            for (unsigned c = 0; c < sizeof(counts)/sizeof(*counts); c++) {
                unsigned count = counts[c];
                double mean = pow(10, (code + .5 + offset * .01) / 20);
                if (mean <= 2147483648.0) check((uint64_t)(mean * count), count);
            }
    uint32_t rng = 12345678;
    for (unsigned trial = 0; trial < 200000; trial++) {
        rng = rng * 1664525u + 1013904223u;
        unsigned count = 1 + rng % 65535;
        rng = rng * 1664525u + 1013904223u;
        unsigned exponent = rng % 32;
        rng = rng * 1664525u + 1013904223u;
        uint64_t sum = exponent ? (uint64_t)(rng >> (32 - exponent)) * count : 0;
        check(sum, count);
    }
    assert(spectrum_sum_power_code(3, spectrum_mean_log2(2)) > 0);
    assert(spectrum_sum_power_code(UINT64_MAX, 0) == 255);
    return 0;
}
''')

    def test_bank_retirement_order_wrap_and_integer_accumulation(self):
        source = (ROOT / 'main/common/ring_capture.c').read_text()
        work = source[source.index('typedef struct {\n    bool pending;'):
                      source.index('enum { BLK_IDLE')]
        harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <math.h>
#include <stdlib.h>
#include "spectrum.h"
#define CONFIG_IDF_TARGET_ESP32C6 1
#define RING_BANKS 2u
#define RING_PAIRS 16384u
#define RING_MASK (RING_PAIRS - 1u)
#define SPEC_MAGIC 0x31435053u
static unsigned spec_n = 256, spec_log2 = 8;
static int16_t fft_buf[512], coefficients[2048];
static int16_t *dsps_fft_w_table_sc16 = coefficients;
static uint16_t bin_of[256];
static uint8_t frame_out[288];
typedef struct __attribute__((packed)) {
    uint32_t magic, frame; uint64_t pair_index; uint32_t pairs;
    uint16_t ffts; uint8_t flags, gain; uint16_t drops; uint8_t nfft_log2, db_step;
} spec_header_t;
static struct {unsigned abandoned, frames, drops, ffts, work_max;} result;
typedef typeof(result) ring_result_t;
static struct {bool max_hold;} config;
static union {uint64_t sum[256]; uint32_t maximum[256];} powers;
static spectrum_integer_workspace_t scalar_power = {powers.sum, powers.maximum};
static unsigned txq_head, txq_tail;
'''
        helpers = r'''
static struct {typeof(result) *res; typeof(config) *cfg; work_t work[RING_BANKS];
               bool dropped; uint64_t last_ok;} st;
static uint32_t banks[RING_BANKS][RING_PAIRS];
static bool valid[RING_BANKS];
static unsigned copies, copied_bank[256], copied_at[256], sent, represented;
static uint64_t first_index, last_index;
static const uint32_t *bank_ptr(unsigned b) {assert(b < RING_BANKS && valid[b]); return banks[b];}
static void spec_unpack(const uint32_t *p, unsigned at, unsigned from, unsigned to) {
    assert(from == 0 && to == 256 && copies < 256);
    copied_bank[copies] = p == banks[0] ? 0 : 1;
    copied_at[copies++] = at;
    for (unsigned j = from; j < to; j++) {
        fft_buf[2*j] = (int16_t)p[(at+j)&RING_MASK];
        fft_buf[2*j+1] = 0;
    }
}
static void spec_remove_dc(void) {}
static uint32_t esp_cpu_get_cycle_count(void) {static unsigned t; return ++t;}
static uint64_t esp_timer_get_time(void) {return 0;}
static uint32_t esp_rom_crc32_le(unsigned c, const void *p, unsigned n) {return 0;}
static bool txq_push(const void *p, unsigned n) {
    const spec_header_t *h = p;
    assert(n == 288 && h->magic == SPEC_MAGIC && h->ffts && h->pairs >= 256);
    assert(h->nfft_log2 == 8 && h->db_step == 2 && (h->flags & 2));
    if (sent) assert(h->pair_index > last_index);
    else first_index = h->pair_index;
    last_index = h->pair_index;
    represented += h->ffts;
    sent++;
    return true;
}
#define scalar_telemetry(cycles, complete) ((void)0)
#include "ring_scalar.h"
static void reset(void) {
    memset(&st, 0, sizeof(st)); memset(&scalar, 0, sizeof(scalar));
    memset(&result, 0, sizeof(result)); memset(&powers, 0, sizeof(powers));
    st.res = &result; st.cfg = &config;
    config.max_hold = false; txq_head = txq_tail = copies = sent = represented = 0;
    first_index = last_index = 0;
    for (unsigned b = 0; b < RING_BANKS; b++) {
        valid[b] = true;
        for (unsigned j = 0; j < RING_PAIRS; j++) banks[b][j] = j + (10u << 20);
    }
}
static void poison(unsigned bank) {
    valid[bank] = false; memset(banks[bank], 0xa5, sizeof(banks[bank]));
}
static void drain(void) {
    unsigned steps = 0;
    while (scalar_work()) assert(++steps < 20000);
    scalar_flush();
    while (scalar_work()) assert(++steps < 20000);
}
static void reach(unsigned phase) {
    unsigned steps = 0;
    while (scalar.phase != phase) assert(scalar_work() && ++steps < 1000);
}
int main(void) {
    for (unsigned j = 0; j < 256; j++) {
        unsigned r = 0;
        for (unsigned k = 0; k < 8; k++) r = r*2 + ((j>>k)&1);
        bin_of[j] = r;
    }
    for (unsigned j = 0; j < 1024; j++) {
        unsigned r = 0;
        for (unsigned k = 0; k < 10; k++) r = r*2 + ((j>>k)&1);
        coefficients[2*j] = (int16_t)(32767*cos(2*M_PI*r/2048));
        coefficients[2*j+1] = (int16_t)(32767*sin(2*M_PI*r/2048));
    }
    /* Every complete window is offered; wrapped copies retain their order. */
    for (unsigned maximum = 0; maximum < 2; maximum++) {
        reset(); config.max_hold = maximum;
        scalar_offer(0, 16300, 3*256+13, 1000); drain();
        assert(result.ffts == 3 && !result.abandoned && represented == 3);
        assert(first_index == 1000 && last_index == 1512 && copies == 3);
        for (unsigned j = 0; j < 3; j++)
            assert(copied_bank[j] == 0 && copied_at[j] == ((16300+j*256)&RING_MASK));
    }
    const unsigned lengths[] = {0,255,256,512,12288,13152};
    for (unsigned k = 0; k < sizeof(lengths)/sizeof(*lengths); k++) {
        reset(); scalar_offer(0, 16300, lengths[k], 1000); drain();
        unsigned blocks = lengths[k] / 256;
        assert(result.ffts == blocks && !result.abandoned && represented == blocks);
        assert(copies == blocks);
        for (unsigned j = 0; j < blocks; j++)
            assert(copied_at[j] == ((16300+j*256)&RING_MASK));
    }
    /* Retire before the copy, during FFT/bins, and during frame encoding. */
    const unsigned phases[] = {SCALAR_COPY, SCALAR_FFT, SCALAR_BINS, SCALAR_ENCODE, SCALAR_FRAME};
    for (unsigned p = 0; p < sizeof(phases)/sizeof(*phases); p++)
        for (unsigned chunk = 0; chunk < 8; chunk++) {
            reset(); txq_head = 1;
            scalar_offer(0, 16000, 3*256+17, 1000);
            assert(scalar_next());
            unsigned phase = phases[p];
            if (phase == SCALAR_ENCODE || phase == SCALAR_FRAME) {
                reach(SCALAR_BINS);
                while (scalar.phase == SCALAR_BINS) scalar_work();
                assert(scalar.merged == 1); scalar_flush();
            }
            reach(phase);
            if (phase == SCALAR_BINS || phase == SCALAR_ENCODE)
                for (unsigned k = 0; k < chunk; k++) assert(scalar_work());
            int16_t saved[512]; memcpy(saved, fft_buf, sizeof(saved));
            scalar_retire(0); poison(0);
            assert(!memcmp(saved, fft_buf, sizeof(saved)));
            drain();
            assert(result.ffts + result.abandoned == 3);
            assert(result.ffts == (phase == SCALAR_COPY ? 0 : 1));
            assert(represented == result.ffts);
        }
    /* A private FFT survives reuse while a newer bank supplies more windows. */
    reset(); scalar_offer(0, 100, 3*256, 1000); assert(scalar_work());
    scalar_retire(0); poison(0); scalar_offer(1, 500, 3*256, 3000); drain();
    assert(result.ffts == 4 && result.abandoned == 2 && represented == 4);
    assert(copies == 4 && copied_bank[0] == 0);
    for (unsigned j = 1; j < 4; j++) assert(copied_bank[j] == 1);
    /* Multiple pending banks are consumed by sample index, not bank number. */
    reset(); scalar_offer(0, 0, 512, 2000); scalar_offer(1, 0, 512, 1000); drain();
    assert(copies == 4 && copied_bank[0] == 1 && copied_bank[2] == 0);
    /* Batch metadata retains the first window's gain through later copies. */
    reset(); txq_head = 1;
    banks[0][0] = 10u<<20; banks[0][256] = 20u<<20;
    scalar_offer(0, 0, 512, 1000); drain();
    spec_header_t *h = (void *)frame_out;
    assert(h->ffts == 2 && h->gain == 10 && h->pair_index == 1000 && h->pairs == 512);
    /* A maximum-sized mean batch retains all 47 significant sum bits. */
    reset(); txq_head = 1;
    scalar.phase = SCALAR_BINS; scalar.merged = 65534;
    for (unsigned j = 0; j < 256; j++) {
        fft_buf[2*j] = fft_buf[2*j+1] = INT16_MIN;
        powers.sum[j] = (UINT64_C(1)<<31)*65534;
    }
    drain(); h = (void *)frame_out;
    assert(h->ffts == 65535);
    for (unsigned j = 0; j < 256; j++) {
        assert(abs((int)frame_out[28+j] - 187) <= 1);
        assert(powers.sum[j] == 0);
    }
    return 0;
}
'''
        self.run_c(harness + work + helpers)
