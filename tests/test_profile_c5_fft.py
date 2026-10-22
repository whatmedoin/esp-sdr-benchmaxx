import contextlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import zlib

ROOT = Path(__file__).resolve().parents[1]


class FakePort:
    def __init__(self, profiling):
        self.profiling = profiling
        self.commands = []
        self.buffer = bytearray()
        self.n = 0
        self.closed = False

    def open(self):
        pass

    def close(self):
        self.closed = True

    def write(self, data):
        for line in data.decode().splitlines():
            if not line:
                continue
            self.commands.append(line)
            reply = 'OK'
            if line.startswith('SYNC '):
                reply = line
            elif line == 'INFO':
                reply = 'C5SDR 6 burst 16380'
            elif line == 'VERSION?':
                reply = 'VERSION {}'
            elif line == 'DC?':
                reply = 'DC 1'
            elif line == 'SPECPROFILE?':
                reply = ('SPECPROFILE ' + json.dumps({'n': self.n, 'ffts': 2, 'frames': 2})
                         if self.profiling else 'ERR command')
            elif line.startswith('SPEC '):
                _, ms, stride, units, det, rate, n, stats = line.split()
                self.n = int(n)
                self.buffer.extend(f'SPEC {n} 80000000 {n} 2412\n'.encode())
                for frame in range(2):
                    header = struct.pack('<4sIQIHBBHBB', b'SPC1', frame, frame * self.n,
                                         self.n, 1, 8 | int(det), 40, 0, self.n.bit_length() - 1, 2)
                    self.packet(header + bytes(self.n))
                stat = bytearray(36)
                stat[:4] = b'SPS1'
                stat[32:36] = (4000).to_bytes(4, 'little')
                self.packet(stat)
                reply = f'SPECEND 0 0 2 {2 * self.n} 500000 0 0 2 0 0 2 0'
            self.buffer.extend((reply + '\n').encode())
        return len(data)

    def packet(self, data):
        self.buffer.extend(data)
        self.buffer.extend(zlib.crc32(data).to_bytes(4, 'little'))

    def read(self, n):
        data = self.buffer[:n]
        del self.buffer[:n]
        return bytes(data)

    def readline(self):
        end = self.buffer.find(b'\n')
        return self.read(end + 1) if end >= 0 else b''


class ProfileC5FFT(unittest.TestCase):
    def run_tool(self, path, port, profile):
        fake_serial = types.SimpleNamespace(Serial=lambda **kwargs: port)
        with patch.dict(sys.modules, serial=fake_serial), patch.object(sys, 'path', [str(ROOT / 'tools')] + sys.path):
            spec = importlib.util.spec_from_file_location('profile_c5_fft_check', ROOT / 'docs/features/c5_fft_optim/profile_c5_fft.py')
            tool = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(tool)
            args = ['profile_c5_fft.py', '--port', 'fake', '--output', str(path),
                    '--milliseconds', '500', '--repeats', '1', '--rates', '0', '--sizes', '256']
            if profile:
                args.append('--profile')
            with patch.object(sys, 'argv', args), contextlib.redirect_stdout(io.StringIO()):
                tool.main()

    def test_normal_and_profile_capture_totals_and_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            for profile in (False, True):
                port = FakePort(profile)
                path = Path(tmp) / f'{profile}.json'
                self.run_tool(path, port, profile)
                runs = json.loads(path.read_text())['runs']
                self.assertEqual(len(runs), 2)
                self.assertTrue(all(r['ffts'] == 2 and r['fft_rates'] == [4000] for r in runs))
                self.assertTrue(all(('cycles' in r) == profile for r in runs))
                self.assertEqual(port.commands[-5:], ['GAIN HARDWARE', 'LPF AUTO', 'ALPF AUTO', 'DC 1', 'RELEASE'])
                self.assertTrue(port.closed)

    def test_missing_diagnostics_restores_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            port = FakePort(False)
            with self.assertRaisesRegex(RuntimeError, 'C5_SPECTRUM_PROFILE'):
                self.run_tool(Path(tmp) / 'missing.json', port, True)
            self.assertEqual(port.commands[-1], 'RELEASE')
            self.assertTrue(port.closed)
