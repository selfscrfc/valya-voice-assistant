"""Download a pinned MLX conversion of Whisper Turbo for offline inference."""
from pathlib import Path
from huggingface_hub import snapshot_download

root = Path.home() / 'Library/Application Support/wchat/models/whisper-large-v3-turbo'
snapshot_download('mlx-community/whisper-large-v3-turbo',
                  revision='a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb',
                  local_dir=root, allow_patterns=['config.json', 'weights.safetensors'])
print(root)
