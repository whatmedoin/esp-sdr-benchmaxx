"""Exercise production snapshot accumulation/encoding with deterministic FFT output."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SnapshotSpectrum(unittest.TestCase):
    @unittest.skipUnless(shutil.which('cc'), 'Host C compiler unavailable')
    def test_mean_max_bin_order_and_frame_counts(self):
        source = (ROOT / 'main/common/spectrum.c').read_text()
        sdk_headers = ['esp_cpu.h', 'dsps_fft2r.h', 'esp_rom_crc.h', 'esp_timer.h',
                       'freertos/FreeRTOS.h', 'freertos/task.h', 'spectrum_fft.h', 'esp_heap_caps.h']
        for header in sdk_headers:
            source = source.replace(f'#include "{header}"', '')
        stub = r'''
#include <assert.h>
#include <math.h>
#include <stdint.h>
#include <string.h>
#include "spectrum_stats.h"
#include "burst_serial.h"
#define ESP_OK 0
#define MALLOC_CAP_INTERNAL 1
#define MALLOC_CAP_DMA 2
#define MALLOC_CAP_8BIT 4
static unsigned fail_alloc, fail_after;
static void *heap_caps_malloc(size_t n,unsigned caps){assert(caps==7);return fail_alloc?NULL:malloc(n);}
unsigned spectrum_dc_mode;
static unsigned transforms,frames,wanted_units,wanted_n,wanted_detector,full_scale,stop_after, release_at;
static unsigned batch_start, acquisitions;
static uint64_t previous_index;
static void verify_input(const int16_t *x, unsigned n);
static char replies[4096];
static uint32_t words[16384];
static int16_t *dsps_fft_w_table_sc16;
static uint32_t esp_cpu_get_cycle_count(void){return 0;}
static int64_t esp_timer_get_time(void){static int64_t t;return t+=1000;}
static void vTaskDelay(unsigned ticks){}
static unsigned dsps_fft2r_init_sc16(int16_t *table,unsigned n){return ESP_OK;}
static void dsps_fft2r_sc16_ansi(int16_t *x,unsigned n){
    verify_input(x,n);
    for(unsigned j=0;j<n;j++){x[2*j]=full_scale?INT16_MIN:(j%97+1)*(transforms+1);x[2*j+1]=full_scale?INT16_MIN:0;}
    transforms++;
}
static void spectrum_fft_poll(int16_t *x,unsigned n,const int16_t *w,bool (*poll)(void*),void *context){
 dsps_fft2r_sc16_ansi(x,n);if(poll)poll(context);
}
static uint32_t esp_rom_crc32_le(uint32_t c,const void *p,unsigned n){return 0;}
burst_serial_port_t burst_serial_port(void){return BURST_SERIAL_USB;}
bool burst_serial_stop_requested(void){return stop_after && transforms>=stop_after;}
void spectrum_stats_init(spectrum_stats_t *s){memset(s,0,sizeof(*s));}
void spectrum_stats_emit(spectrum_stats_t *s,unsigned n,unsigned fs,uint32_t ffts,
 unsigned abandoned,uint32_t drops,uint32_t late,unsigned queue,bool (*send)(const void*,size_t)){}
'''
        check = r'''
static unsigned test_reverse(unsigned x,unsigned bits){unsigned r=0;while(bits--){r=r*2+(x&1);x>>=1;}return r;}
static bool acquire(unsigned n,unsigned rate,const uint32_t **p,unsigned *us){
#if CONFIG_IDF_TARGET_ESP32C5
 assert(n==(16380u/wanted_n)*wanted_n);
#else
 assert(n==wanted_n);
#endif
 acquisitions++;
 if(fail_after && acquisitions>fail_after)return false;
 for(unsigned j=0;j<n;j++){
  unsigned i=(j*73u+transforms)&1023u, q=(j*137u+511u)&1023u;
  words[j]=i|(q<<10)|(((transforms+j/wanted_n)%85u)<<20);
 }
 words[n]=0x5a5aa5a5;*p=words;*us=1;return true;
}
static void verify_input(const int16_t *x,unsigned n){
#if CONFIG_IDF_TARGET_ESP32C5
 unsigned at=(transforms%((16380u/n)))*n;
#else
 unsigned at=0;
#endif
 for(unsigned j=0;j<n;j++){
  uint32_t w=words[at+j];
  int i=(int)(w<<22)>>22,q=(int)(w<<12)>>22;
  assert(x[2*j]==(i*window[j]>>9));assert(x[2*j+1]==(q*window[j]>>9));
 }
}
bool burst_serial_send(const void *data,size_t size){
 const uint8_t *b=data;
 if(size<4||memcmp(b,"SPC1",4)){
  assert(strlen(replies)+size<sizeof(replies));
  strncat(replies,data,size);return true;
 }
 frames++;assert(size==wanted_n+32);
 wanted_units=b[20]|b[21]<<8;assert(wanted_units>0);
 assert((b[20]|b[21]<<8)==wanted_units);
 assert((b[22]&1)==wanted_detector);assert(b[22]&8);assert(b[27]==2);
 assert(b[23]==batch_start%85);
 uint64_t index;memcpy(&index,b+8,8);
 if(frames>1)assert(index>previous_index);
 previous_index=index;
 unsigned pairs=b[16]|b[17]<<8|b[18]<<16|b[19]<<24;
 assert(pairs==wanted_n*wanted_units);
 for(unsigned j=1;j<wanted_n;j++){
  if(j==wanted_n/2||j==wanted_n-1)continue; /* DC correction neighbors */
  double p=0;
  for(unsigned u=1;u<=wanted_units;u++){
   double v=(j%97+1)*(u+batch_start);v=full_scale?2147483648.0:v*v;
   p=wanted_detector?fmax(p,v):p+v;
  }
  if(!wanted_detector)p/=wanted_units;
  int expected=(int)lrint(20*log10(p));if(expected>255)expected=255;
  assert(abs((int)b[28+test_reverse(j,b[26])]-expected)<=1);
 }
 batch_start+=wanted_units;return true;
}
size_t burst_serial_try_send(const void *data,size_t size){
 if(transforms<release_at)return 0;
 assert(burst_serial_send(data,size));return size;
}
int main(void){
 assert(spectrum_command("SPECINFO?",2412,acquire));
 assert(strstr(replies,"[80000000,0,2048,1,1,0]"));
#if CONFIG_IDF_TARGET_ESP32C61
 assert(strstr(replies,"[4000000,5,2048,1,1,0]"));
#endif
#if CONFIG_IDF_TARGET_ESP32C61 || CONFIG_IDF_TARGET_ESP32C6
 assert(strstr(replies,"[80000000,0,256,1,1,1]"));
#else
 assert(strstr(replies,"[80000000,0,256,1,1,0]"));
#endif
 replies[0]=0;
 assert(spectrum_command("SPEC 1 1 1 0 0 4096",2412,acquire));
 assert(!strcmp(replies,"ERR spec_args\n"));
#if CONFIG_IDF_TARGET_ESP32C5
 fail_alloc=1;replies[0]=0;wanted_n=256;
 assert(spectrum_command("SPEC 1 1 1 0 0 256",2412,acquire));
 assert(!strcmp(replies,"ERR spectrum_memory\n"));assert(!acquisitions && !transforms);
 fail_alloc=0;replies[0]=0;
 assert(spectrum_command("SPEC 1 1 1 0 0 256",2412,acquire));
 assert(transforms==1 && acquisitions==1 && batch_start==1);
 assert(strstr(replies,"SPECEND 0 0 1 256 "));
 batch_start=transforms=frames=acquisitions=0;replies[0]=0;stop_after=70;fail_after=1;
 assert(spectrum_command("SPEC 0 1 1 0 0 256",2412,acquire));
 assert(transforms==63 && batch_start==63 && acquisitions==2);
 assert(strstr(replies,"SPECEND 1 0 63 16128 "));
 fail_after=0;
#endif
 for(full_scale=0;full_scale<=1;full_scale++)
 for(wanted_n=256;wanted_n<=2048;wanted_n*=2)
 for(unsigned legacy_units=1;legacy_units<=8;legacy_units++)
 for(wanted_detector=0;wanted_detector<=1;wanted_detector++)
 for(release_at=0;release_at<=5;release_at+=5)
 for(stop_after=6;stop_after<=70;stop_after+=64){
  batch_start=transforms=frames=acquisitions=0;previous_index=0;replies[0]=0;char command[64];
  snprintf(command,sizeof(command),"SPEC 0 1 %u %u 0 %u",legacy_units,wanted_detector,wanted_n);
  assert(spectrum_command(command,2412,acquire));
  assert(transforms==stop_after && batch_start==stop_after);
  assert(frames==stop_after-(release_at?3:0));
#if CONFIG_IDF_TARGET_ESP32C5
  unsigned depth=16380u/wanted_n;
  assert(acquisitions==(stop_after+depth-1)/depth);
#else
  assert(acquisitions==stop_after);
#endif
 }
 replies[0]=0;
#if C5_SPECTRUM_PROFILE
 assert(spectrum_command("SPECPROFILE?",2412,acquire));
 assert(strstr(replies,"SPECPROFILE {\"n\":2048,\"ffts\":70,\"frames\":67,"));
 assert(strstr(replies,"\"fft_tx\":0}"));
#else
 assert(!spectrum_command("SPECPROFILE?",2412,acquire));
#endif
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / 'snapshot.c'
            src.write_text('#include <stdlib.h>\n' + stub + source + check)
            exe = Path(tmp) / 'snapshot'
            for target, profile in [('ESP32C3', 0), ('ESP32C61', 0), ('ESP32C6', 0),
                                    ('ESP32C5', 0), ('ESP32C5', 1)]:
                with self.subTest(target=target, profile=profile):
                    subprocess.run(['cc', '-std=gnu11', '-O2', '-fsanitize=address,undefined',
                                    '-fno-sanitize-recover=all',
                                    f'-DCONFIG_IDF_TARGET_{target}=1', f'-DC5_SPECTRUM_PROFILE={profile}',
                                    '-I' + str(ROOT / 'main/common'), str(src), '-lm', '-o', str(exe)], check=True)
                    subprocess.run([str(exe)], check=True)
