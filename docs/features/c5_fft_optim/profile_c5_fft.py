#!/usr/bin/env python3
"""Compare C5 FFT throughput and optional diagnostic stage-cycle counters."""
import argparse
import json
from pathlib import Path
import statistics
import sys
import serial

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'c6_fft_optim'))
from check_c6_fft import probe, command, synchronize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--milliseconds', type=int, default=1500)
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--rates', type=int, nargs='+', default=[0, 5], choices=range(6))
    parser.add_argument('--sizes', type=int, nargs='+', default=[256, 512, 1024, 2048],
                        choices=[256, 512, 1024, 2048])
    parser.add_argument('--profile', action='store_true', help='Require C5_SPECTRUM_PROFILE firmware')
    args = parser.parse_args()
    if args.output.exists() or not 500 <= args.milliseconds <= 60000 or not 1 <= args.repeats <= 20:
        parser.error('Choose a new output path, 500..60000 ms and 1..20 repeats')
    port = serial.Serial(port=None, baudrate=2000000, timeout=5)
    port.dtr = port.rts = False
    port.port = args.port
    port.open()
    dc = None
    result = {'runs': []}
    try:
        synchronize(port)
        identity = command(port, 'INFO')
        if not identity.startswith('C5SDR '):
            raise RuntimeError(identity)
        result.update(identity=identity, version=command(port, 'VERSION?'))
        dc = command(port, 'DC?').split()[1]
        result['settings'] = ['FREQ 2412', 'GAIN MANUAL 40', 'LPF AUTO', 'ALPF AUTO', 'DC 1']
        for setting in result['settings']:
            if command(port, setting) != 'OK':
                raise RuntimeError(setting)
        if args.profile and not command(port, 'SPECPROFILE?').startswith('SPECPROFILE '):
            raise RuntimeError('Build with -DC5_SPECTRUM_PROFILE=ON first')
        for repeat in range(args.repeats):
            for rate in args.rates:
                for n in args.sizes:
                    for detector in (0, 1):
                        run = probe(port, args.milliseconds, rate, n, detector, 1, identity=identity)
                        run['repeat'] = repeat
                        if not run['fft_rates']:
                            raise RuntimeError('Missing throughput telemetry')
                        if args.profile:
                            line = command(port, 'SPECPROFILE?')
                            if not line.startswith('SPECPROFILE '):
                                raise RuntimeError(line)
                            run['cycles'] = json.loads(line.split(' ', 1)[1])
                            if run['cycles']['ffts'] != run['ffts'] or run['cycles']['frames'] != run['frames']:
                                raise RuntimeError('Profile totals do not match the capture')
                        result['runs'].append(run)
                        print(f'{repeat + 1}: rate {rate}, {n} bins, detector {detector}: '
                              f'{statistics.mean(run["fft_rates"]):.1f} FFT/s', flush=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + '\n')
    finally:
        try:
            if dc is not None:
                for setting in ['GAIN HARDWARE', 'LPF AUTO', 'ALPF AUTO', f'DC {dc}', 'RELEASE']:
                    if command(port, setting) != 'OK':
                        raise RuntimeError(f'Could not restore {setting}')
        finally:
            port.close()


if __name__ == '__main__':
    main()
