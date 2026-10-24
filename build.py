"""Build the standalone Windows app using dependencies in vendor/."""
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "vendor"))
import PyInstaller.__main__

PyInstaller.__main__.run([
    str(ROOT / "live_trainer.py"), "--noconfirm", "--onefile", "--windowed",
    "--name", "GroundworkTrainer", "--paths", str(ROOT),
    "--paths", str(ROOT / "vendor"), "--distpath", str(ROOT / "release"),
    "--workpath", str(ROOT / "build"), "--specpath", str(ROOT / "build"),
])
shutil.copy2(ROOT / "QUICK_START.txt", ROOT / "release" / "QUICK_START.txt")
licenses = ROOT / "release" / "licenses"
licenses.mkdir(exist_ok=True)
for pattern in ("msgpack-*.dist-info", "zstandard-*.dist-info", "pyinstaller-*.dist-info"):
    for package in (ROOT / "vendor").glob(pattern):
        for file in package.rglob("*"):
            if file.is_file() and ("license" in file.name.lower() or "copying" in file.name.lower()):
                target = licenses / package.name / file.relative_to(package)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file, target)
python_license = Path(sys.base_prefix) / "LICENSE.txt"
if python_license.exists():
    shutil.copy2(python_license, licenses / "Python-LICENSE.txt")
tk_license = Path(sys.base_prefix) / "tcl/tk8.6/license.terms"
if tk_license.exists():
    shutil.copy2(tk_license, licenses / "Tk-license.terms")
print("Built", ROOT / "release" / "GroundworkTrainer.exe")
