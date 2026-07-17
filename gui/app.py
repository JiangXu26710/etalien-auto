"""GUI 入口：检查运行时依赖、启动本地服务并创建 WebView 窗口。"""

import os
import sys
import threading
import logging
import socket
import time
import ctypes
import winreg
import json
import secrets

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MB_OK = 0x00000000
MB_OKCANCEL = 0x00000001
MB_YESNO = 0x00000004
MB_ICONERROR = 0x00000010
MB_ICONQUESTION = 0x00000020
MB_ICONWARNING = 0x00000030
MB_ICONINFORMATION = 0x00000040
IDOK = 1
IDCANCEL = 2
IDYES = 6
IDNO = 7

# SetWindowPos 标志位（Win32 API 命名常量，避免魔数）
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_FRAMECHANGED = 0x0020


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

    from main import cli_entry
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
            cli_entry(auto_close=auto_close)
        except SystemExit as e:
            exit_code = e.code if isinstance(e.code, int) else 0
            # 退出码 6/7/8（EXIT_ALREADY_RUNNING / EXIT_NO_DB / EXIT_NOTIFIED_GUI）
            # 由 cli_entry 内部已按窗口保留策略处理 print + input，此处跳过避免双重回车
            if exit_code not in (6, 7, 8) and not auto_close:
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
from core.config import reload_settings, migrate_settings, CONFIG_DIR, ACCOUNTS_FILE, UNSAFE_PORTS
from core.db import DB_PATH, is_db_valid, migrate_from_json
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

        Returns:
            bool: 移动成功返回 True，参数非法返回 False。
        """
        try:
            x, y = int(x), int(y)
        except (TypeError, ValueError):
            return False
        # 限制坐标在合理范围，避免窗口被移出可见区域
        x = max(-10000, min(10000, x))
        y = max(-10000, min(10000, y))
        self._window.move(x, y)
        return True

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

        # 删除端口文件（CLI 通知 GUI 的依据，退出时清理）
        _delete_gui_port()

        if self._server:
            try:
                self._server.shutdown()
                logger.info("服务器已关闭")
            except Exception as e:
                logger.warning("关闭服务器时出错: %s", e)

        self._window.destroy()


def acquire_gui_port(preferred: int | None = None) -> int:
    """获取可用的本地 GUI 监听端口。

    Args:
        preferred: 期望端口。None 或非法时由 OS 动态分配；
                   合法时优先尝试绑定，失败则回退到 OS 动态分配。

    Returns:
        已成功获取的端口号（preferred 或 OS 动态分配）。

    Raises:
        OSError: preferred 绑定失败且 OS 动态分配也失败。
    """
    # preferred 合法性校验：必须是非 bool 的 int 且在 1-65535 范围内
    # （isinstance(True, int) 为 True，需显式排除 bool）
    if isinstance(preferred, int) and not isinstance(preferred, bool) and 1 <= preferred <= 65535:
        # Chromium 不安全端口黑名单检查：preferred 命中黑名单时跳过，直接动态分配
        # （WebView2 基于 Chromium，访问这些端口的 URL 会被拦截 ERR_UNSAFE_PORT → 白屏）
        if preferred in UNSAFE_PORTS:
            logger.warning("gui_port 配置值 %d 在 Chromium 不安全端口黑名单中，回退到动态分配", preferred)
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.bind(('127.0.0.1', preferred))
                sock.close()
                return preferred
            except OSError as e:
                sock.close()
                logger.warning("指定端口 %d 绑定失败，回退到动态分配: %s", preferred, e)
    elif preferred is not None:
        logger.warning("gui_port 配置值 %r 非法，回退到动态分配", preferred)
    # 动态分配：bind(0) 由 OS 分配可用端口，必在 TCP 排除范围之外
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        sock.close()
        return port
    except OSError as e:
        sock.close()
        raise OSError(f"动态端口分配失败: {e}") from e


def _perform_db_migration():
    """GUI 启动时检查 db 是否存在并执行迁移流程。

    流程：
    1. db 存在且 schema 校验通过 → 跳过迁移
    2. db 不存在或损坏 + accounts.json 存在 → 弹窗询问是否迁移
       - "是" → 迁移 + 重命名 json 为 .bak
       - "否" → 退出程序
    3. db 不存在或损坏 + 无 json + 有 .bak → 弹窗询问是否从备份恢复
       - "是" → 从 .bak 迁移
       - "否" → 建空表
    4. db 不存在或损坏 + 无 json + 无 .bak → 直接建空表
    5. 迁移失败 → 弹窗报错 + 退出程序
    """
    # db 存在且健康 → 跳过迁移
    if os.path.exists(DB_PATH) and is_db_valid(DB_PATH):
        return

    bak_path = ACCOUNTS_FILE + ".bak"

    if os.path.exists(ACCOUNTS_FILE):
        # 有 accounts.json → 弹窗询问是否迁移
        msg = (
            '检测到现有账号数据（accounts.json），是否迁移到数据库？\n\n'
            '点击"是"：迁移数据到 accounts.db（迁移完成后原 json 文件重命名为 .bak 备份）\n'
            '点击"否"：退出程序（程序运行时必须使用数据库，无数据库无法运行）'
        )
        result = ctypes.windll.user32.MessageBoxW(
            0, msg, "etalien-auto - 数据迁移", MB_YESNO | MB_ICONQUESTION
        )
        if result != IDYES:
            # 用户选"否"或关闭弹窗（X/ESC 等同 IDNO）→ 退出程序
            logger.info("用户取消迁移，GUI 退出")
            sys.exit(0)
        # 用户选"是" → 迁移
        try:
            migrate_from_json(ACCOUNTS_FILE, DB_PATH)
        except Exception as e:
            _show_migration_error(str(e))
            sys.exit(1)
        # 迁移成功 → 重命名 json 为 .bak
        _rename_json_to_bak(ACCOUNTS_FILE, bak_path)
        return

    # 无 json → 检查 .bak
    if os.path.exists(bak_path):
        msg = (
            '数据库损坏且无 JSON 源，检测到备份文件 accounts.json.bak，是否从备份恢复？\n\n'
            '点击"是"：从 .bak 文件迁移数据到 accounts.db\n'
            '点击"否"：建空表启动（无账号数据）'
        )
        result = ctypes.windll.user32.MessageBoxW(
            0, msg, "etalien-auto - 数据恢复", MB_YESNO | MB_ICONQUESTION
        )
        if result == IDYES:
            # 从 .bak 恢复（不重命名 .bak，避免链式覆盖）
            try:
                migrate_from_json(bak_path, DB_PATH)
            except Exception as e:
                _show_migration_error(str(e))
                sys.exit(1)
            return
        # 用户选"否" → 建空表（不询问）
        try:
            migrate_from_json(ACCOUNTS_FILE, DB_PATH)  # ACCOUNTS_FILE 不存在 → 建空表
        except Exception as e:
            _show_migration_error(str(e))
            sys.exit(1)
        return

    # 无 json 无 .bak → 建空表（不询问）
    logger.info("无 json 与 .bak，直接建空表")
    try:
        migrate_from_json(ACCOUNTS_FILE, DB_PATH)  # ACCOUNTS_FILE 不存在 → 建空表
    except Exception as e:
        _show_migration_error(str(e))
        sys.exit(1)


def _rename_json_to_bak(json_path: str, bak_path: str):
    """将 accounts.json 重命名为 .bak 备份（Windows 标准命名规则）。

    accounts.json.bak 已存在 → accounts.json (1).bak → accounts.json (2).bak → ...
    重命名失败仅记日志，不影响 GUI 启动（db 已有完整数据）。
    """
    if not os.path.exists(bak_path):
        target = bak_path
    else:
        # accounts.json.bak → accounts.json (1).bak → accounts.json (2).bak → ...
        base, ext = os.path.splitext(bak_path)  # ("accounts.json", ".bak")
        i = 1
        while True:
            target = f"{base} ({i}){ext}"
            if not os.path.exists(target):
                break
            i += 1
    try:
        os.rename(json_path, target)
        logger.info("已重命名 %s → %s", json_path, target)
    except OSError as e:
        # 重命名失败（权限问题等）→ 不影响数据完整性（db 已有完整数据），仅记日志
        logger.warning("重命名 json 为 .bak 失败（不影响 db 数据）: %s", e)


def _show_migration_error(error_msg: str):
    """弹窗提示迁移失败。"""
    msg = f"数据迁移失败：{error_msg}\n\n请检查文件权限或联系技术支持。"
    ctypes.windll.user32.MessageBoxW(
        0, msg, "etalien-auto - 迁移失败", MB_OK | MB_ICONERROR
    )


def _write_gui_port(port: int, token: str):
    """写入 GUI 端口文件（CLI 通过此文件找到 GUI 端口与认证 token）。

    文件格式为 JSON：{"port": <int>, "token": <hex str>}。
    token 用于 CLI 请求 /api/cli-trigger-claim 时的 Bearer 认证。

    写入失败仅记日志，不影响 GUI 启动（CLI 通知失败时 CLI 自身会退出）。
    """
    port_file = os.path.join(CONFIG_DIR, ".gui_port")
    try:
        with open(port_file, "w", encoding="utf-8") as f:
            json.dump({"port": port, "token": token}, f)
    except OSError as e:
        logger.warning("写入端口文件 %s 失败: %s", port_file, e)


def _delete_gui_port():
    """删除 GUI 端口文件（GUI 退出时调用）。

    文件不存在或删除失败均忽略（下次 GUI 启动会覆盖写）。
    """
    port_file = os.path.join(CONFIG_DIR, ".gui_port")
    try:
        if os.path.exists(port_file):
            os.unlink(port_file)
    except OSError as e:
        logger.warning("删除端口文件 %s 失败: %s", port_file, e)


def main():
    """GUI 主流程：检查实例锁、迁移数据、启动本地服务并创建 WebView 窗口。"""

    # 统一 Named Mutex 检查（与 CLI 共用，任何模式只能开一个实例）
    from main import _create_mutex
    mutex_handle, already_exists = _create_mutex()
    if mutex_handle == 0 or already_exists:
        # 已有实例在运行（GUI 或 CLI），直接退出（不弹窗、不区分实例类型）
        logger.info("已有实例在运行，GUI 退出")
        sys.exit(0)

    # 迁移旧版 settings.json（补全缺失字段，幂等；在初始化缓存前执行，确保缓存含完整字段）
    migrate_settings()

    # 初始化 settings 缓存（整个进程生命周期内首次读取）
    reload_settings()

    # db 迁移流程（GUI 启动时执行，CLI 不迁移）
    _perform_db_migration()

    # 获取可用端口：settings.gui_port 指定则优先尝试，失败/未指定则 OS 动态分配
    preferred = reload_settings().get('gui_port')
    try:
        port = acquire_gui_port(preferred)
        if preferred is not None and port != preferred:
            logger.warning("指定端口 %s 绑定失败，回退到动态端口 %d", preferred, port)
        elif preferred is None:
            logger.info("使用动态端口 %d", port)
    except OSError as e:
        logger.error("获取端口失败，GUI 启动失败: %s", e)
        sys.exit(1)

    # 启动服务器：acquire_gui_port 与 make_server 之间存在 TOCTOU 竞态，
    # 失败时强制动态分配再试一次
    try:
        server = make_server('127.0.0.1', port, gui_api.app, threaded=True)
    except OSError as e:
        logger.warning("make_server 端口 %d 失败（TOCTOU）: %s，强制动态分配重试", port, e)
        try:
            port = acquire_gui_port(None)
            server = make_server('127.0.0.1', port, gui_api.app, threaded=True)
        except OSError as e2:
            logger.error("动态端口也失败，GUI 启动失败: %s", e2)
            sys.exit(1)
    # 写入实际端口供 API 暴露给前端（GET /api/settings 返回 actual_gui_port）
    gui_api._actual_gui_port = port
    # 写入端口文件（CLI 通过此文件找到 GUI 端口，在 serve_forever 之前写入）
    # 同时生成随机 token 用于 CLI→GUI 触发领取的 Bearer 认证
    cli_trigger_token = secrets.token_hex(16)
    gui_api._cli_trigger_token = cli_trigger_token
    _write_gui_port(port, cli_trigger_token)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.info("服务器已启动在 http://127.0.0.1:%d", port)

    # 窗口默认尺寸 960×740，居中显示
    win_w, win_h = 960, 740
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
                SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER,
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
        _delete_gui_port()
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
