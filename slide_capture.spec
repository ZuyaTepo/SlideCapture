# -*- mode: python ; coding: utf-8 -*-
# PyInstaller のビルド設定
#   pyinstaller --noconfirm --clean slide_capture.spec
#
# 環境変数 ONEFILE=1 を付けると、exe 1つにまとめる（起動は少し遅くなる）。
# 付けなければ dist/SlideCapture/ フォルダ形式（起動が速い・おすすめ）。

import os

APP_NAME = "SlideCapture"
ONEFILE = os.environ.get("ONEFILE", "0") == "1"
ICON = "app.ico" if os.path.exists("app.ico") else None

# 使っていない大きなライブラリ・Qtモジュールを除外してサイズを減らす
EXCLUDES = [
    "tkinter", "matplotlib", "pandas", "scipy", "IPython", "pytest",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtBluetooth", "PySide6.QtNetwork",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtHelp",
]

a = Analysis(
    ["slide_capture.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)
# Qt のプラグイン経由で入ってしまう、このアプリでは使わない Qt ライブラリを取り除く
# （QML / Quick / PDF表示 / 仮想キーボード / ソフトウェアOpenGL / GTKテーマ など）
DROP_BINARIES = (
    "Qt6Qml", "Qt6Quick", "Qt6Pdf", "Qt6VirtualKeyboard", "Qt6Network",
    "qtvirtualkeyboard", "qpdf", "opengl32sw", "qgtk3", "libgtk-3", "libgdk-3",
    # 動画の読み込みは PyAV で行うので、OpenCV の FFmpeg プラグイン（Windows）は不要
    "opencv_videoio_ffmpeg",
)
a.binaries = [b for b in a.binaries
              if not any(k.lower() in os.path.basename(b[0]).lower() for k in DROP_BINARIES)]
# Qt 本体の翻訳ファイル（未使用）
a.datas = [d for d in a.datas if "translations" not in d[0].replace("\\", "/")]

pyz = PYZ(a.pure)

if ONEFILE:
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [],
        name=APP_NAME,
        console=False,          # コンソール窓を出さない
        upx=False,              # UPX圧縮はウイルス対策ソフトの誤検知を招きやすいので使わない
        icon=ICON,
    )
else:
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name=APP_NAME,
        console=False,
        upx=False,
        icon=ICON,
    )
    coll = COLLECT(exe, a.binaries, a.datas, name=APP_NAME, upx=False)
