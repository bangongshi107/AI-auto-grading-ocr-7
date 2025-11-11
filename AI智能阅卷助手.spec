# Future-proofing instructions for building this application.
#
# To build the executable, follow these steps in your terminal:
#
# 1. (Recommended) Clean up previous builds to avoid conflicts:
#    rm -r -fo dist build
#
# 2. Run PyInstaller with this spec file:
#    pyinstaller AI智能阅卷助手.spec
#
# The final single-file executable will be located in the 'dist' folder.

# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = collect_submodules('numpy') + collect_submodules('cv2')

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('setting', 'setting')],
    hiddenimports=['PyQt5.sip', 'PyQt5.QtCore', 'PyQt5.QtGui', 'PyQt5.QtWidgets', 'api_service', 'auto_thread', 'config_manager', 'ui_components.main_window', 'ui_components.question_config_dialog', 'pyautogui', 'PIL', 'PIL.ImageGrab', 'PIL.Image', 'PIL.ImageDraw', 'appdirs', 'requests', 'winsound'] + hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'unittest', 'doctest'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='AI智能阅卷助手',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='AI阅卷助手.ico',
    onefile=True,
)
