import subprocess
import sys
import os

VENV_PYTHON = os.path.join(os.path.dirname(__file__), ".venv", "Scripts", "python.exe")
SPEC_FILE = os.path.join(os.path.dirname(__file__), "etalien-auto.spec")


def main():
    if not os.path.exists(VENV_PYTHON):
        print(f"[ERROR] 虚拟环境不存在: {VENV_PYTHON}")
        sys.exit(1)

    if not os.path.exists(SPEC_FILE):
        print(f"[ERROR] Spec文件不存在: {SPEC_FILE}")
        sys.exit(1)

    check_result = subprocess.run(
        [VENV_PYTHON, "-c", "import PyInstaller"],
        capture_output=True, text=True,
    )
    if check_result.returncode != 0:
        print(f"[ERROR] PyInstaller 未安装，请运行: {VENV_PYTHON} -m pip install pyinstaller")
        sys.exit(1)

    cmd = [VENV_PYTHON, "-m", "PyInstaller", SPEC_FILE, "--noconfirm", "--clean"]
    print(f"[BUILD] {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=os.path.dirname(__file__))
    if result.returncode == 0:
        print("[BUILD] 打包成功! 输出目录: dist/etalien-auto/")
    else:
        print(f"[BUILD] 打包失败, 退出码: {result.returncode}")
        sys.exit(result.returncode)


if __name__ == "__main__":
    main()
