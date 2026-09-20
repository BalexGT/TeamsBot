# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('submit_button_current.png', '.'), ('submit_button_light.png', '.'), ('teams_notification_icon.png', '.'), ('new_messages_reference.png', '.'), ('last_read_reference.png', '.'), ('TeamsBotMark.png', '.'), ('microsoft-teams.webp', '.')]
binaries = [('teamsbot_menu_bar', '.'), ('teamsbot_vision_ocr', '.')]
hiddenimports = []
tmp_ret = collect_all('msal')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('requests')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('cryptography')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('cffi')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['teams_bot.py'],
    pathex=['vendor'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    name='TeamsBotGraphBeta',
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
    name='TeamsBotGraphBeta',
)
app = BUNDLE(
    coll,
    name='TeamsBotGraphBeta.app',
    icon='TeamsBot.icns',
    bundle_identifier='com.balexgt.teamsbot.graphbeta',
)
