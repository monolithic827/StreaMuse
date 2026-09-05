"""The desktop window. A plain framed window: the panel is a web page and Windows draws the frame."""

import winreg

import webview

from . import paths

WIDTH = 1080
HEIGHT = 820

DARK_GROUND = "#141517"
LIGHT_GROUND = "#f2f2f3"


def run(url: str, settings) -> None:
    """Blocks until the user closes the window, which is what ends the app."""
    webview.create_window(
        "StreaMuse",
        url,
        width=WIDTH,
        height=HEIGHT,
        min_size=(WIDTH, HEIGHT),
        background_color=ground(settings.theme),
    )
    webview.start(private_mode=False, storage_path=str(paths.WEBVIEW_DIR))


def ground(theme: str) -> str:
    """What shows before the page's first paint and wherever the view lags a resize."""
    dark = theme == "Dark" or (theme == "Auto" and _windows_prefers_dark())
    return DARK_GROUND if dark else LIGHT_GROUND


def _windows_prefers_dark() -> bool:
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False
