#!/usr/bin/env vpython3
# Copyright 2026 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Integration tests for update_depot_tools."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


DEPOT_TOOLS_ROOT = Path(__file__).resolve().parent.parent


@unittest.skipIf(os.name == "nt", "requires the Unix update script")
class UpdateDepotToolsTest(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        temp_path = Path(self.temp_dir.name)
        self.origin = temp_path / "origin"
        self.checkout = temp_path / "checkout"

        self._git(self.origin.parent, "init", "-b", "main", self.origin)
        self._configure_user(self.origin)
        shutil.copy2(DEPOT_TOOLS_ROOT / "update_depot_tools", self.origin)
        (self.origin / "cipd_bin_setup.sh").write_text(
            "cipd_bin_setup() { :; }\n", encoding="utf-8"
        )
        (self.origin / "conflict.txt").write_text("base\n", encoding="utf-8")
        self._git(self.origin, "add", ".")
        self._git(self.origin, "commit", "-m", "initial")

        self._git(self.origin.parent, "clone", self.origin, self.checkout)
        self._configure_user(self.checkout)
        self._git(self.checkout, "switch", "-c", "philwo")

    def _git(self, cwd, *args, check=True):
        return subprocess.run(
            ["git", *(str(arg) for arg in args)],
            cwd=cwd,
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _configure_user(self, repo):
        self._git(repo, "config", "user.name", "depot_tools test")
        self._git(repo, "config", "user.email", "depot-tools-test@example.com")

    def _commit_file(self, repo, name, contents, message):
        (repo / name).write_text(contents, encoding="utf-8")
        self._git(repo, "add", name)
        self._git(repo, "commit", "-m", message)

    def _run_updater(self):
        env = os.environ.copy()
        env.update(
            {
                "DEPOT_TOOLS_BOOTSTRAP_PYTHON3": "0",
                "DEPOT_TOOLS_DIR": str(self.checkout),
                "DEPOT_TOOLS_UPDATE": "1",
                "USER": "depot-tools-test",
            }
        )
        return subprocess.run(
            [self.checkout / "update_depot_tools"],
            cwd=self.checkout.parent,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_rebases_local_commits(self):
        self._commit_file(self.checkout, "local.txt", "local\n", "local patch")
        self._commit_file(
            self.origin, "upstream.txt", "upstream\n", "upstream change"
        )

        result = self._run_updater()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            self._git(
                self.checkout, "symbolic-ref", "--short", "HEAD"
            ).stdout.strip(),
            "philwo",
        )
        self.assertEqual(
            self._git(
                self.checkout, "rev-list", "--count", "origin/main..HEAD"
            ).stdout.strip(),
            "1",
        )
        self.assertEqual(
            self._git(self.checkout, "log", "-1", "--format=%s").stdout.strip(),
            "local patch",
        )
        self._git(
            self.checkout, "merge-base", "--is-ancestor", "origin/main", "HEAD"
        )

    def test_rebase_conflict_fails_and_aborts(self):
        self._commit_file(
            self.checkout, "conflict.txt", "local\n", "local conflicting patch"
        )
        local_head = self._git(
            self.checkout, "rev-parse", "HEAD"
        ).stdout.strip()
        self._commit_file(
            self.origin,
            "conflict.txt",
            "upstream\n",
            "upstream conflicting change",
        )

        result = self._run_updater()

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("depot_tools update failed", result.stderr)
        self.assertEqual(
            self._git(
                self.checkout, "symbolic-ref", "--short", "HEAD"
            ).stdout.strip(),
            "philwo",
        )
        self.assertEqual(
            self._git(self.checkout, "rev-parse", "HEAD").stdout.strip(),
            local_head,
        )
        self.assertEqual(
            self._git(self.checkout, "status", "--porcelain").stdout, ""
        )


if __name__ == "__main__":
    unittest.main()
