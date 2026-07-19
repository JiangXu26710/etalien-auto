"""
打包脚本：使用 PyInstaller 将项目打包为可执行文件。
"""

import subprocess
import sys
import os

# 虚拟环境中的 Python 解释器路径（Windows: .venv/Scripts/python.exe，Linux/Mac: .venv/bin/python）
if sys.platform == "win32":
    VENV_PYTHON = os.path.join(os.path.dirname(__file__), ".venv", "Scripts", "python.exe")
else:
    VENV_PYTHON = os.path.join(os.path.dirname(__file__), ".venv", "bin", "python")
# PyInstaller 规格文件路径
SPEC_FILE = os.path.join(os.path.dirname(__file__), "etalien-auto.spec")


def main():
    """执行打包流程。

    检查虚拟环境、PyInstaller 是否可用，并调用 PyInstaller 根据
    etalien-auto.spec 进行打包。打包成功时正常退出；失败时以非零状态码退出。
    """
    # 检查虚拟环境是否存在
    if not os.path.exists(VENV_PYTHON):
        print(f"[ERROR] 虚拟环境不存在: {VENV_PYTHON}")
        sys.exit(1)

    # 检查 spec 文件是否存在
    if not os.path.exists(SPEC_FILE):
        print(f"[ERROR] Spec文件不存在: {SPEC_FILE}")
        sys.exit(1)

    # 检查虚拟环境中是否已安装 PyInstaller 且版本 >= 6.0（单次子进程调用）
    # 返回码：0=OK，1=版本过低，2=未安装
    check_result = subprocess.run(
        [VENV_PYTHON, "-c",
         "import sys\n"
         "try:\n"
         "    import PyInstaller\n"
         "except ImportError:\n"
         "    sys.exit(2)\n"
         "v = int(PyInstaller.__version__.split('.')[0])\n"
         "print(PyInstaller.__version__)\n"
         "sys.exit(0 if v >= 6 else 1)"],
        capture_output=True, text=True,
    )
    version = check_result.stdout.strip()
    if check_result.returncode == 2:
        print(f"[ERROR] PyInstaller 未安装，请运行: {VENV_PYTHON} -m pip install pyinstaller")
        sys.exit(1)
    elif check_result.returncode == 1:
        print(f"[ERROR] PyInstaller 版本过低: {version}, 需 >=6.0")
        sys.exit(1)

    # 执行打包命令
    cmd = [VENV_PYTHON, "-m", "PyInstaller", SPEC_FILE, "--noconfirm", "--clean"]
    print(f"[BUILD] {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=os.path.dirname(__file__))
    if result.returncode != 0:
        print(f"[BUILD] 打包失败, 退出码: {result.returncode}")
        sys.exit(result.returncode)

    # 验证打包产物存在（PyInstaller 静默失败时 dist 可能未生成）
    dist_exe = os.path.join(os.path.dirname(__file__), "dist", "etalien-auto", "etalien-auto.exe")
    if not os.path.exists(dist_exe):
        print(f"[ERROR] 打包产物不存在: {dist_exe}（PyInstaller 可能静默失败）")
        sys.exit(1)
    print(f"[OK] 打包成功: {dist_exe}")


if __name__ == "__main__":
    main()
