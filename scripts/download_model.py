"""Download the official Russian model once; keep it outside the repository."""
from pathlib import Path
import tempfile
import urllib.request
import zipfile

root = Path.home() / "Library/Application Support/wchat/models"
name = "vosk-model-small-ru-0.22"
root.mkdir(parents=True, exist_ok=True)
if not (root / name / "am/final.mdl").is_file():
    with tempfile.TemporaryDirectory(dir=root) as temporary:
        archive = Path(temporary) / "model.zip"
        urllib.request.urlretrieve(f"https://alphacephei.com/vosk/models/{name}.zip", archive)
        with zipfile.ZipFile(archive) as source:
            for member in source.infolist():
                if not (Path(temporary) / member.filename).resolve().is_relative_to(Path(temporary).resolve()):
                    raise RuntimeError("Unsafe model archive")
            source.extractall(temporary)
        (Path(temporary) / name).rename(root / name)
print(f"Модель: {root / name}")
