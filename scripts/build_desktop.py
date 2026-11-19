#!/usr/bin/env python3
import os
import platform
import pathlib
import plistlib
import subprocess

root = pathlib.Path(__file__).resolve().parents[1]
app = root / 'build/Wchat.app/Contents'
(app / 'MacOS').mkdir(parents=True, exist_ok=True)
with (app / 'Info.plist').open('wb') as stream:
    plistlib.dump({
        'CFBundleExecutable': 'Wchat', 'CFBundleIdentifier': 'org.selfscrfc.wchat',
        'CFBundleName': 'Valya', 'CFBundleDisplayName': 'Valya', 'CFBundleDevelopmentRegion': 'en', 'CFBundlePackageType': 'APPL', 'CFBundleVersion': '1',
        'CFBundleShortVersionString': '0.2.0', 'LSMinimumSystemVersion': '14.0',
        'NSMicrophoneUsageDescription': 'Listens for the wake word “чат” and transcribes commands locally.',
        'WChatProjectRoot': str(root), 'NSHighResolutionCapable': True,
    }, stream)
sdk = os.environ.get('SDKROOT') or str(pathlib.Path(subprocess.check_output(['xcrun', '--show-sdk-path'], text=True).strip()).parent / 'MacOSX.sdk')
subprocess.run(['swiftc', '-sdk', sdk, '-swift-version', '5', '-parse-as-library', '-O',
                '-target', platform.machine() + '-apple-macosx14.0', str(root / 'native/Wchat.swift'),
                '-o', str(app / 'MacOS/Wchat')], check=True)
subprocess.run(['codesign', '--force', '--sign', '-', str(app.parent)], check=True)
print(app.parent)
