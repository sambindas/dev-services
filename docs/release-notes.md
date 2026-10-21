An early preview of Dev Services: a local dashboard for project commands and existing Docker containers.

## Choose your download

- **Windows x64:** `DevServices-0.2.2-Windows-Setup.exe`
- **macOS Apple Silicon:** `DevServices-0.2.2-macOS-arm64.dmg`
- **macOS Intel:** `DevServices-0.2.2-macOS-x86_64.dmg`
- **Ubuntu/Debian x64:** `dev-services_0.2.2_amd64.deb`
- **Other Linux x64 (glibc 2.35+):** `DevServices-0.2.2-Linux-x86_64.tar.gz`

The installers bundle Python. Install your project's runtime or Docker separately, launch Dev Services, then use **Setup guide** and **Add service**. The dashboard opens at http://127.0.0.1:7777 and stays on your computer.

This preview is unsigned and macOS builds are not notarized. Native CI checks cover tests, packaging, startup, and service controls; broader desktop testing is still needed. Linux RPM, Alpine/musl, and Windows/Linux ARM builds are not included.

Settings start empty and remain local. Your project files and Docker containers are kept when removing dashboard entries. Closing the browser leaves the controller running; use **Quit dashboard** to close it. Services continue running unless you stop them first.

See the repository README for installation details and troubleshooting. `SHA256SUMS.txt` contains checksums for the downloads. GitHub's automatically included source-code archives are for developers; choose an installer above to use the app.
