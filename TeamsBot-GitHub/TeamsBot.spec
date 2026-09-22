# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['teams_bot.py'],
    pathex=[],
    binaries=[('teamsbot_menu_bar', '.'), ('teamsbot_vision_ocr', '.')],
    datas=[('submit_button_current.png', '.'), ('submit_button_light.png', '.'), ('teams_notification_icon.png', '.'), ('new_messages_reference.png', '.'), ('last_read_reference.png', '.'), ('TeamsBotMark.png', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TeamsBot',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['TeamsBot.icns'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='TeamsBot',
)
app = BUNDLE(
    coll,
    name='TeamsBot.app',
    icon='TeamsBot.icns',
    bundle_identifier='com.balexgt.teamsbot',
    info_plist={
        'CFBundleShortVersionString': '0.6.0-beta.15.1',
        'CFBundleVersion': '17',
    },
)
