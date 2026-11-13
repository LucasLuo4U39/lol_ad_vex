"""LoLAdvisor 左下角悬浮窗（Windows / 纯 ctypes，无第三方依赖）。

设计目标：
- 对局中不切屏就能看到当前胜率、趋势、对局时间与一条建议；
- 半透明、置顶、鼠标穿透（点击穿透到游戏，不会抢焦点）；
- 纯外部窗口：不注入游戏进程、不装钩子，和 OBS 的窗口叠加同一类做法；
- 游戏必须是无边框/窗口模式（独占全屏时任何叠加层都不可见）。

用法：
    python overlay.py --demo           # 用假数据预览 8 秒（截图/调样式用）
    from overlay import OverlayController
    ctl = OverlayController(provider)  # provider() -> dict | None；None 表示隐藏
    ctl.start(); ...; ctl.stop()
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import sys
import threading
import time
from typing import Any, Callable, Dict, Optional

IS_WINDOWS = sys.platform == "win32"

# ------------------------------------------------------------------ Win32 常量
WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000

SW_SHOWNOACTIVATE = 8
SW_HIDE = 0
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

LWA_ALPHA = 0x2
SRCCOPY = 0x00CC0020
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_PAINT = 0x000F
WM_ERASEBKGND = 0x0014
WM_TIMER = 0x0113
DT_LEFT = 0x0000
DT_RIGHT = 0x0002
DT_SINGLELINE = 0x0020
DT_VCENTER = 0x0004
DT_END_ELLIPSIS = 0x8000
TRANSPARENT_BK = 1
CLEARTYPE_QUALITY = 5
DEFAULT_CHARSET = 1
FF_DONTCARE = 0
DEFAULT_PITCH = 0
FW_BOLD = 700
FW_NORMAL = 400

# 视觉参数（物理像素）
WIDTH = 392
HEIGHT = 152
MARGIN = 22
CORNER = 18
ALPHA = 224  # 0-255，越小越透明


def _bgr(rgb: int) -> int:
    """0xRRGGBB -> COLORREF(0x00BBGGRR)。"""
    r = (rgb >> 16) & 0xFF
    g = (rgb >> 8) & 0xFF
    b = rgb & 0xFF
    return (b << 16) | (g << 8) | r


COLOR_BG = _bgr(0x0B0E17)
COLOR_TRACK = _bgr(0x1B2230)
COLOR_TEXT = _bgr(0xF2F5F8)
COLOR_FAINT = _bgr(0x8B95A7)
COLOR_GOOD = _bgr(0x3AD966)
COLOR_BAD = _bgr(0xF5544A)
COLOR_AMBER = _bgr(0xF5B336)
COLOR_LINE = _bgr(0x33405A)


if IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM
    )
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_uint),
            ("style", ctypes.c_uint),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
            ("hIconSm", wintypes.HICON),
        ]

    class PAINTSTRUCT(ctypes.Structure):
        _fields_ = [
            ("hdc", wintypes.HDC),
            ("fErase", wintypes.BOOL),
            ("rcPaint", wintypes.RECT),
            ("fRestore", wintypes.BOOL),
            ("fIncUpdate", wintypes.BOOL),
            ("rgbReserved", ctypes.c_byte * 32),
        ]

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    # --- 显式声明原型：64 位下句柄不能用默认 c_int，否则返回/传参会溢出或被截断 ---
    H = ctypes.c_void_p
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
    user32.RegisterClassExW.restype = ctypes.c_ushort
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        H, H, H, ctypes.c_void_p,
    ]
    user32.CreateWindowExW.restype = H
    user32.SetLayeredWindowAttributes.argtypes = [H, wintypes.DWORD, ctypes.c_ubyte, wintypes.DWORD]
    user32.SetWindowRgn.argtypes = [H, H, wintypes.BOOL]
    user32.SetTimer.argtypes = [H, ctypes.c_size_t, ctypes.c_uint, ctypes.c_void_p]
    user32.KillTimer.argtypes = [H, ctypes.c_size_t]
    user32.ShowWindow.argtypes = [H, ctypes.c_int]
    user32.SetWindowPos.argtypes = [H, H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    user32.InvalidateRect.argtypes = [H, ctypes.c_void_p, wintypes.BOOL]
    user32.BeginPaint.argtypes = [H, ctypes.POINTER(PAINTSTRUCT)]
    user32.BeginPaint.restype = H
    user32.EndPaint.argtypes = [H, ctypes.POINTER(PAINTSTRUCT)]
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), H, ctypes.c_uint, ctypes.c_uint]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.PostMessageW.argtypes = [H, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
    user32.DestroyWindow.argtypes = [H]
    user32.DefWindowProcW.argtypes = [H, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.LoadCursorW.argtypes = [H, ctypes.c_void_p]
    user32.LoadCursorW.restype = H
    user32.GetWindowRect.argtypes = [H, ctypes.POINTER(RECT)]
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = H
    user32.IsWindowVisible.argtypes = [H]
    user32.IsIconic.argtypes = [H]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.GetWindowTextLengthW.argtypes = [H]
    user32.GetWindowTextW.argtypes = [H, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.argtypes = [H, wintypes.LPWSTR, ctypes.c_int]
    user32.FillRect.argtypes = [H, ctypes.POINTER(RECT), H]
    user32.DrawTextW.argtypes = [H, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(RECT), ctypes.c_uint]

    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = H

    gdi32.CreateCompatibleDC.argtypes = [H]
    gdi32.CreateCompatibleDC.restype = H
    gdi32.CreateCompatibleBitmap.argtypes = [H, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = H
    gdi32.SelectObject.argtypes = [H, H]
    gdi32.SelectObject.restype = H
    gdi32.DeleteObject.argtypes = [H]
    gdi32.DeleteDC.argtypes = [H]
    gdi32.CreateSolidBrush.argtypes = [wintypes.DWORD]
    gdi32.CreateSolidBrush.restype = H
    gdi32.CreateFontW.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR,
    ]
    gdi32.CreateFontW.restype = H
    gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
    gdi32.CreateRoundRectRgn.restype = H
    gdi32.SetBkMode.argtypes = [H, ctypes.c_int]
    gdi32.SetBkMode.restype = ctypes.c_int
    gdi32.SetTextColor.argtypes = [H, wintypes.DWORD]
    gdi32.SetTextColor.restype = wintypes.DWORD
    gdi32.BitBlt.argtypes = [H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, H, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi32.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi32.CreatePen.restype = H
    gdi32.GetStockObject.argtypes = [ctypes.c_int]
    gdi32.GetStockObject.restype = H
    gdi32.RoundRect.argtypes = [H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    gdi32.RoundRect.restype = wintypes.BOOL

GAME_WINDOW_TITLES = ("League of Legends", "英雄联盟")


def _valid_game_window(hwnd: int) -> bool:
    """可见、未最小化、尺寸合理（排除 -32000 的最小化坐标）。"""
    if not hwnd or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
        return False
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    return width >= 640 and height >= 480 and rect.left > -30000 and rect.top > -30000


def _find_game_window() -> Optional[int]:
    """找英雄联盟对局窗口；找不到返回 None。"""
    if not IS_WINDOWS:
        return None
    for cls in ("RiotWindowClass", "League of Legends (TM) Client"):
        hwnd = user32.FindWindowW(cls, None)
        if hwnd and _valid_game_window(int(hwnd)):
            return int(hwnd)
    found: list[int] = []

    @WNDENUMPROC
    def _cb(hwnd, _lp):
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value.strip() in GAME_WINDOW_TITLES and _valid_game_window(int(hwnd)):
                found.append(int(hwnd))
        return True

    user32.EnumWindows(_cb, 0)
    return found[0] if found else None


class OverlayWindow:
    """半透明置顶悬浮窗（鼠标穿透）。"""

    def __init__(self, provider: Callable[[], Optional[Dict[str, Any]]]):
        self.provider = provider
        self.hwnd = 0
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wndproc = None
        self._data: Optional[Dict[str, Any]] = None
        self._pos: tuple[int, int] = (MARGIN, 400)
        self._visible = False

    # ---------------------------------------------------------- 生命周期
    def start(self) -> None:
        if not IS_WINDOWS:
            return
        self._thread = threading.Thread(target=self._run, name="overlay", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if IS_WINDOWS and self.hwnd:
            try:
                user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    # ---------------------------------------------------------- 线程内
    def _run(self) -> None:
        try:
            user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        except Exception:  # noqa: BLE001
            pass

        hinstance = kernel32.GetModuleHandleW(None)
        class_name = "LoLAdvisorOverlayWnd"
        self._wndproc = WNDPROC(self._wnd_proc)

        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = 0
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinstance
        wc.hCursor = user32.LoadCursorW(0, ctypes.c_void_p(32512))  # IDC_ARROW
        wc.hbrBackground = 0
        wc.lpszClassName = class_name
        user32.RegisterClassExW(ctypes.byref(wc))

        ex_style = (
            WS_EX_TOPMOST
            | WS_EX_LAYERED
            | WS_EX_TRANSPARENT
            | WS_EX_TOOLWINDOW
            | WS_EX_NOACTIVATE
        )
        x, y = self._position()
        self.hwnd = user32.CreateWindowExW(
            ex_style, class_name, "LoLAdvisor Overlay", WS_POPUP,
            x, y, WIDTH, HEIGHT, None, None, hinstance, None,
        )
        if not self.hwnd:
            return

        user32.SetLayeredWindowAttributes(self.hwnd, 0, ALPHA, LWA_ALPHA)
        region = gdi32.CreateRoundRectRgn(0, 0, WIDTH + 1, HEIGHT + 1, CORNER, CORNER)
        user32.SetWindowRgn(self.hwnd, region, True)
        user32.SetTimer(self.hwnd, 1, 500, None)

        msg = wintypes.MSG()
        while not self._stop.is_set():
            got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if got <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        user32.KillTimer(self.hwnd, 1)
        user32.DestroyWindow(self.hwnd)
        self.hwnd = 0

    def _position(self) -> tuple[int, int]:
        """贴在游戏窗口左下角（用户要求：左下）；找不到游戏窗口时用主屏左下角。"""
        game = _find_game_window()
        rect = RECT()
        if game and user32.GetWindowRect(game, ctypes.byref(rect)):
            return rect.left + MARGIN, rect.bottom - HEIGHT - MARGIN
        screen_h = user32.GetSystemMetrics(1)
        return MARGIN, screen_h - HEIGHT - 80

    def _refresh(self) -> None:
        try:
            data = self.provider()
        except Exception:  # noqa: BLE001
            data = None
        self._data = data
        if not self.hwnd:
            return
        if data is None:
            if self._visible:
                user32.ShowWindow(self.hwnd, SW_HIDE)
                self._visible = False
            return
        pos = self._position()
        if not self._visible:
            user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
            self._visible = True
        if pos != self._pos or not self._visible:
            self._pos = pos
            user32.SetWindowPos(
                self.hwnd, HWND_TOPMOST, pos[0], pos[1], WIDTH, HEIGHT,
                SWP_NOACTIVATE | SWP_SHOWWINDOW,
            )
        user32.InvalidateRect(self.hwnd, None, False)

    def _wnd_proc(self, hwnd, msg, wp, lp):
        if msg == WM_TIMER:
            self._refresh()
            return 0
        if msg == WM_ERASEBKGND:
            return 1
        if msg == WM_PAINT:
            self._paint(hwnd)
            return 0
        if msg == WM_CLOSE or msg == WM_DESTROY:
            self._stop.set()
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    # ---------------------------------------------------------- 绘制
    def _paint(self, hwnd) -> None:
        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
        mem = gdi32.CreateCompatibleDC(hdc)
        bitmap = gdi32.CreateCompatibleBitmap(hdc, WIDTH, HEIGHT)
        old_bitmap = gdi32.SelectObject(mem, bitmap)
        try:
            self._draw(mem)
            gdi32.BitBlt(hdc, 0, 0, WIDTH, HEIGHT, mem, 0, 0, SRCCOPY)
        finally:
            gdi32.SelectObject(mem, old_bitmap)
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(mem)
            user32.EndPaint(hwnd, ctypes.byref(ps))

    def _fill(self, hdc, x1, y1, x2, y2, color) -> None:
        brush = gdi32.CreateSolidBrush(color)
        r = RECT(x1, y1, x2, y2)
        user32.FillRect(hdc, ctypes.byref(r), brush)
        gdi32.DeleteObject(brush)

    def _text(self, hdc, text, x, y, w, h, size, color, bold=False, align=DT_LEFT) -> None:
        size = int(round(float(size)))
        font = gdi32.CreateFontW(
            -size, 0, 0, 0, FW_BOLD if bold else FW_NORMAL, 0, 0, 0,
            DEFAULT_CHARSET, 0, 0, CLEARTYPE_QUALITY, DEFAULT_PITCH | FF_DONTCARE,
            "Microsoft YaHei UI",
        )
        old = gdi32.SelectObject(hdc, font)
        gdi32.SetBkMode(hdc, TRANSPARENT_BK)
        gdi32.SetTextColor(hdc, color)
        r = RECT(x, y, x + w, y + h)
        user32.DrawTextW(
            hdc, str(text), -1, ctypes.byref(r),
            align | DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS,
        )
        gdi32.SelectObject(hdc, old)
        gdi32.DeleteObject(font)

    def _draw(self, hdc) -> None:
        data = self._data or {}
        self._fill(hdc, 0, 0, WIDTH, HEIGHT, COLOR_BG)
        # 1px 描边（圆角），让卡片在亮色画面上有边界
        pen = gdi32.CreatePen(0, 1, COLOR_LINE)  # PS_SOLID
        old_pen = gdi32.SelectObject(hdc, pen)
        old_brush = gdi32.SelectObject(hdc, gdi32.GetStockObject(5))  # NULL_BRUSH
        gdi32.RoundRect(hdc, 0, 0, WIDTH, HEIGHT, CORNER, CORNER)
        gdi32.SelectObject(hdc, old_brush)
        gdi32.SelectObject(hdc, old_pen)
        gdi32.DeleteObject(pen)

        prob = data.get("winProb")
        has_prob = prob is not None
        pct = (float(prob) * 100.0) if has_prob else 0.0
        accent = COLOR_GOOD if (prob or 0.0) >= 0.5 else COLOR_BAD

        prob_text = f"{pct:.1f}%" if has_prob else "--.-%"
        self._text(hdc, prob_text, 18, 14, 170, 46, 38, accent, bold=True)
        self._text(hdc, "当前胜率", 20, 62, 80, 18, 12.5, COLOR_FAINT)

        trend = data.get("trendPp")
        if trend is not None:
            arrow = "▲" if trend > 0.05 else ("▼" if trend < -0.05 else "·")
            tcolor = COLOR_GOOD if trend > 0.05 else (COLOR_BAD if trend < -0.05 else COLOR_FAINT)
            self._text(hdc, f"{arrow}{abs(trend):.1f}pp/分", WIDTH - 154, 62, 136, 18, 12, tcolor, align=DT_RIGHT)

        clock = str(data.get("clock") or "--:--")
        self._text(hdc, clock, WIDTH - 136, 16, 120, 22, 17, COLOR_TEXT, bold=True, align=DT_RIGHT)
        model = str(data.get("model") or "")
        tag = "观战/回放" if data.get("source") == "replay" else (model or "实时")
        self._text(hdc, tag, WIDTH - 156, 42, 140, 18, 12, COLOR_FAINT, align=DT_RIGHT)

        bar_x, bar_y, bar_w, bar_h = 18, 86, WIDTH - 36, 6
        self._fill(hdc, bar_x, bar_y, bar_x + bar_w, bar_y + bar_h, COLOR_TRACK)
        if has_prob:
            filled = max(2, int(bar_w * max(0.0, min(1.0, float(prob)))))
            self._fill(hdc, bar_x, bar_y, bar_x + filled, bar_y + bar_h, accent)

        prediction = data.get("prediction") or {}
        if prediction:
            prob_pct = int(round(float(prediction.get("prob") or 0.0) * 100))
            pcolor = COLOR_AMBER if prob_pct >= 35 else COLOR_FAINT
            self._text(
                hdc, "预测 " + str(prediction.get("label") or "") + " · " + str(prob_pct) + "%",
                18, 96, WIDTH - 36, 18, 12, pcolor,
            )

        advice = str(data.get("advice") or "").strip() or "等待对局数据…"
        self._text(hdc, "建议 " + advice, 18, 118, WIDTH - 36, 18, 12, COLOR_FAINT)


class OverlayController:
    """在后台线程里跑消息循环；provider 每 500ms 拉一次数据。"""

    def __init__(self, provider: Callable[[], Optional[Dict[str, Any]]]):
        self.window = OverlayWindow(provider)

    def start(self) -> None:
        self.window.start()

    def stop(self) -> None:
        self.window.stop()


# --------------------------------------------------------------------- 演示
def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--demo" not in argv:
        print(__doc__)
        return 0
    if not IS_WINDOWS:
        print("仅支持 Windows")
        return 1
    import math

    started = time.time()

    def fake() -> Dict[str, Any]:
        t = time.time() - started
        return {
            "winProb": 0.5 + 0.12 * math.sin(t / 2.0),
            "trendPp": 1.4 * math.cos(t / 2.0),
            "clock": f"{int(t) // 60:02d}:{int(t) % 60:02d}",
            "model": "live_model5",
            "source": "live",
            "advice": "敌方打野刚在下路露头，可压线抢经验；2 分钟后注意峡谷先锋。",
            "prediction": {"label": "敌方 60 秒内 gank", "prob": 0.42},
        }

    ctl = OverlayController(fake)
    ctl.start()
    try:
        time.sleep(8.0)
    finally:
        ctl.stop()
    print("演示结束")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
