"""Git updates for local project folders; never stash, reset, or merge divergent work."""
import os
from pathlib import Path
import subprocess
import threading

_guard = threading.Lock()
_locks = {}


def repository(folder):
    if not folder:
        return None
    path = Path(folder).expanduser().resolve()
    if not path.is_dir():
        return None
    return next((p for p in (path, *path.parents) if (p / ".git").exists()), None)


def pull(folder, env, report):
    root = repository(folder)
    if root is None:
        raise RuntimeError("This project folder is not inside a Git repository.")
    with _guard:
        lock = _locks.setdefault(str(root), threading.Lock())
    if not lock.acquire(blocking=False):
        raise RuntimeError("Another service is already pulling this repository. Try again when it finishes.")
    try:
        git_env = {**env, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"}

        def git(*args):
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            try:
                result = subprocess.run(["git", *args], cwd=root, env=git_env,
                                        capture_output=True, text=True, errors="replace", timeout=120, **options)
            except FileNotFoundError:
                raise RuntimeError("Git is not installed or is not on PATH. Install Git, then reopen the dashboard.") from None
            except subprocess.TimeoutExpired:
                raise RuntimeError("Git timed out after 120 seconds. Check network access and Git authentication in your terminal.") from None
            if result.returncode:
                raise RuntimeError((result.stdout + result.stderr).strip()[-12000:] or "Git command failed.")
            return result.stdout.strip(), result.stderr.strip()

        report("Checking branch and local changes…")
        git("rev-parse", "--verify", "HEAD")
        detached, _ = git("rev-parse", "--abbrev-ref", "HEAD")
        if detached == "HEAD":
            raise RuntimeError("Pull stopped: HEAD is detached. Check out a branch in your terminal first.")
        branch, _ = git("symbolic-ref", "--quiet", "--short", "HEAD")
        changes, _ = git("status", "--porcelain", "--untracked-files=normal")
        if changes:
            raise RuntimeError("Pull stopped: this repository has uncommitted or untracked changes. Commit or move them first.\n" + changes[:8000])
        # Resolve the upstream before touching the network; never guess a remote or branch.
        try:
            upstream, _ = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
        except RuntimeError as error:
            raise RuntimeError(f"Pull stopped: configure an upstream for branch {branch} in your terminal first.\n{error}") from None
        report(f"Branch: {branch}\nUpstream: {upstream}\nPulling updates (fast-forward only)…")
        output, errors = git("-c", "merge.autoStash=false", "-c", "rebase.autoStash=false",
                             "pull", "--ff-only", "--no-rebase", "--no-autostash")
        report(f"Branch: {branch}\nUpstream: {upstream}\n{output}\n{errors}\nPull completed. Restart the service when you are ready.".strip())
    finally:
        lock.release()
