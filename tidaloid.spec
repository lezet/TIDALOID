# PyInstaller spec: builds dist/TIDALOID.exe (single file, no console window).
#   pyinstaller --noconfirm --clean tidaloid.spec

a = Analysis(
    ["tidaloid.pyw"],
    datas=[("assets/tidaloid.ico", "assets")],
    excludes=["numpy", "pytest", "IPython"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="TIDALOID",
    icon="assets/tidaloid.ico",
    console=False,
    upx=False,
)
