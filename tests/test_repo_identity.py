"""Repository root and GitHub slug for submodule checkouts and SSH host aliases."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from factory import config


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "protocol.file.allow=always", *args], cwd=cwd, check=True,
                   capture_output=True, env={"GIT_CONFIG_GLOBAL": "/dev/null", "PATH": "/usr/bin:/bin:/opt/homebrew/bin",
                                             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


class RepoIdentityTest(unittest.TestCase):
    def test_slug_ignores_ssh_host_alias(self) -> None:
        for url in ("git@github.com-qt:O/R.git", "git@github.com-personal:O/R",
                    "ssh://git@github.com-qt/O/R.git", "https://github.com/O/R.git", "https://github.com/O/R/"):
            with self.subTest(url=url):
                match = config._GITHUB_SLUG.search(url)
                self.assertEqual(match[1] if match else None, "O/R")

    def test_submodule_root_is_its_checkout_not_the_superproject_git_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp).resolve()
            child, parent = tmp / "child", tmp / "parent"
            for repo in (child, parent):
                repo.mkdir()
                git(repo, "init", "-q", "-b", "main")
                git(repo, "commit", "-q", "--allow-empty", "-m", "init")
            git(parent, "submodule", "add", "-q", str(child), "nested/child")
            sub = parent / "nested" / "child"
            git(sub, "remote", "set-url", "origin", "git@github.com-qt:O/child.git")
            self.assertEqual(config.repo_root(sub), sub)
            self.assertEqual(config.remote_slug(config.repo_root(sub), "origin"), "O/child")
            self.assertEqual(config.repo_root(parent), parent)


if __name__ == "__main__":
    unittest.main()
