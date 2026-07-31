"""
desktop.py — launches the Search Engine as a native desktop window.
Run with:  python desktop.py
"""
import os
import sys
import threading
import time
import webbrowser

# Ensure stdout/stderr use UTF-8 so unicode characters don't crash under pythonw.exe
try:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass
os.environ.setdefault('PYTHONIOENCODING', 'utf-8')

import webview
import uvicorn

os.environ["APP_MODE"] = "desktop"

# ── Windows: set a custom AppUserModelID so the taskbar shows our icon, not Python's
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SearchEngine.Desktop.1")
    except Exception:
        pass

from app import app as fastapi_app

HOST = "127.0.0.1"
PORT = 9191  # separate port so it doesn't clash with any dev server
APP_URL = f"http://{HOST}:{PORT}/"

ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")


def _inject_desktop_zoom_controls(window):
    js = r'''
        (() => {
            if (window.__desktopZoomInit) return;
            window.__desktopZoomInit = true;

            const MIN_ZOOM = 0.7;
            const MAX_ZOOM = 2.0;
            const STEP = 0.1;
            const STORE_KEY = 'desktop_ui_zoom';
            const SIDEBAR_STORE_KEY = 'desktop_assistant_width';
            const SIDEBAR_MIN = 420;
            const SIDEBAR_MAX_RATIO = 0.92;
            const SIDEBAR_STEP = 40;
            const SIDEBAR_DEFAULT = 520;

            const clamp = (v) => Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, Number(v) || 1));
            const load = () => {
                try {
                    const raw = localStorage.getItem(STORE_KEY);
                    if (!raw) return 1;
                    return clamp(parseFloat(raw));
                } catch (_) {
                    return 1;
                }
            };
            const save = (v) => {
                try { localStorage.setItem(STORE_KEY, String(v)); } catch (_) {}
            };

            const state = { zoom: load() };

            const sidebarClamp = (v) => {
                const viewport = Math.max(900, window.innerWidth || 1280);
                const maxWidth = Math.max(SIDEBAR_MIN, Math.floor(viewport * SIDEBAR_MAX_RATIO));
                return Math.max(SIDEBAR_MIN, Math.min(maxWidth, Number(v) || SIDEBAR_DEFAULT));
            };
            const loadSidebar = () => {
                try {
                    const raw = localStorage.getItem(SIDEBAR_STORE_KEY);
                    if (!raw) return SIDEBAR_DEFAULT;
                    return sidebarClamp(parseInt(raw, 10));
                } catch (_) {
                    return SIDEBAR_DEFAULT;
                }
            };
            const saveSidebar = (v) => {
                try { localStorage.setItem(SIDEBAR_STORE_KEY, String(v)); } catch (_) {}
            };
            const sidebarState = { width: loadSidebar() };

            const applyZoom = () => {
                const value = clamp(state.zoom);
                state.zoom = value;
                if (document.body) {
                    document.body.style.zoom = String(value);
                }
                document.documentElement.style.setProperty('--desktop-zoom-scale', String(value));
                if (zoomLabel) {
                    zoomLabel.textContent = Math.round(value * 100) + '%';
                }
                save(value);
            };

            const hostControls = document.getElementById('appSystemControls');
            const panel = document.createElement('div');
            panel.id = 'desktop-zoom-panel';
            panel.style.cssText = [
                'display:inline-flex',
                'align-items:center',
                'gap:8px',
                'margin-right:6px',
                'padding-right:6px',
                'border-right:1px solid rgba(255,255,255,.22)',
            ].join(';');

            const zoomRow = document.createElement('div');
            zoomRow.style.cssText = 'display:flex;align-items:center;gap:6px;';
            const sideRow = document.createElement('div');
            sideRow.style.cssText = 'display:flex;align-items:center;gap:6px;';

            const mkBtn = (label, title, onClick) => {
                const btn = document.createElement('button');
                btn.type = 'button';
                btn.textContent = label;
                btn.title = title;
                btn.style.cssText = [
                    'border:1px solid rgba(255,255,255,0.30)',
                    'background:rgba(255,255,255,0.16)',
                    'color:#fff',
                    'border-radius:10px',
                    'min-width:38px',
                    'height:38px',
                    'padding:0 8px',
                    'font-size:14px',
                    'font-weight:700',
                    'cursor:pointer',
                    'line-height:1',
                ].join(';');
                btn.addEventListener('click', onClick);
                return btn;
            };

            const zoomLabel = document.createElement('span');
            zoomLabel.style.cssText = 'color:#fff;font:600 12px/1.2 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;min-width:42px;text-align:center;';
            const sideLabel = document.createElement('span');
            sideLabel.style.cssText = 'color:#fff;font:600 12px/1.2 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;min-width:42px;text-align:center;';

            const zoomOut = mkBtn('A−', 'Zoom out (Ctrl + -)', () => {
                state.zoom = clamp(state.zoom - STEP);
                applyZoom();
            });
            const zoomIn = mkBtn('A+', 'Zoom in (Ctrl + +)', () => {
                state.zoom = clamp(state.zoom + STEP);
                applyZoom();
            });
            const zoomReset = mkBtn('Reset', 'Reset zoom (Ctrl + 0)', () => {
                state.zoom = 1;
                applyZoom();
            });

            const applySidebarWidth = () => {
                const width = sidebarClamp(sidebarState.width);
                sidebarState.width = width;
                document.documentElement.style.setProperty('--sidebar-width', width + 'px');
                if (sideLabel) sideLabel.textContent = width + 'px';
                saveSidebar(width);
            };

            const sideDown = mkBtn('◧−', 'Decrease assistant width (Ctrl+Alt+-)', () => {
                sidebarState.width = sidebarClamp(sidebarState.width - SIDEBAR_STEP);
                applySidebarWidth();
            });
            const sideUp = mkBtn('◨+', 'Increase assistant width (Ctrl+Alt+=)', () => {
                sidebarState.width = sidebarClamp(sidebarState.width + SIDEBAR_STEP);
                applySidebarWidth();
            });
            const sideReset = mkBtn('Panel', 'Reset assistant width (Ctrl+Alt+0)', () => {
                sidebarState.width = SIDEBAR_DEFAULT;
                applySidebarWidth();
            });

            zoomRow.appendChild(zoomOut);
            zoomRow.appendChild(zoomLabel);
            zoomRow.appendChild(zoomIn);
            zoomRow.appendChild(zoomReset);
            sideRow.appendChild(sideDown);
            sideRow.appendChild(sideLabel);
            sideRow.appendChild(sideUp);
            sideRow.appendChild(sideReset);
            panel.appendChild(zoomRow);
            panel.appendChild(sideRow);
            if (hostControls) {
                hostControls.insertBefore(panel, hostControls.firstChild);
            }

            document.addEventListener('keydown', (event) => {
                if (!(event.ctrlKey || event.metaKey)) return;
                const key = String(event.key || '').toLowerCase();
                if (key === '+' || key === '=') {
                    event.preventDefault();
                    state.zoom = clamp(state.zoom + STEP);
                    applyZoom();
                    return;
                }
                if (key === '-') {
                    event.preventDefault();
                    state.zoom = clamp(state.zoom - STEP);
                    applyZoom();
                    return;
                }
                if (key === '0') {
                    event.preventDefault();
                    state.zoom = 1;
                    applyZoom();
                    return;
                }
                if (!event.altKey) return;
                if (key === '+' || key === '=') {
                    event.preventDefault();
                    sidebarState.width = sidebarClamp(sidebarState.width + SIDEBAR_STEP);
                    applySidebarWidth();
                    return;
                }
                if (key === '-') {
                    event.preventDefault();
                    sidebarState.width = sidebarClamp(sidebarState.width - SIDEBAR_STEP);
                    applySidebarWidth();
                    return;
                }
                if (key === '0') {
                    event.preventDefault();
                    sidebarState.width = SIDEBAR_DEFAULT;
                    applySidebarWidth();
                }
            }, { passive: false });

            applyZoom();
            applySidebarWidth();
            window.addEventListener('resize', () => {
                applySidebarWidth();
            });

            const ensureControlsMounted = () => {
                const controls = document.getElementById('appSystemControls');
                const mounted = document.getElementById('desktop-zoom-panel');
                if (controls && mounted && mounted.parentElement !== controls) {
                    controls.insertBefore(mounted, controls.firstChild);
                }
            };
            ensureControlsMounted();
            setTimeout(ensureControlsMounted, 400);
            setTimeout(ensureControlsMounted, 1200);

            const keepFabVisibleWhenClosed = () => {
                const fab = document.getElementById('botFab');
                const side = document.getElementById('codeSide');
                if (!fab || !side) return;
                if (!side.classList.contains('bot-open') && fab.style.display === 'none') {
                    fab.style.display = '';
                }
            };
            keepFabVisibleWhenClosed();
            setInterval(keepFabVisibleWhenClosed, 1200);
        })();
        '''
    window.evaluate_js(js)


def _set_app_icon_win32(title: str = "Search Engine"):
    """Set the taskbar + title-bar icon to icon.ico on Windows via ctypes.
    Uses EnumWindows to reliably find the HWND even if the title isn't
    matched immediately by FindWindowW.
    """
    if not os.path.isfile(ICON_PATH):
        return
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        LR_LOADFROMFILE = 0x0010
        IMAGE_ICON = 1
        WM_SETICON = 0x0080
        ICON_SMALL = 0
        ICON_BIG   = 1

        hicon_big   = user32.LoadImageW(None, ICON_PATH, IMAGE_ICON, 256, 256, LR_LOADFROMFILE)
        hicon_small = user32.LoadImageW(None, ICON_PATH, IMAGE_ICON,  16,  16, LR_LOADFROMFILE)

        def _apply(hwnd):
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG,   hicon_big)
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_small)

        # --- attempt 1: fast FindWindowW ---
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            _apply(hwnd)
            return

        # --- attempt 2: EnumWindows — find a window whose title contains our app name ---
        pid = os.getpid()
        found = []
        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def _enum_cb(hwnd, _):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            if title.lower() in buf.value.lower():
                found.append(hwnd)
            return True

        user32.EnumWindows(EnumWindowsProc(_enum_cb), 0)
        for hwnd in found:
            _apply(hwnd)
    except Exception:
        pass


def _wire_desktop_window(window):
    def _on_loaded():
        try:
            _inject_desktop_zoom_controls(window)
        except Exception:
            pass
        try:
            _set_app_icon_win32(window.title)
        except Exception:
            pass
        # Retry after a short delay — the HWND may not be enumerable yet right at load time
        def _retry_icon():
            time.sleep(0.8)
            try:
                _set_app_icon_win32(window.title)
            except Exception:
                pass
        threading.Thread(target=_retry_icon, daemon=True).start()

    try:
        window.events.loaded += _on_loaded
    except Exception:
        pass


def _start_server():
    uvicorn.run(fastapi_app, host=HOST, port=PORT, log_level="warning")


def _wait_for_server(timeout: float = 15.0):
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(APP_URL, timeout=1)
            return True
        except Exception:
            time.sleep(0.15)
    return False


def _log(msg: str):
    """Write to a log file next to desktop.py — visible even under pythonw.exe."""
    try:
        log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "desktop_launch.log")
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


if __name__ == "__main__":
    import traceback as _tb
    _log("desktop.py starting")

    # start uvicorn in a background daemon thread
    server_thread = threading.Thread(target=_start_server, daemon=True)
    server_thread.start()
    _log("uvicorn thread started")

    # wait until the server accepts connections
    if not _wait_for_server():
        _log("ERROR: server did not start in time on port " + str(PORT))
        webbrowser.open(APP_URL)
    else:
        _log("server ready on " + APP_URL)

    try:
        window = webview.create_window(
            title="Search Engine",
            url=APP_URL,
            width=1280,
            height=820,
            resizable=True,
            min_size=(800, 600),
            maximized=True,
            background_color="#ffffff",
        )
        _wire_desktop_window(window)
        _log("calling webview.start")
        webview.start(gui="edgechromium")
        _log("webview.start returned (window closed)")
    except Exception as exc:
        err = _tb.format_exc()
        _log("Window error: " + str(exc) + "\n" + err)
        print(f"Desktop window failed to open: {exc}")
        print(f"Opening Search Engine in your browser instead: {APP_URL}")
        webbrowser.open(APP_URL)
        try:
            while server_thread.is_alive():
                time.sleep(1)
        except KeyboardInterrupt:
            pass
