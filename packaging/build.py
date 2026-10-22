"""Build on each target OS: python packaging/build.py [--package]."""
import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.2.3"


def run(args):
    subprocess.run([str(arg) for arg in args], cwd=ROOT, check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", action="store_true", help="Also create a native installer/package")
    args = parser.parse_args()
    os.environ.setdefault("PYINSTALLER_CONFIG_DIR", str(ROOT / "build/pyinstaller-cache"))
    system = platform.system()
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
               "--name", "DevServices", "--add-data", f"{ROOT / 'index.html'}{os.pathsep}.",
               "--workpath", ROOT / "build/pyinstaller", "--specpath", ROOT / "build"]
    if system in ("Darwin", "Windows"):
        command += ["--windowed"]
    if system == "Darwin":
        command += ["--osx-bundle-identifier", "local.devservices.dashboard"]
    run(command + [ROOT / "launcher.py"])
    dist = ROOT / "dist"
    if not args.package:
        return
    arch = platform.machine().lower()
    if system == "Darwin":
        staging = ROOT / "build/dmg"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        shutil.copytree(dist / "DevServices.app", staging / "DevServices.app", symlinks=True)
        (staging / "Applications").symlink_to("/Applications")
        image = dist / f"DevServices-{VERSION}-macOS-{arch}.dmg"
        image.unlink(missing_ok=True)
        run(["hdiutil", "create", "-volname", "Dev Services", "-srcfolder", staging, "-ov", "-format", "UDZO", image])
    elif system == "Windows":
        compiler = shutil.which("ISCC") or str(Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Inno Setup 6/ISCC.exe")
        run([compiler, ROOT / "packaging/windows.iss"])
    elif system == "Linux":
        # A portable archive works on distributions without dpkg.
        shutil.make_archive(str(dist / f"DevServices-{VERSION}-Linux-{arch}"), "gztar", dist, "DevServices")
        if not shutil.which("dpkg-deb"):
            return
        debarch = {"x86_64": "amd64", "aarch64": "arm64"}.get(arch, arch)
        staging = ROOT / "build/deb"
        if staging.exists():
            shutil.rmtree(staging)
        (staging / "DEBIAN").mkdir(parents=True)
        shutil.copytree(dist / "DevServices", staging / "opt/dev-services")
        (staging / "usr/share/applications").mkdir(parents=True)
        shutil.copyfile(ROOT / "packaging/dev-services.desktop", staging / "usr/share/applications/dev-services.desktop")
        (staging / "DEBIAN/control").write_text(
            f"Package: dev-services\nVersion: {VERSION}\nArchitecture: {debarch}\n"
            "Maintainer: Dev Services contributors\nDepends: libc6 (>= 2.35), xdg-utils\n"
            "Section: devel\nPriority: optional\nDescription: Local developer service dashboard\n", encoding="utf-8")
        run(["dpkg-deb", "--root-owner-group", "--build", staging, dist / f"dev-services_{VERSION}_{debarch}.deb"])


if __name__ == "__main__":
    main()
