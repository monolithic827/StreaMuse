"""The desktop window(s). The main window is plain and framed - Windows draws its frame. The DJ
window is frameless with its own HTML titlebar (dj.html), matching the reference implementation's
design there; dj.html's titlebar carries pywebview's default drag-region class so it stays movable
with no extra wiring. Both windows share one CoreWebView2Environment by both coming from one
webview.start() call - two separately-initialized environments over the same user-data folder is a
documented conflict."""

import threading
import winreg

import webview

from . import paths

WIDTH = 1080
HEIGHT = 820

DJ_WIDTH = 460
DJ_HEIGHT = 620

DARK_GROUND = "#141517"
LIGHT_GROUND = "#f2f2f3"


class _MainWindowApi:
    """Exposed to the main panel's page as `pywebview.api`, so its "Open DJ" button can reveal the
    second window without the page needing to know anything about window management."""

    def __init__(self, dj_window) -> None:
        self._dj_window = dj_window

    def open_dj(self) -> None:
        self._dj_window.show()


def run(url: str, dj_url: str, settings) -> None:
    """Blocks until the user closes the main window, which is what ends the app."""
    ground_color = ground(settings.theme)

    dj_window = webview.create_window(
        "StreaMuse DJ",
        dj_url,
        width=DJ_WIDTH,
        height=DJ_HEIGHT,
        min_size=(DJ_WIDTH, DJ_HEIGHT),
        background_color=ground_color,
        hidden=True,
        frameless=True,
    )

    # js_api takes an object at construction time, but the DJ window's own API needs a reference to
    # the window itself for its titlebar's minimise/close buttons - expose() registers bare
    # functions after the fact instead, which sidesteps the chicken-and-egg problem.
    def minimize_dj() -> None:
        dj_window.minimize()

    def hide_dj() -> None:
        dj_window.hide()

    dj_window.expose(minimize_dj, hide_dj)

    # Closing hides rather than disposes, so reopening skips a WebView2 re-init stall - only the
    # main window's own close ends the app and tears everything down for real.
    dj_window.events.closing += _hide_instead_of_close(dj_window)

    main_window = webview.create_window(
        "StreaMuse",
        url,
        width=WIDTH,
        height=HEIGHT,
        min_size=(WIDTH, HEIGHT),
        background_color=ground_color,
        js_api=_MainWindowApi(dj_window),
    )

    # Docked to the main window's right edge rather than a free-floating window: matches its height
    # and rides along on every move or resize. moved/resized/shown all run this on their own thread
    # (webview.Event.set spawns one per firing), and move()/resize() reach the native window via
    # SetWindowPos, which - unlike most WinForms calls - is safe to invoke off the UI thread.
    dock = _dock_dj_window(main_window, dj_window)
    main_window.events.shown += dock
    main_window.events.moved += dock
    main_window.events.resized += dock

    webview.start(private_mode=False, storage_path=str(paths.WEBVIEW_DIR))


def _dock_dj_window(main_window, dj_window):
    """Matches height to the main window and sits immediately right of it - accepts *_args so the
    same handler works for shown() (no args), moved(x, y) and resized(width, height) alike.

    webview.Event.set() spawns a fresh thread per firing, and WinForms fires `moved` continuously
    while the window is being dragged - dozens of overlapping reposition threads with nothing
    serializing them. Without the lock below, an older thread can finish after a newer one and snap
    the DJ window back to a stale position, which is what dragging looked like before this: a
    constant teleport rather than a smooth follow. `busy.acquire(blocking=False)` drops a reposition
    entirely when one is already running instead of queuing it - safe to drop, since the one already
    in flight reads the window's position fresh at the moment it actually runs, not a value captured
    back when its event fired, so the drag's true final position is still what gets applied once
    dragging stops and the last `moved` event finds the lock free."""
    busy = threading.Lock()

    def reposition(*_args) -> None:
        if not busy.acquire(blocking=False):
            return
        try:
            dj_window.resize(dj_window.width, main_window.height)
            dj_window.move(main_window.x + main_window.width, main_window.y)
        finally:
            busy.release()

    return reposition


def _hide_instead_of_close(window):
    def handler():
        window.hide()
        return False

    return handler


def ground(theme: str) -> str:
    """What shows before the page's first paint and wherever WebView2 lags a resize."""
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
