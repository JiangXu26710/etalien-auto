# -*- mode: python ; coding: utf-8 -*-
import os

block_cipher = None

PROJECT_DIR = SPECPATH

a = Analysis(
    [os.path.join(PROJECT_DIR, 'gui', 'app.py')],
    pathex=[PROJECT_DIR],
    binaries=[],
    datas=[
        (os.path.join(PROJECT_DIR, 'gui', 'static'), 'gui/static'),
        (os.path.join(PROJECT_DIR, 'gui', 'api.py'), 'gui'),
        (os.path.join(PROJECT_DIR, 'gui', '__init__.py'), 'gui'),
        (os.path.join(PROJECT_DIR, 'core', '__init__.py'), 'core'),
        (os.path.join(PROJECT_DIR, 'core', 'config.py'), 'core'),
        (os.path.join(PROJECT_DIR, 'core', 'client.py'), 'core'),
        (os.path.join(PROJECT_DIR, 'core', 'service.py'), 'core'),
        (os.path.join(PROJECT_DIR, 'core', 'sign.py'), 'core'),
        (os.path.join(PROJECT_DIR, 'account_pb2.py'), '.'),
        (os.path.join(PROJECT_DIR, 'apiv2_pb2.py'), '.'),
        (os.path.join(PROJECT_DIR, 'error_pb2.py'), '.'),
    ],
    hiddenimports=[
        'core',
        'core.client',
        'core.sign',
        'gui',
        'gui.api',
        'account_pb2',
        'apiv2_pb2',
        'error_pb2',
        'flask',
        'webview',
        'requests',
        'google.protobuf',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

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
    console=False,
    icon=os.path.join(PROJECT_DIR, 'logo', 'logo.ico'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='etalien-auto',
)
