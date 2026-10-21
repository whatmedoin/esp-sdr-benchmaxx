#!/usr/bin/env python3
"""Run a controlled C5/C6 scalar spectrum matrix without flashing firmware."""
import argparse
import json
from pathlib import Path
import time
import zlib
import serial
from check_spectrum import check, command, exact, synchronize


def probe(port, milliseconds, rate, n, detector, stats, stop_after=None, pause_before_read=0,
          identity='C6SDR 6 burst 16380'):
    header = command(port, f'SPEC {milliseconds} 1 1 {detector} {rate} {n} {stats}')
    fs = {0: 80000000, 1: 40000000, 2: 20000000, 3: 10000000,
          4: 8000000, 5: 4000000, 6: 16000000}[rate]
    if header.split()[:3] != ['SPEC', str(n), str(fs)]:
        raise RuntimeError(header)
    start = time.monotonic()
    if pause_before_read:
        time.sleep(pause_before_read)
    frames = ffts = statistics = 0
    previous = -1
    stopped = False
    while True:
        if stop_after is not None and not stopped and time.monotonic() - start >= stop_after:
            port.write(b'\n')
            stopped = True
        magic = exact(port, 4)
        if magic == b'SPEC':
            end = (magic + port.readline()).decode('ascii').strip().split()
            if len(end) != 13 or end[0] != 'SPECEND':
                raise RuntimeError(end)
            report = [int(v) for v in end[1:]]
            break
        if magic not in (b'SPC1', b'SPS1'):
            raise RuntimeError(f'Lost framing: {magic!r}')
        frame = magic + exact(port, 36 if magic == b'SPS1' else n + 28)
        if zlib.crc32(frame[:-4]) != int.from_bytes(frame[-4:], 'little'):
            raise RuntimeError('CRC mismatch')
        if magic == b'SPS1':
            if not stats:
                raise RuntimeError('Unrequested statistics')
            statistics += 1
            continue
        index = int.from_bytes(frame[8:16], 'little')
        count = int.from_bytes(frame[20:22], 'little')
        if (int.from_bytes(frame[4:8], 'little') != frames or index <= previous or not count or
                int.from_bytes(frame[16:20], 'little') < count * n or
                frame[22] & 1 != detector or bool(frame[22] & 8) != (identity.startswith('C5SDR ') or n != 256) or
                frame[26] != n.bit_length() - 1 or frame[27] != 2):
            raise RuntimeError('Invalid frame metadata')
        previous = index
        frames += 1
        ffts += count
    if (report[0] or report[7] != frames or report[8] or report[10] != ffts or
            bool(report[11]) != stopped):
        raise RuntimeError(f'Invalid end report: {report}, frames={frames}, FFTs={ffts}')
    if command(port, 'INFO') != identity:
        raise RuntimeError('Command access failed after capture')
    return {'milliseconds': milliseconds, 'rate': rate, 'n': n, 'detector': detector,
            'stats': stats, 'stop_after': stop_after, 'pause_before_read': pause_before_read,
            'frames': frames, 'ffts': ffts,
            'statistics': statistics, 'report': report}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True)
    parser.add_argument('--milliseconds', type=int, default=2000)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--stress', action='store_true', help='Also check short captures and host stops')
    args = parser.parse_args()
    if not 500 <= args.milliseconds <= 60000:
        parser.error('--milliseconds must be between 500 and 60000')
    if args.output.exists():
        parser.error('Choose a new output path')
    port = serial.Serial(port=None, baudrate=2000000, timeout=5)
    port.dtr = port.rts = False
    port.port = args.port
    port.open()
    try:
        synchronize(port)
        identity = command(port, 'INFO')
        if not identity.startswith(('C5SDR ', 'C6SDR ')):
            raise RuntimeError(f'Expected C5SDR or C6SDR, got {identity}')
        c5 = identity.startswith('C5SDR ')
        rates = (0, 1, 2, 3, 4, 5) if c5 else (0, 1, 6)
        version = command(port, 'VERSION?')
        dc = command(port, 'DC?').split()[1]
        settings = ['FREQ 2412', 'GAIN MANUAL 40', 'ALPF AUTO', 'DC 1']
        if c5:
            settings.append('LPF AUTO')
        for setting in settings:
            reply = command(port, setting)
            if reply != 'OK':
                raise RuntimeError(f'{setting}: {reply}')
        result = check(port, args.milliseconds, stats=True)
        result.update(version=version, settings=settings, milliseconds=args.milliseconds)
        for run in result['runs']:
            report = run['report']
            if run['ffts'] != report[10] or run['frames'] != report[7]:
                raise RuntimeError(f'Frame/end count mismatch: {run}')
            if report[8]:
                raise RuntimeError(f'Dropped spectrum frames: {run}')
        if args.stress:
            result['stress'] = []
            for mode in (0, 1):
                if command(port, f'DC {mode}') != 'OK':
                    raise RuntimeError('DC command failed')
                for rate in rates:
                    for n in (256, 512, 1024, 2048):
                        for detector in (0, 1):
                            run = probe(port, 5, rate, n, detector, 0, identity=identity)
                            run['dc'] = mode
                            result['stress'].append(run)
                    for n in (256, 1024):
                        for detector in (0, 1):
                            for delay in (0, .03):
                                run = probe(port, 0, rate, n, detector, 1, delay, identity=identity)
                                run['dc'] = mode
                                result['stress'].append(run)
            for rate in rates:
                for detector in (0, 1):
                    result['stress'].append(probe(port, 5000, rate, 256, detector, 1, identity=identity))
            for rate, n in [(0, 256), (1, 256), (rates[-1], 256), (0, 1024)]:
                for detector in (0, 1):
                    result['stress'].append(probe(port, 800, rate, n, detector, 1,
                                                 pause_before_read=.35, identity=identity))
            result['raw'] = []
            for rate in rates:
                for n in (257, 16380):
                    for bits in (16, 20, 32):
                        tag = 'CAP' if bits == 32 else f'CAP{bits}'
                        header = command(port, f'{tag} {n} {rate}').split()
                        if len(header) != 4 or header[:2] != ['DATA', str(n)]:
                            raise RuntimeError(header)
                        data = exact(port, (n * bits + 7) // 8)
                        if zlib.crc32(data) != int(header[2], 16):
                            raise RuntimeError('Raw capture CRC mismatch')
                        result['raw'].append({'rate': rate, 'n': n, 'bits': bits})
        for setting in ['GAIN HARDWARE', 'ALPF AUTO', f'DC {dc}', 'RELEASE']:
            if command(port, setting) != 'OK':
                raise RuntimeError(f'Could not restore {setting}')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + '\n')
        for run in result['runs']:
            fs, rate, n = run['profile'][:3]
            stats = run['stats']
            fps = sum(s['ffts_per_s'] for s in stats) / len(stats)
            print(f'{fs/1e6:g} MS/s {n:4} bins detector {run["detector"]}: '
                  f'{fps:.0f} FFT/s, {100*fps*n/fs:.3f}% coverage')
        print(args.output)
    finally:
        port.close()


if __name__ == '__main__':
    main()
