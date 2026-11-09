"""Exercise real Git histories using disposable repositories and a local bare remote."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import git_ops


class GitPullTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dashboard git ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.remote, self.source, self.project = (self.base / name for name in ("remote.git", "source", "project"))
        self.git(self.base, "init", "--bare", str(self.remote))
        self.git(self.base, "init", "-b", "main", str(self.source))
        self.commit(self.source, "initial.txt", "initial")
        self.git(self.source, "remote", "add", "origin", str(self.remote))
        self.git(self.source, "push", "-u", "origin", "main")
        self.git(self.base, "clone", "-b", "main", str(self.remote), str(self.project))
        self.output = []

    def git(self, folder, *args):
        return subprocess.run(["git", "-c", "user.name=Dashboard Test", "-c", "user.email=test@example.invalid",
                               "-c", "commit.gpgsign=false", *args], cwd=folder,
                              capture_output=True, text=True, check=True).stdout.strip()

    def commit(self, repo, name, content):
        (repo / name).write_text(content, encoding="utf-8")
        self.git(repo, "add", name)
        self.git(repo, "commit", "-m", content)

    def pull(self):
        git_ops.pull(str(self.project), os.environ, self.output.append)

    def test_fast_forward_from_project_subfolder_and_repeat(self):
        self.commit(self.source, "update.txt", "remote update")
        self.git(self.source, "push")
        nested = self.project / "nested"
        nested.mkdir()
        git_ops.pull(str(nested), os.environ, self.output.append)
        self.assertEqual((self.project / "update.txt").read_text(), "remote update")
        self.assertIn("Branch: main", self.output[-1])
        self.pull()
        self.assertIn("Already up to date", self.output[-1])

    def test_local_changes_and_untracked_files_remain_untouched(self):
        for name in ("initial.txt", "untracked.txt"):
            with self.subTest(name=name):
                original = self.git(self.project, "rev-parse", "HEAD")
                (self.project / name).write_text("local work")
                with self.assertRaisesRegex(RuntimeError, "uncommitted or untracked"):
                    self.pull()
                self.assertEqual((self.project / name).read_text(), "local work")
                self.assertEqual(self.git(self.project, "rev-parse", "HEAD"), original)
                if name == "initial.txt":
                    self.git(self.project, "restore", name)

    def test_diverged_branches_are_not_merged_or_reset(self):
        self.commit(self.project, "local.txt", "local commit")
        original = self.git(self.project, "rev-parse", "HEAD")
        self.commit(self.source, "remote.txt", "remote commit")
        self.git(self.source, "push")
        with self.assertRaises(RuntimeError):
            self.pull()
        self.assertEqual(self.git(self.project, "rev-parse", "HEAD"), original)
        self.assertTrue((self.project / "local.txt").exists())
        self.assertFalse((self.project / "remote.txt").exists())

    def test_autostash_configuration_cannot_hide_changes(self):
        self.git(self.project, "config", "merge.autoStash", "true")
        (self.project / "initial.txt").write_text("local work")
        with self.assertRaisesRegex(RuntimeError, "uncommitted"):
            self.pull()
        self.assertEqual(self.git(self.project, "stash", "list"), "")

    def test_detached_head_and_missing_upstream_refused(self):
        self.git(self.project, "checkout", "--detach")
        with self.assertRaises(RuntimeError):
            self.pull()
        self.git(self.project, "checkout", "main")
        self.git(self.project, "branch", "--unset-upstream")
        with self.assertRaises(RuntimeError):
            self.pull()

    def test_shared_repository_pull_is_serialized(self):
        lock = git_ops._locks.setdefault(str(self.project.resolve()), threading.Lock())
        with lock, self.assertRaisesRegex(RuntimeError, "already pulling"):
            self.pull()

    def test_non_repository_refused(self):
        self.assertIsNone(git_ops.repository(self.base))
        with self.assertRaisesRegex(RuntimeError, "not inside"):
            git_ops.pull(self.base, os.environ, self.output.append)


if __name__ == "__main__":
    unittest.main()
