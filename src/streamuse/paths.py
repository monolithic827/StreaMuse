"""Well-known directories. Config is roaming, binaries and HLS output are machine-local."""

import os
from pathlib import Path

CONFIG_DIR = Path(os.environ["APPDATA"]) / "StreaMuse"
DATA_DIR = Path(os.environ["LOCALAPPDATA"]) / "StreaMuse"
BIN_DIR = DATA_DIR / "bin"
HLS_DIR = DATA_DIR / "hls"
WEBVIEW_DIR = DATA_DIR / "webview"
LIBRESPOT_DIR = DATA_DIR / "librespot"

SETTINGS_PATH = CONFIG_DIR / "settings.json"


def wwwroot() -> Path:
    """The panel and listener assets, whether running from source or from a frozen bundle."""
    return Path(__file__).parent / "wwwroot"


def ensure() -> None:
    for directory in (CONFIG_DIR, DATA_DIR, BIN_DIR, HLS_DIR):
        directory.mkdir(parents=True, exist_ok=True)
