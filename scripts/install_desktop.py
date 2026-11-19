"""Install the native launcher in the user's Applications folder."""
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = root / 'build/Wchat.app'
destination = Path.home() / 'Applications/Valya.app'
destination.parent.mkdir(parents=True, exist_ok=True)
if destination.exists():
    info = plistlib.loads((destination / 'Contents/Info.plist').read_bytes())
    if info.get('CFBundleIdentifier') != 'org.selfscrfc.wchat':
        raise RuntimeError(f'Refusing to replace a different app: {destination}')
with tempfile.TemporaryDirectory(prefix='.valya-install-', dir=destination.parent) as directory:
    staged = Path(directory) / 'Valya.app'
    shutil.copytree(source, staged)
    previous = Path(directory) / 'previous.app'
    if destination.exists():
        destination.rename(previous)
    try:
        staged.rename(destination)
    except BaseException:
        if previous.exists():
            previous.rename(destination)
        raise
register = Path('/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister')
if register.exists():
    subprocess.run([str(register), '-f', str(destination)], check=True)
print(destination)
