"""GUI 入口：检查运行时依赖、启动本地服务并创建 WebView 窗口。"""

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
    """检查 .NET Framework 4.6.2+ 是否已安装。

    Returns:
        tuple[bool, str]: 第一个元素表示是否满足要求；
        第二个元素为状态描述，满足时返回版本字符串，否则返回失败原因。
    """
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
    """检查 Microsoft Edge WebView2 Runtime 是否已安装且版本满足要求。

    Returns:
        tuple[bool, str]: 第一个元素表示是否满足要求；
        第二个元素为状态描述，满足时返回版本与通道，否则返回失败原因。
    """
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
    """检查 Windows 运行时依赖是否齐全。

    在非 Windows 平台直接返回 True；在 Windows 上检查 .NET Framework 与
    WebView2 Runtime，并在缺失时弹出提示框。

    Returns:
        bool: 依赖满足返回 True，否则返回 False。
    """
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

# 判断启动模式：--cli 标志决定走 CLI 还是 GUI 路径。
# 必须在导入 pywebview 等重依赖前判断，CLI 模式无需加载 GUI 库。
is_cli_mode = "--cli" in sys.argv

if not _check_runtime_env():
    sys.exit(1)

if is_cli_mode:
    # 分配控制台窗口（PyInstaller console=False 模式下默认无控制台）
    ctypes.windll.kernel32.AllocConsole()

    # 禁用快速编辑模式：防止用户误点控制台窗口进入"选择模式"导致
    # WriteFile 阻塞，表现为程序卡住、需按回车才能继续
    # 使用 CreateFile("CONIN$") 直接打开控制台输入句柄，比 GetStdHandle 更可靠：
    # GetStdHandle 在 AllocConsole 后可能返回陈旧/无效句柄，导致修复静默失败
    _kernel32 = ctypes.windll.kernel32
    _ENABLE_QUICK_EDIT_MODE = 0x0040
    _ENABLE_EXTENDED_FLAGS = 0x0080
    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _FILE_SHARE_READ = 0x00000001
    _OPEN_EXISTING = 3

    _h_in = _kernel32.CreateFileW(
        "CONIN$",
        _GENERIC_READ | _GENERIC_WRITE,
        _FILE_SHARE_READ,
        None,
        _OPEN_EXISTING,
        0,
        None,
    )
    _mode = ctypes.c_ulong()
    if _h_in != ctypes.c_void_p(-1).value and _kernel32.GetConsoleMode(_h_in, ctypes.byref(_mode)):
        _new_mode = _mode.value & ~_ENABLE_QUICK_EDIT_MODE
        _new_mode |= _ENABLE_EXTENDED_FLAGS
        _kernel32.SetConsoleMode(_h_in, _new_mode)
        _kernel32.CloseHandle(_h_in)

    from main import main as cli_main
    auto_close = "--auto-close" in sys.argv
    # 移除 CLI 专属参数，避免传递给 main.py
    if "--cli" in sys.argv:
        sys.argv.remove("--cli")
    if "--auto-close" in sys.argv:
        sys.argv.remove("--auto-close")

    exit_code = 0
    # 重定向标准流到分配的控制台（CONOUT$=控制台输出，CONIN$=控制台输入）
    with open('CONOUT$', 'w') as _cli_stdout, \
         open('CONOUT$', 'w') as _cli_stderr, \
         open('CONIN$', 'r') as _cli_stdin:
        sys.stdout = _cli_stdout
        sys.stderr = _cli_stderr
        sys.stdin = _cli_stdin
        try:
            cli_main(auto_close=auto_close)
        except SystemExit as e:
            exit_code = e.code if isinstance(e.code, int) else 0
            if not auto_close:
                print("\n按回车键关闭...")
                try:
                    input()
                except Exception:
                    pass

    # with 块退出后文件已关闭，置 None 避免后续代码使用已关闭的文件对象
    sys.stdout = None
    sys.stderr = None
    sys.stdin = None
    sys.exit(exit_code)
else:
    # GUI 模式：初始化 _devnull_files 空列表，后续若 stdout/stderr 为 None
    # 则打开 devnull 并追加到此列表，统一在退出时关闭。
    _devnull_files = []

# PyInstaller console=False 时 sys.stdout/stderr 可能为 None，重定向到 devnull 避免打印异常
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
from core.config import reload_settings
from werkzeug.serving import make_server


class WindowApi:
    """WebView 窗口与后端服务器的桥接 API。"""

    def __init__(self, window, server):
        """初始化窗口 API。

        Args:
            window: pywebview 窗口实例。
            server: Werkzeug 服务器实例。
        """
        self._window = window
        self._is_maximized = False
        self._server = server
        self._shutdown_called = False

    def minimize(self):
        """最小化窗口。"""
        self._window.minimize()

    def maximize(self):
        """最大化/还原窗口。

        Returns:
            bool: 当前是否处于最大化状态。
        """
        if self._is_maximized:
            self._window.restore()
            self._is_maximized = False
        else:
            self._window.maximize()
            self._is_maximized = True
        return self._is_maximized

    def restore(self):
        """还原窗口大小。"""
        self._window.restore()
        self._is_maximized = False

    def close(self):
        """关闭窗口并停止服务器。"""
        self._shutdown()

    def is_maximized(self):
        """查询窗口是否处于最大化状态。

        Returns:
            bool: 是否最大化。
        """
        return self._is_maximized

    def get_position(self):
        """获取窗口当前位置。

        Returns:
            dict: 包含 x、y 坐标的字典。
        """
        return {'x': self._window.x, 'y': self._window.y}

    def move_window(self, x, y):
        """移动窗口到指定坐标。

        Args:
            x: 目标 x 坐标。
            y: 目标 y 坐标。
        """
        self._window.move(int(x), int(y))

    def _shutdown(self):
        if self._shutdown_called:
            return
        self._shutdown_called = True
        logger.info("正在关闭...")

        # 轮询等待领取任务完成（最长 30 秒超时），避免在任务运行中关闭导致状态不一致。
        # 通过 gui_api.claim_mgr.running 动态访问，避免布尔值导入时被拷贝导致读取到旧值。
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
    """在指定端口范围内查找第一个可用的本地端口。

    Args:
        start_port: 起始端口号（含）。
        max_port: 结束端口号（含）。

    Returns:
        第一个成功绑定的端口号。

    Raises:
        RuntimeError: 范围内所有端口均被占用。
    """
    for port in range(start_port, max_port + 1):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        try:
            sock.bind(('127.0.0.1', port))
            sock.close()
            return port
        except OSError:
            sock.close()
            continue
    raise RuntimeError(f"无法在端口范围 {start_port}-{max_port} 中找到可用端口")


def main():
    """GUI 主流程：初始化设置、查找可用端口、启动本地服务并创建 WebView 窗口。"""

    # 初始化 settings 缓存（整个进程生命周期内首次读取）
    reload_settings()

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

    # 窗口默认尺寸 960×720，居中显示
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

    def _fix_taskbar_minimize(window):
        # pywebview 创建 frameless 窗口时 WinForms 会移除 WS_MINIMIZEBOX 样式，
        # 导致 Windows 任务栏点击无法触发最小化。此处通过 Win32 API 补回样式
        # 并调用 SetWindowPos + SWP_FRAMECHANGED 通知系统重新计算窗口帧。
        # 参数名必须为 'window'：pywebview 6.x Event.set() 据此注入 self._window，
        # 用其他参数名（如 win）会命中透传分支导致 missing argument 错误。
        try:
            hwnd = window.native.Handle.ToInt32()
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
    # webview.start 返回后窗口已关闭；若 _shutdown 未被调用过（如异常退出路径），
    # 此处补充关闭 Flask 服务器，避免与窗口关闭按钮触发的 _shutdown 重复关闭
    if not api._shutdown_called:
        if api._server:
            try:
                api._server.shutdown()
                logger.info("服务器已关闭")
            except Exception:
                pass

    # 关闭 devnull 文件对象，避免文件描述符泄漏
    for f in _devnull_files:
        try:
            f.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
