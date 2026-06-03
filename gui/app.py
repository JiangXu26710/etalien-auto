import os
import sys
import threading
import logging
import socket
import time
import ctypes
import winreg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MB_OK = 0x00000000
MB_OKCANCEL = 0x00000001
MB_ICONERROR = 0x00000010
MB_ICONWARNING = 0x00000030
MB_ICONINFORMATION = 0x00000040
IDOK = 1
IDCANCEL = 2


def _check_dotnet_framework() -> tuple[bool, str]:
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full",
        )
        release, _ = winreg.QueryValueEx(key, "Release")
        winreg.CloseKey(key)
        if release >= 394802:
            version_map = [
                (533320, "4.8.1"), (528040, "4.8"), (461808, "4.7.2"),
                (461308, "4.7.1"), (460798, "4.7"), (394802, "4.6.2"),
            ]
            ver_str = "4.6.2+"
            for threshold, name in version_map:
                if release >= threshold:
                    ver_str = name
                    break
            return True, ver_str
        return False, f"已安装但版本过低(Release={release})"
    except OSError:
        return False, "未安装"


def _check_webview2_runtime() -> tuple[bool, str]:
    build_versions = [
        ("{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}", "Runtime"),
        ("{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}", "Beta"),
        ("{0D50BFEC-CD6A-4F9A-964C-C7416E3ACB10}", "Developer"),
        ("{65C35B14-6C1D-4122-AC46-7148CC9D6497}", "Canary"),
    ]
    for guid, channel in build_versions:
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for wow in ("", r"WOW6432Node"):
                try:
                    key = winreg.OpenKey(
                        root,
                        rf"Software\{wow}\Microsoft\EdgeUpdate\Clients\{guid}",
                    )
                    pv, _ = winreg.QueryValueEx(key, "pv")
                    winreg.CloseKey(key)
                    parts = pv.split(".")
                    if len(parts) >= 3:
                        major = int(parts[0])
                        if major >= 86:
                            return True, f"{pv} ({channel})"
                    return False, f"版本过低: {pv}"
                except OSError:
                    continue
    return False, "未安装"


def _check_runtime_env() -> bool:
    if sys.platform != "win32":
        return True

    dotnet_ok, dotnet_ver = _check_dotnet_framework()
    webview2_ok, webview2_ver = _check_webview2_runtime()

    if dotnet_ok and webview2_ok:
        return True

    if not dotnet_ok:
        msg = (
            "缺少必要的系统组件：.NET Framework 4.6.2 或更高版本\n\n"
            f"当前状态：{dotnet_ver}\n\n"
            "请访问以下地址下载安装：\n"
            "https://dotnet.microsoft.com/download/dotnet-framework\n\n"
            "安装完成后重新启动本程序。"
        )
        ctypes.windll.user32.MessageBoxW(0, msg, "免广告自动领时长 F - 缺少依赖", MB_OK | MB_ICONERROR)
        return False

    if not webview2_ok:
        msg = (
            "缺少必要的系统组件：Microsoft Edge WebView2 Runtime\n\n"
            f"当前状态：{webview2_ver}\n\n"
            "点击「确定」将打开下载页面，安装后重新启动本程序。\n"
            "下载地址：https://developer.microsoft.com/microsoft-edge/webview2/"
        )
        result = ctypes.windll.user32.MessageBoxW(
            0, msg, "免广告自动领时长 F - 缺少依赖", MB_OKCANCEL | MB_ICONWARNING,
        )
        if result == IDOK:
            import webbrowser
            webbrowser.open("https://developer.microsoft.com/microsoft-edge/webview2/#download-section")
        return False

    return True

is_cli_mode = "--cli" in sys.argv

if not _check_runtime_env():
    sys.exit(1)

if is_cli_mode:
    ctypes.windll.kernel32.AllocConsole()

    from main import main as cli_main
    auto_close = "--auto-close" in sys.argv
    if "--cli" in sys.argv:
        sys.argv.remove("--cli")
    if "--auto-close" in sys.argv:
        sys.argv.remove("--auto-close")

    exit_code = 0
    with open('CONOUT$', 'w') as _cli_stdout, \
         open('CONOUT$', 'w') as _cli_stderr, \
         open('CONIN$', 'r') as _cli_stdin:
        sys.stdout = _cli_stdout
        sys.stderr = _cli_stderr
        sys.stdin = _cli_stdin
        try:
            cli_main()
        except SystemExit as e:
            exit_code = e.code if isinstance(e.code, int) else 0
            if not auto_close:
                print("\n按回车键关闭...")
                try:
                    input()
                except Exception:
                    pass

    sys.stdout = None
    sys.stderr = None
    sys.stdin = None
    sys.exit(exit_code)
else:
    _devnull_files = []

if sys.stdout is None:
    f = open(os.devnull, 'w')
    _devnull_files.append(f)
    sys.stdout = f
if sys.stderr is None:
    f = open(os.devnull, 'w')
    _devnull_files.append(f)
    sys.stderr = f

logging.getLogger('werkzeug').setLevel(logging.WARNING)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

import webview
import gui.api as gui_api
from werkzeug.serving import make_server


class WindowApi:
    def __init__(self, window, server):
        self._window = window
        self._is_maximized = False
        self._server = server
        self._shutdown_called = False

    def minimize(self):
        self._window.minimize()

    def maximize(self):
        if self._is_maximized:
            self._window.restore()
            self._is_maximized = False
        else:
            self._window.maximize()
            self._is_maximized = True
        return self._is_maximized

    def restore(self):
        self._window.restore()
        self._is_maximized = False

    def close(self):
        self._shutdown()

    def is_maximized(self):
        return self._is_maximized

    def get_position(self):
        return {'x': self._window.x, 'y': self._window.y}

    def move_window(self, x, y):
        self._window.move(int(x), int(y))

    def _shutdown(self):
        if self._shutdown_called:
            return
        self._shutdown_called = True
        logger.info("正在关闭...")

        max_wait = 30
        waited = 0
        while waited < max_wait:
            if not gui_api.claim_mgr.running:
                break
            if waited == 0:
                logger.warning("有领取任务正在运行，等待任务完成...")
            time.sleep(1)
            waited += 1

        if self._server:
            try:
                self._server.shutdown()
                logger.info("服务器已关闭")
            except Exception as e:
                logger.warning("关闭服务器时出错: %s", e)

        self._window.destroy()


def find_available_port(start_port: int = 52137, max_port: int = 52200) -> int:
    for port in range(start_port, max_port + 1):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        try:
            sock.bind(('127.0.0.1', port))
            sock.close()
            return port
        except OSError:
            continue
    raise RuntimeError(f"无法在端口范围 {start_port}-{max_port} 中找到可用端口")


def main():
    logger.info("正在启动...")

    # 查找可用端口
    try:
        port = find_available_port()
        if port != 52137:
            logger.info("端口 52137 被占用，使用端口 %d", port)
    except RuntimeError as e:
        logger.error("%s", e)
        sys.exit(1)

    # 启动服务器
    server = make_server('127.0.0.1', port, gui_api.app, threaded=True)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.info("服务器已启动在 http://127.0.0.1:%d", port)

    win_w, win_h = 960, 720
    screen_w = ctypes.windll.user32.GetSystemMetrics(0)
    screen_h = ctypes.windll.user32.GetSystemMetrics(1)
    x = (screen_w - win_w) // 2
    y = (screen_h - win_h) // 2

    window = webview.create_window(
        title="外星仔加速器 - 免广告自动领时长 F",
        url=f"http://127.0.0.1:{port}",
        width=win_w,
        height=win_h,
        x=x,
        y=y,
        min_size=(720, 540),
        resizable=True,
        frameless=True,
        easy_drag=False,
        background_color='#0a0a0c',
    )

    api = WindowApi(window, server)
    window.expose(
        api.minimize, api.maximize, api.restore, api.close,
        api.is_maximized, api.get_position, api.move_window,
    )

    def _fix_taskbar_minimize(win):
        try:
            hwnd = win.native.Handle.ToInt32()
            GWL_STYLE = -16
            WS_MINIMIZEBOX = 0x20000
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_STYLE)
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_STYLE, style | WS_MINIMIZEBOX)
            ctypes.windll.user32.SetWindowPos(
                hwnd, 0, 0, 0, 0, 0,
                0x0020 | 0x0002 | 0x0001 | 0x0004,
            )
            logger.info("已添加 WS_MINIMIZEBOX 样式")
        except Exception as e:
            logger.warning("添加 WS_MINIMIZEBOX 样式失败: %s", e)

    window.events.shown += _fix_taskbar_minimize

    webview.start(debug=False)
    logger.info("应用已关闭")
    if not api._shutdown_called:
        if api._server:
            try:
                api._server.shutdown()
                logger.info("服务器已关闭")
            except Exception:
                pass

    for f in _devnull_files:
        try:
            f.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
