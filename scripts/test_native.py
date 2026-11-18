#!/usr/bin/env python3
"""Run native state regressions without opening a window or connecting to Codex."""
import os
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
sdk = os.environ.get('SDKROOT') or str(Path(subprocess.check_output(['xcrun', '--show-sdk-path'], text=True).strip()).parent / 'MacOSX.sdk')
with tempfile.TemporaryDirectory(prefix='wchat-native-tests-') as directory:
    executable = Path(directory) / 'native-regression'
    subprocess.run(['swiftc', '-sdk', sdk, '-swift-version', '5', '-parse-as-library', '-D', 'WCHAT_TEST',
                    str(root / 'native/Wchat.swift'), str(root / 'tests/native_regression.swift'),
                    '-o', str(executable)], check=True, cwd=root)
    subprocess.run([str(executable)], check=True, cwd=root)
