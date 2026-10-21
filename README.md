# Dev Services

A local dashboard for starting, stopping, restarting and viewing logs from your projects and existing Docker containers. It opens in your browser and runs only on the loopback interface of your computer. There are no cloud accounts or remote agents in this release.

## Install a packaged build

Download the **unsigned preview installers** from [GitHub Releases](https://github.com/sambindas/dev-services/releases). Open the newest preview release and expand **Assets**, then choose the file for your operating system below. Builds are platform and CPU specific. Source-code ZIP/tar archives are for developers, not installers.

| Platform | Package | Install and launch |
| --- | --- | --- |
| macOS Apple Silicon / Intel | `.dmg` | Open the matching image, drag DevServices to Applications, then open it. |
| Windows x64 | `Windows-Setup.exe` | Run the installer as your normal user, then open Dev Services from Start. |
| Ubuntu/Debian x64 | `.deb` | Install with `sudo apt install ./dev-services_0.2.2_amd64.deb`, then open Dev Services from the applications menu. |
| Other Linux x64 | `.tar.gz` | Extract it, then run `DevServices/DevServices`. Requires glibc 2.35 or newer; not an Alpine/musl package. |

Python is bundled in these packages. Project runtimes and Docker are installed separately. Preview packages are not signed/notarized yet; Windows and macOS may display publisher warnings. Public distribution needs signing credentials and native-machine verification before calling the installers stable. Linux RPM and ARM Linux/Windows builds are not provided yet.

The app opens `http://127.0.0.1:7777`. Opening it again reconnects to an existing instance. Closing the browser does not stop the backend. Use **Quit dashboard** to close the backend. Service processes stay running and are recognized when you reopen the app; use **Stop all** before quitting if you want to stop them.

## Run from source

Requires Python 3.9+ and pip. In the project folder:

**macOS/Linux**

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python server.py
```

**Windows PowerShell**

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe server.py
```

Use `--no-browser` for terminal/server usage. Change the port with `DASHBOARD_PORT`. Set `DASHBOARD_DATA_DIR` to use a separate configuration directory. All configuration paths refer to the machine running the backend.

A source checkout keeps its existing `services.json` and `logs` directory. Packaged apps start empty and store settings outside the application installation:

- Windows: `%LOCALAPPDATA%\DevServices`
- macOS: `~/Library/Application Support/DevServices`
- Linux: `$XDG_DATA_HOME/dev-services`, defaulting to `~/.local/share/dev-services`

Back up that directory to preserve configuration and logs. App updates and normal uninstallation leave this data intact. Do not distribute your personal `services.json`, logs, or credentials. The build script includes only the UI and Python application code/dependencies.

## Add a project command

1. Install the runtime your project needs and its dependencies.
2. Verify its start command works in a terminal from its project folder. Keep it in the foreground; commands that daemonize/detach are unsupported.
3. Select **Add service → Project command**.
4. Enter a unique ID, name, group, absolute project folder, start command and local listening port.
5. Optionally add an HTTP(S) URL, notes and startup timeout. Save, then select **Start**.
6. Open **Logs** if startup fails. The project must open its configured loopback port before the startup timeout.

Examples:

| Project | Command | Port |
| --- | --- | --- |
| Vite project | `npm run dev -- --port 3000 --strictPort` | 3000 |
| Python static server (macOS/Linux) | `python3 -m http.server 8000 --bind 127.0.0.1` | 8000 |
| Python static server (Windows) | `python -m http.server 8000 --bind 127.0.0.1` | 8000 |
| .NET project | `dotnet run --urls http://127.0.0.1:5000` | 5000 |

Commands use `cmd.exe` on Windows and your configured shell on macOS/Linux. Version managers may require explicit initialization in the command or an absolute executable path. GUI apps inherit a different PATH from interactive terminals; **Setup guide** reports which tools are visible. Restart Dev Services after installing tools.

The app tracks PID and process creation time, persists ownership across dashboard restarts, and stops the managed process tree. It will not terminate an independently started process merely because it occupies the configured port. Stop such a process from its original terminal first. Processes that detach from their parent are not supported.

## Add an existing Docker container

1. Install Docker and start its engine.
2. Create a container yourself, publishing the service port. For example, `docker run -d --name dashboard-redis -p 127.0.0.1:6379:6379 redis:7` (only if you want this container; image download requires internet).
3. Select **Add service → Docker container**, enter `dashboard-redis`, and use host port `6379`.
4. Save and start. The dashboard controls the named container and streams `docker logs`.

Use `docker ps -a` to find existing container names. This release does not provision images, volumes or containers. A healthy container with no matching published port is reported as waiting for its port.

## Edit, remove, and legacy services

Stop a service before editing or removing it. Removal deletes its dashboard entry, keeping project files, containers and logs. Duplicate IDs/ports, nonexistent project folders and invalid URLs are rejected. Service updates are atomic and do not require a dashboard restart.

The original `brew`, `supabase`, `app`, and planned definitions remain readable. Homebrew and Docker Desktop app controls remain macOS-only. Legacy definitions are maintained in `services.json`; the guided editor supports process and Docker services. Shared data must be adapted per machine; a Mac path or command is not automatically translated for Windows.

## Build installers

Build **on the target OS and architecture** using Python 3.12:

```sh
python -m pip install -r requirements-build.txt
python packaging/build.py --package
```

Windows also needs Inno Setup 6 (`ISCC.exe`). macOS needs Apple's `hdiutil` and build command line tools. Ubuntu uses `dpkg-deb`; without it Linux still produces a portable archive. Outputs go to `dist/`. The GitHub workflow runs tests, builds packages, smoke-tests the bundled executable, and saves artifacts for Windows x64, Linux x64, macOS ARM64 and Intel. A version tag triggers a preview GitHub Release only after all four native build jobs pass. Release assets include SHA-256 checksums. Per-machine services.json, process ownership records, logs, and environment files are excluded from Git.

See [PyInstaller's build documentation](https://pyinstaller.org/en/stable/usage.html) for bundling and OS restrictions, and [psutil's documentation](https://psutil.readthedocs.io/stable/index.html) for process management. Build numbers and CPU support follow the actual build environment.

## Validation

```sh
python -m unittest discover -s tests -v
python tests/smoke_bundle.py
```

The second command requires a built `dist/DevServices` app. Tests use temporary settings and disposable service processes; they do not start or stop your configured services.

The backend intentionally binds to `127.0.0.1`; keep it local. Commands run with your account's permissions. Only configure commands you trust. Hosted accounts, remote access, machine pairing, automatic updates, startup-at-login and native Windows/Linux system-service management are future work.
