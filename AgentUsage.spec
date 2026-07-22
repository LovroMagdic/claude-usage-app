# PyInstaller spec for Agent Usage — one-file, windowed Windows build.
# Build with:  pyinstaller AgentUsage.spec   (or run build.bat)

block_cipher = None

a = Analysis(
    ['tray_app.py'],
    pathex=[],
    binaries=[],
    # Bundle the mascot as a read-only asset; tray_app resolves it via
    # config.resource_dir() (sys._MEIPASS at runtime).
    datas=[('agent usage mascot.png', '.')],
    hiddenimports=['pystray._win32'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # The app uses only pystray + Pillow + tkinter. These get pulled in as
    # optional Pillow/backends but are never used — excluding them shrinks the
    # binary dramatically.
    excludes=[
        'numpy', 'matplotlib', 'scipy', 'pandas',
        'PyQt5', 'PyQt6', 'PySide2', 'PySide6',
        'PIL.ImageQt',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='AgentUsage',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # --windowed: no console window
    disable_windowed_traceback=False,
    icon='app.ico',
    version_file=None,
)
