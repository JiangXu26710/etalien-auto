# -*- mode: python ; coding: utf-8 -*-
import os

PROJECT_DIR = SPECPATH

a = Analysis(
    [os.path.join(PROJECT_DIR, 'gui', 'app.py')],
    pathex=[PROJECT_DIR],
    binaries=[],
    datas=[
        (os.path.join(PROJECT_DIR, 'gui', 'static'), 'gui/static'),
        (os.path.join(PROJECT_DIR, 'proto'), 'proto'),
    ],
    hiddenimports=[
        'core',
        'core.client',
        'core.config',
        'core.db',
        'core.service',
        'core.notify',
        'core.sign',
        'gui',
        'gui.api',
        'account_pb2',
        'apiv2_pb2',
        'error_pb2',
        'flask',
        'webview',
        'webview.guilib',
        'webview.util',
        'webview.platforms.edgechromium',
        'webview.platforms.winforms',
        'requests',
        'google.protobuf',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', 'PyQt5', 'PySide6', 'matplotlib', 'numpy', 'pandas',
        'scipy', 'IPython', 'pytest', 'pydoc',
        'distutils', 'lib2to3', 'turtle', 'turtledemo',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='etalien-auto',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,    # GUI模式无终端窗口；CLI模式通过 --cli 参数切换，由 gui/app.py 调用 AllocConsole 分配控制台
    icon=os.path.join(PROJECT_DIR, 'logo', 'logo.ico'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[
        'python311.dll', 'vcruntime140.dll', 'vcruntime140_1.dll',
        'pywintypes311.dll', 'pythoncom311.dll',
        'WebView2Loader.dll',  # pywebview 依赖
    ],
    name='etalien-auto',
)
