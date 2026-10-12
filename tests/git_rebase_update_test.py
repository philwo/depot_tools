#!/usr/bin/env vpython3
# Copyright 2014 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Unit tests for git_rebase_update.py"""

import os
import shutil
import sys
from unittest import mock

DEPOT_TOOLS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, DEPOT_TOOLS_ROOT)

from testing_support import coverage_utils  # noqa: E402
from testing_support import git_test_utils  # noqa: E402


class GitRebaseUpdateTest(git_test_utils.GitRepoReadWriteTestBase):
    REPO_SCHEMA = """
  A B C D E F G
    B H I J K
          J L
  """

    @classmethod
    def getRepoContent(cls, commit):
        # Every commit X gets a file X with the content X
        return {commit: {"data": commit.encode("utf-8")}}

    @classmethod
    def setUpClass(cls):
        super(GitRebaseUpdateTest, cls).setUpClass()
        import git_rebase_update
        import git_new_branch
        import git_reparent_branch
        import git_common
        import git_rename_branch

        cls.reup = git_rebase_update
        cls.rp = git_reparent_branch
        cls.nb = git_new_branch
        cls.mv = git_rename_branch
        cls.gc = git_common
        cls.gc.TEST_MODE = True

    def setUp(self):
        super(GitRebaseUpdateTest, self).setUp()
        # Include branch_K, branch_L to make sure that ABCDEFG all get the
        # same commit hashes as self.repo. Otherwise they get committed with the
        # wrong timestamps, due to commit ordering.
        # TODO(iannucci): Make commit timestamps deterministic in left to right,
        # top to bottom order, not in lexi-topographical order.
        origin_schema = git_test_utils.GitRepoSchema(
            """
    A B C D E F G M N O
      B H I J K
            J L
    """,
            self.getRepoContent,
        )
        self.origin = origin_schema.reify()
        self.origin.git("checkout", "main")
        self.origin.git("branch", "-d", *["branch_" + l for l in "KLG"])  # noqa: E741

        self.repo.git("remote", "add", "origin", self.origin.repo_path)
        self.repo.git(
            "config", "--add", "remote.origin.fetch", "+refs/tags/*:refs/tags/*"
        )
        self.repo.git("update-ref", "refs/remotes/origin/main", "tag_E")
        self.repo.git("branch", "--set-upstream-to", "branch_G", "branch_K")
        self.repo.git("branch", "--set-upstream-to", "branch_K", "branch_L")
        self.repo.git("branch", "--set-upstream-to", "origin/main", "branch_G")

        self.repo.to_schema_refs += ["origin/main"]
        mock.patch("git_rebase_update.RESET", "").start()
        mock.patch("git_rebase_update.BRIGHT", "").start()

    def tearDown(self):
        self.origin.nuke()
        super(GitRebaseUpdateTest, self).tearDown()

    def testRebaseUpdate(self):
        self.repo.git("checkout", "branch_K")

        self.repo.run(self.nb.main, ["foobar"])
        self.assertEqual(
            self.repo.git("rev-parse", "HEAD").stdout,
            self.repo.git("rev-parse", "origin/main").stdout,
        )

        with self.repo.open("foobar", "w") as f:
            f.write("this is the foobar file")
        self.repo.git("add", "foobar")
        self.repo.git_commit("foobar1")

        with self.repo.open("foobar", "w") as f:
            f.write("totes the Foobar file")
        self.repo.git_commit("foobar2")

        self.repo.run(self.nb.main, ["--upstream-current", "int1_foobar"])
        self.repo.run(self.nb.main, ["--upstream-current", "int2_foobar"])
        self.repo.run(self.nb.main, ["--upstream-current", "sub_foobar"])
        with self.repo.open("foobar", "w") as f:
            f.write("some more foobaring")
        self.repo.git("add", "foobar")
        self.repo.git_commit("foobar3")

        self.repo.git("checkout", "branch_K")
        self.repo.run(self.nb.main, ["--upstream-current", "sub_K"])
        with self.repo.open("K", "w") as f:
            f.write("This depends on K")
        self.repo.git_commit("sub_K")

        self.repo.run(self.nb.main, ["old_branch"])
        self.repo.git("reset", "--hard", self.repo["A"])
        with self.repo.open("old_file", "w") as f:
            f.write("old_files we want to keep around")
        self.repo.git("add", "old_file")
        self.repo.git_commit("old_file")
        self.repo.git("config", "branch.old_branch.dormant", "true")

        self.repo.git("checkout", "origin/main")

        self.assertSchema("""
    A B H I J K sub_K
            J L
      B C D E foobar1 foobar2 foobar3
            E F G
    A old_file
    """)
        self.assertEqual(self.repo["A"], self.origin["A"])
        self.assertEqual(self.repo["E"], self.origin["E"])

        with self.repo.open("bob", "wb") as f:
            f.write(b"testing auto-freeze/thaw")

        output, _ = self.repo.capture_stdio(self.reup.main, [])
        self.assertIn("Cannot rebase-update", output)

        self.repo.run(self.nb.main, ["empty_branch"])
        self.repo.run(self.nb.main, ["--upstream-current", "empty_branch2"])

        self.repo.git("checkout", "branch_K")

        output, _ = self.repo.capture_stdio(self.reup.main, [])

        self.assertIn("Rebasing: branch_G", output)
        self.assertIn("Rebasing: branch_K", output)
        self.assertIn("Rebasing: branch_L", output)
        self.assertIn("Rebasing: foobar", output)
        self.assertIn("Rebasing: sub_K", output)
        self.assertIn("Deleted branch branch_G", output)
        self.assertIn("Deleted branch empty_branch", output)
        self.assertIn("Deleted branch empty_branch2", output)
        self.assertIn("Deleted branch int1_foobar", output)
        self.assertIn("Deleted branch int2_foobar", output)
        self.assertIn("Reparented branch_K to track origin/main", output)
        self.assertIn("Reparented sub_foobar to track foobar", output)

        self.assertSchema("""
    A B C D E F G M N O H I J K sub_K
                              K L
                      O foobar1 foobar2 foobar3
    A old_file
    """)

        output, _ = self.repo.capture_stdio(self.reup.main, [])
        self.assertIn("branch_K up-to-date", output)
        self.assertIn("branch_L up-to-date", output)
        self.assertIn("foobar up-to-date", output)
        self.assertIn("sub_K up-to-date", output)

        with self.repo.open("bob") as f:
            self.assertEqual(b"testing auto-freeze/thaw", f.read())

        self.assertEqual(
            self.repo.git("status", "--porcelain").stdout, "?? bob\n"
        )

        self.repo.git("checkout", "origin/main")
        _, err = self.repo.capture_stdio(self.rp.main, [])
        self.assertIn("Must specify new parent somehow", err)
        _, err = self.repo.capture_stdio(self.rp.main, ["foobar"])
        self.assertIn("Must be on the branch", err)

        self.repo.git("checkout", "branch_K")
        _, err = self.repo.capture_stdio(self.rp.main, ["origin/main"])
        self.assertIn("Cannot reparent a branch to its existing parent", err)
        output, _ = self.repo.capture_stdio(self.rp.main, ["foobar"])
        self.assertIn("Rebasing: branch_K", output)
        self.assertIn("Rebasing: sub_K", output)
        self.assertIn("Rebasing: branch_L", output)

        self.assertSchema("""
    A B C D E F G M N O foobar1 foobar2 H I J K L
                                foobar2 foobar3
                                              K sub_K
    A old_file
    """)

        self.repo.git("checkout", "sub_K")
        output, _ = self.repo.capture_stdio(self.rp.main, ["foobar"])
        self.assertIn("You probably have a real merge conflict", output)

        self.assertTrue(self.repo.run(self.gc.in_rebase))

        self.repo.git("rebase", "--abort")
        self.assertIsNone(self.repo.run(self.gc.thaw))

        self.assertSchema("""
    A B C D E F G M N O foobar1 foobar2 H I J K L
                                foobar2 foobar3
    A old_file
                                              K sub_K
    """)

        self.assertEqual(
            self.repo.git("status", "--porcelain").stdout, "?? bob\n"
        )

        branches = self.repo.run(set, self.gc.branches())
        self.assertEqual(
            branches,
            {
                "branch_K",
                "main",
                "sub_K",
                "root_A",
                "branch_L",
                "old_branch",
                "foobar",
                "sub_foobar",
            },
        )

        self.repo.git("checkout", "branch_K")
        self.repo.run(self.mv.main, ["special_K"])

        branches = self.repo.run(set, self.gc.branches())
        self.assertEqual(
            branches,
            {
                "special_K",
                "main",
                "sub_K",
                "root_A",
                "branch_L",
                "old_branch",
                "foobar",
                "sub_foobar",
            },
        )

        self.repo.git("checkout", "origin/main")
        _, err = self.repo.capture_stdio(
            self.mv.main, ["special_K", "cool branch"]
        )
        self.assertIn("fatal: 'cool branch' is not a valid branch name", err)

        self.repo.run(self.mv.main, ["special_K", "cool_branch"])
        branches = self.repo.run(set, self.gc.branches())
        # This check fails with git 2.4 (see crbug.com/487172)
        self.assertEqual(
            branches,
            {
                "cool_branch",
                "main",
                "sub_K",
                "root_A",
                "branch_L",
                "old_branch",
                "foobar",
                "sub_foobar",
            },
        )

        _, branch_tree = self.repo.run(self.gc.get_branch_tree)
        self.assertEqual(branch_tree["sub_K"], "foobar")

    def testRebaseConflicts(self):
        # Pretend that branch_L landed
        self.origin.git("checkout", "main")
        with self.origin.open("L", "w") as f:
            f.write("L")
        self.origin.git("add", "L")
        self.origin.git_commit("L")

        # Add a commit to branch_K so that things fail
        self.repo.git("checkout", "branch_K")
        with self.repo.open("M", "w") as f:
            f.write("NOPE")
        self.repo.git("add", "M")
        self.repo.git_commit("K NOPE")

        # Add a commits to branch_L which will work when squashed
        self.repo.git("checkout", "branch_L")
        self.repo.git("reset", "branch_L~")
        with self.repo.open("L", "w") as f:
            f.write("NOPE")
        self.repo.git("add", "L")
        self.repo.git_commit("L NOPE")
        with self.repo.open("L", "w") as f:
            f.write("L")
        self.repo.git("add", "L")
        self.repo.git_commit("L YUP")

        # start on a branch which will be deleted
        self.repo.git("checkout", "branch_G")

        output, _ = self.repo.capture_stdio(self.reup.main, [])
        self.assertIn("branch.branch_K.dormant true", output)

        output, _ = self.repo.capture_stdio(self.reup.main, [])
        self.assertIn("Rebase in progress", output)

        self.repo.git("checkout", "--theirs", "M")
        self.repo.git("rebase", "--skip")

        output, _ = self.repo.capture_stdio(self.reup.main, [])
        self.assertIn("branch_L landed upstream (was ", output)
        self.assertIn("Deleted branch branch_G", output)
        self.assertIn("Deleted branch branch_L", output)
        self.assertIn("'branch_G' was merged", output)
        self.assertIn("checking out 'origin/main'", output)

    def testRebaseConflictsKeepGoing(self):
        # Pretend that branch_L landed
        self.origin.git("checkout", "main")
        with self.origin.open("L", "w") as f:
            f.write("L")
        self.origin.git("add", "L")
        self.origin.git_commit("L")

        # Add a commit to branch_K so that things fail
        self.repo.git("checkout", "branch_K")
        with self.repo.open("M", "w") as f:
            f.write("NOPE")
        self.repo.git("add", "M")
        self.repo.git_commit("K NOPE")

        # Add a commits to branch_L which will work when squashed
        self.repo.git("checkout", "branch_L")
        self.repo.git("reset", "branch_L~")
        with self.repo.open("L", "w") as f:
            f.write("NOPE")
        self.repo.git("add", "L")
        self.repo.git_commit("L NOPE")
        with self.repo.open("L", "w") as f:
            f.write("L")
        self.repo.git("add", "L")
        self.repo.git_commit("L YUP")

        # start on a branch which will be deleted
        self.repo.git("checkout", "branch_G")

        self.repo.git("config", "branch.branch_K.dormant", "false")
        output, _ = self.repo.capture_stdio(self.reup.main, ["-k"])
        self.assertIn("--keep-going set, continuing with next branch.", output)
        self.assertIn("could not be cleanly rebased:", output)
        self.assertIn("  branch_K", output)

    def _landChange(self, content):
        """Commits `content` to the file `stack` on origin's main branch."""
        with self.origin.open("stack", "w") as f:
            f.write(content)
        self.origin.git("add", "stack")
        self.origin.git_commit(f"Land {content.strip()}")
        return self.origin.git("rev-parse", "HEAD").stdout.strip()

    def _createStackBranch(self, name, upstream, content):
        """Creates `name` tracking `upstream`, writing `content` to `stack`."""
        self.repo.git("checkout", "-b", name, upstream)
        self.repo.git("branch", "--set-upstream-to", upstream, name)
        with self.repo.open("stack", "w") as f:
            f.write(content)
        self.repo.git("add", "stack")
        self.repo.git_commit(name)

    def testSquashLandedBranchCheckedOutInOtherWorktree(self):
        self._createStackBranch("unrelated", "origin/main", "unrelated\n")
        # cl_1 landed squashed: its first commit conflicts with what landed.
        self._createStackBranch("cl_1", "origin/main", "x\n")
        with self.repo.open("stack", "w") as f:
            f.write("1\n")
        self.repo.git("add", "stack")
        self.repo.git_commit("cl_1 fixup")
        self._landChange("1\n")
        # cl_1 is checked out in another worktree, so `git rebase` refuses to
        # rebase it: git's error must be reported, and no other branch touched.
        worktree = self.repo.repo_path + "_worktree"
        self.addCleanup(shutil.rmtree, worktree, ignore_errors=True)
        self.repo.git("checkout", "unrelated")
        self.repo.git("worktree", "add", worktree, "cl_1")
        unrelated = self.repo.git("rev-parse", "unrelated").stdout.strip()

        output, _ = self.repo.capture_stdio(
            self.reup.main, ["--no-skip-worktree"]
        )

        self.assertIn("Failed to rebase cl_1:", output)
        self.assertIn("already used by worktree", output)
        self.assertNotIn("mid-rebase", output)
        self.assertNotIn("landed upstream", output)
        self.assertFalse(self.repo.run(self.gc.in_rebase))
        self.assertEqual(
            self.repo.git("rev-parse", "unrelated").stdout.strip(), unrelated
        )

    def _createStack(self):
        """Creates a stack of 3 branches that all modify the same line."""
        self._createStackBranch("cl_1", "origin/main", "1\n")
        self._createStackBranch("cl_2", "cl_1", "2\n")
        self._createStackBranch("cl_3", "cl_2", "3\n")

    def testLandedStack(self):
        self._createStack()
        # Rebasing cl_1 and cl_2 conflicts, as later changes modified the same
        # line.
        self._landChange("1\n")
        self._landChange("2\n")
        self._landChange("3\n")

        self.repo.git("checkout", "cl_1")
        output, _ = self.repo.capture_stdio(self.reup.main, [])

        self.assertIn("cl_1 landed upstream along with cl_3", output)
        self.assertIn("cl_2 landed upstream along with cl_3", output)
        self.assertIn("Deleted branch cl_1", output)
        self.assertIn("Deleted branch cl_2", output)
        self.assertIn("Deleted branch cl_3", output)
        self.assertFalse(self.repo.run(self.gc.in_rebase))

    def testLandedStackPrefix(self):
        self._createStack()
        self._landChange("1\n")
        self._landChange("2\n")

        self.repo.git("checkout", "cl_1")
        output, _ = self.repo.capture_stdio(self.reup.main, [])

        self.assertIn("cl_1 landed upstream along with cl_2", output)
        self.assertIn("Deleted branch cl_1", output)
        self.assertIn("Deleted branch cl_2", output)
        self.assertNotIn("Deleted branch cl_3", output)
        self.assertIn("Reparented cl_3 to track origin/main", output)
        self.assertFalse(self.repo.run(self.gc.in_rebase))
        self.assertEqual(
            self.repo.git("show", "cl_3:stack").stdout.splitlines(), ["3"]
        )

    def testLandedStackWithLocalChanges(self):
        self._createStack()
        # cl_1 has a change that did not land, which cl_2 and cl_3 include.
        self.repo.git("checkout", "cl_1")
        with self.repo.open("unlanded", "w") as f:
            f.write("unlanded")
        self.repo.git("add", "unlanded")
        self.repo.git(
            "commit", "--amend", "--no-edit", env=self.repo.get_git_commit_env()
        )
        self.repo.git("rebase", "cl_1", "cl_2")
        self.repo.git("rebase", "cl_2", "cl_3")
        self._landChange("1\n")
        self._landChange("2\n")
        self._landChange("3\n")

        self.repo.git("checkout", "cl_1")
        output, _ = self.repo.capture_stdio(self.reup.main, [])

        self.assertNotIn("landed upstream along with", output)
        self.assertNotIn("Deleted branch cl_1", output)
        self.assertTrue(self.repo.run(self.gc.in_rebase))
        self.repo.git("rebase", "--abort")

    def testLandedStackWithStaleDescendant(self):
        self._createStack()
        self._landChange("1\n")
        self._landChange("2\n")
        self._landChange("3\n")
        # cl_1 is amended after cl_2 and cl_3 were last rebased, so they don't
        # reflect its latest changes.
        self.repo.git("checkout", "cl_1")
        with self.repo.open("unlanded", "w") as f:
            f.write("unlanded")
        self.repo.git("add", "unlanded")
        self.repo.git(
            "commit", "--amend", "--no-edit", env=self.repo.get_git_commit_env()
        )

        output, _ = self.repo.capture_stdio(self.reup.main, [])

        self.assertNotIn("landed upstream along with", output)
        self.assertNotIn("Deleted branch cl_1", output)
        self.assertTrue(self.repo.run(self.gc.in_rebase))
        self.repo.git("rebase", "--abort")

    def testLandedStackReverted(self):
        self._createStack()
        self._landChange("1\n")
        self._landChange("2\n")
        self._landChange("3\n")
        # The stack gets reverted, then someone else modifies the same line.
        self._landChange("other\n")

        self.repo.git("checkout", "cl_1")
        output, _ = self.repo.capture_stdio(self.reup.main, [])

        self.assertNotIn("landed upstream along with", output)
        self.assertNotIn("Deleted branch cl_1", output)
        self.assertTrue(self.repo.run(self.gc.in_rebase))
        self.repo.git("rebase", "--abort")

    def testLandedStackDescendantRevertsBranch(self):
        self._createStackBranch("cl_1", "origin/main", "1\n")
        # cl_2 reverts cl_1, so the stack as a whole changes nothing.
        self.repo.git("checkout", "-b", "cl_2", "cl_1")
        self.repo.git("branch", "--set-upstream-to", "cl_1", "cl_2")
        self.repo.git("rm", "stack")
        self.repo.git_commit("cl_2")
        # Rebasing cl_1 conflicts with an unrelated change to the same file.
        self._landChange("other\n")

        self.repo.git("checkout", "cl_1")
        output, _ = self.repo.capture_stdio(self.reup.main, [])

        self.assertNotIn("landed upstream along with", output)
        self.assertNotIn("Deleted branch cl_1", output)
        self.assertTrue(self.repo.run(self.gc.in_rebase))
        self.repo.git("rebase", "--abort")

    def testLandedStackNoSquash(self):
        self._createStack()
        self._landChange("1\n")
        self._landChange("2\n")
        self._landChange("3\n")

        self.repo.git("checkout", "cl_1")
        output, _ = self.repo.capture_stdio(self.reup.main, ["--no-squash"])

        self.assertNotIn("landed upstream", output)
        self.assertNotIn("Deleted branch cl_1", output)
        self.assertTrue(self.repo.run(self.gc.in_rebase))
        self.repo.git("rebase", "--abort")

    def testLandedCheckSkipsDescendantsIfBranchMergesCleanly(self):
        # Replaying cl_1 commit by commit conflicts, but its changes as a whole
        # merge cleanly into origin/main without being contained in it: part of
        # it landed, and `unlanded` didn't.
        self._createStackBranch("cl_1", "origin/main", "x\n")
        with self.repo.open("stack", "w") as f:
            f.write("1\n")
        with self.repo.open("unlanded", "w") as f:
            f.write("unlanded")
        self.repo.git("add", "stack", "unlanded")
        self.repo.git_commit("cl_1 fixup")
        self._createStackBranch("cl_2", "cl_1", "2\n")
        self._landChange("1\n")

        # cl_1 did not land, so its descendants are not examined.
        self.repo.git("checkout", "cl_1")
        with mock.patch(
            "git_common.is_branch_contained_in",
            wraps=self.gc.is_branch_contained_in,
        ) as is_branch_contained_in:
            output, _ = self.repo.capture_stdio(self.reup.main, [])

        is_branch_contained_in.assert_not_called()
        self.assertNotIn("landed upstream", output)
        # The in-memory replay failed, so `git rebase` was left mid-rebase.
        self.assertTrue(self.repo.run(self.gc.in_rebase))
        self.repo.git("rebase", "--abort")

    def testRebaseUpdateSkipWorktrees(self):
        self.repo.git("checkout", "branch_L")
        self.repo.run(
            self.nb.main, ["--upstream-current", "empty_branch_in_worktree"]
        )

        # Mock git.run to return a worktree list that includes branch_L and empty_branch_in_worktree in different worktrees
        original_run = self.gc.run
        repo_root = self.repo.git("rev-parse", "--show-toplevel").stdout.strip()

        def mock_run(*args, **kwargs):
            if args[:3] == ("worktree", "list", "--porcelain"):
                return f"""worktree {repo_root}
HEAD deadbeef
branch refs/heads/branch_K

worktree /path/to/other/worktree
HEAD deaffeed
branch refs/heads/branch_L

worktree /path/to/another/worktree
HEAD feedface
branch refs/heads/empty_branch_in_worktree
"""
            return original_run(*args, **kwargs)

        with mock.patch("git_common.run", side_effect=mock_run):
            output, _ = self.repo.capture_stdio(self.reup.main)

        self.assertIn(
            "Skipping branch checked out in another worktree branch_L", output
        )
        self.assertNotIn(
            "Skipping branch checked out in another worktree branch_K", output
        )
        self.assertIn(
            "Skipping deletion of branch checked out in another worktree empty_branch_in_worktree",
            output,
        )

    def testRebaseUpdateKeepsMainWhenEmpty(self):
        self.repo.git("checkout", "branch_K")
        self.repo.git("branch", "-f", "main", "origin/main")
        self.repo.git("branch", "empty_branch", "origin/main")
        self.repo.git("branch", "--set-upstream-to", "origin/main", "main")
        self.repo.git(
            "branch", "--set-upstream-to", "origin/main", "empty_branch"
        )

        self.repo.run(
            self.reup.remove_empty_branches,
            {"main": "origin/main", "empty_branch": "origin/main"},
            set(),
        )

        branches = self.repo.run(set, self.gc.branches())
        self.assertIn("main", branches)
        self.assertNotIn("empty_branch", branches)

    def testRebaseUpdateKeepsRootBranchWhenEmpty(self):
        """An empty local copy of a non-main root, such as master, is kept."""
        self.repo.git("update-ref", "refs/remotes/origin/master", "tag_E")
        self.repo.run(
            self.gc.set_config, "depot-tools.upstream", "origin/master"
        )
        self.repo.git("checkout", "branch_K")
        self.repo.git("branch", "master", "origin/master")
        self.repo.git("branch", "empty_branch", "origin/master")
        self.repo.git("branch", "--set-upstream-to", "origin/master", "master")
        self.repo.git(
            "branch", "--set-upstream-to", "origin/master", "empty_branch"
        )

        self.repo.run(
            self.reup.remove_empty_branches,
            {"master": "origin/master", "empty_branch": "origin/master"},
            set(),
        )

        branches = self.repo.run(set, self.gc.branches())
        self.assertIn("master", branches)
        self.assertNotIn("empty_branch", branches)

    def testRootBranchNameStripsRemoteContainingSlash(self):
        """The remote prefix is matched against remotes, not the first slash."""
        self.repo.git("remote", "add", "team/fork", self.origin.repo_path)
        self.repo.run(
            self.gc.set_config, "depot-tools.upstream", "team/fork/main"
        )

        self.assertEqual("main", self.repo.run(self.reup.root_branch_name))

    def testTrackTag(self):
        self.origin.git("tag", "tag-to-track", self.origin["M"])
        self.repo.git("tag", "tag-to-track", self.repo["D"])

        self.repo.git("config", "branch.branch_G.remote", ".")
        self.repo.git(
            "config", "branch.branch_G.merge", "refs/tags/tag-to-track"
        )

        self.assertIn(
            "fatal: 'foo bar' is not a valid branch name",
            self.repo.capture_stdio(
                self.nb.main, ["--upstream", "tags/tag-to-track", "foo bar"]
            )[1],
        )

        self.repo.run(
            self.nb.main, ["--upstream", "tags/tag-to-track", "foobar"]
        )

        with self.repo.open("foobar", "w") as f:
            f.write("this is the foobar file")
        self.repo.git("add", "foobar")
        self.repo.git_commit("foobar1")

        with self.repo.open("foobar", "w") as f:
            f.write("totes the Foobar file")
        self.repo.git_commit("foobar2")

        self.assertSchema("""
    A B H I J K
            J L
      B C D E F G
          D foobar1 foobar2
    """)
        self.assertEqual(self.repo["A"], self.origin["A"])
        self.assertEqual(self.repo["G"], self.origin["G"])

        output, _ = self.repo.capture_stdio(self.reup.main, [])
        self.assertIn("Rebasing: branch_G", output)
        self.assertIn("Rebasing: branch_K", output)
        self.assertIn("Rebasing: branch_L", output)
        self.assertIn("Rebasing: foobar", output)
        self.assertEqual(
            self.repo.git("rev-parse", "tags/tag-to-track").stdout.strip(),
            self.origin["M"],
        )

        self.assertSchema("""
    A B C D E F G M N O
                  M H I J K L
                  M foobar1 foobar2
    """)

        _, err = self.repo.capture_stdio(self.rp.main, ["tag F"])
        self.assertIn("fatal: invalid reference", err)

        output, _ = self.repo.capture_stdio(self.rp.main, ["tag_F"])
        self.assertIn("to track tag_F [tag] (was tag-to-track [tag])", output)

        self.assertSchema("""
    A B C D E F G M N O
                  M H I J K L
              F foobar1 foobar2
    """)

        output, _ = self.repo.capture_stdio(self.rp.main, ["tag-to-track"])
        self.assertIn("to track tag-to-track [tag] (was tag_F [tag])", output)

        self.assertSchema("""
    A B C D E F G M N O
                  M H I J K L
                  M foobar1 foobar2
    """)

        output, _ = self.repo.capture_stdio(self.rp.main, ["--root"])
        self.assertIn("to track origin/main (was tag-to-track [tag])", output)

        self.assertSchema("""
    A B C D E F G M N O foobar1 foobar2
                  M H I J K L
    """)

    def testReparentBranchWithoutUpstream(self):
        self.repo.git("branch", "nerp")
        self.repo.git("checkout", "nerp")

        _, err = self.repo.capture_stdio(self.rp.main, ["branch_K"])

        self.assertIn("Unable to determine nerp@{upstream}", err)

    def testReplayRebase(self):
        if not self.repo.run(self.gc.meets_git_version, (2, 55)):
            self.skipTest(
                "git replay with --ref-action=print requires Git >= 2.55"
            )

        self.repo.git("checkout", "branch_K")
        self.repo.run(self.nb.main, ["feature_branch"])
        with self.repo.open("feat", "w") as f:
            f.write("feature work")
        self.repo.git("add", "feat")
        self.repo.git_commit("feat1")

        start_hash = self.repo.git("rev-parse", "branch_K").stdout.strip()
        new_sha = self.repo.run(
            self.gc.replay_rebase, "origin/main", start_hash, "feature_branch"
        )
        self.assertIsNotNone(new_sha)
        old_sha = self.repo.git("rev-parse", "feature_branch").stdout.strip()
        self.repo.run(
            self.gc.update_refs_atomic,
            [("feature_branch", new_sha, old_sha)],
            "test-rebase",
        )
        self.assertEqual(
            self.repo.git("rev-parse", "feature_branch~").stdout.strip(),
            self.repo.git("rev-parse", "origin/main").stdout.strip(),
        )

    def testFetchRemotesWithUnresolvableParent(self):
        # branch_tree contains a valid upstream parent ("origin/main") and an
        # unresolvable parent ref ("origin/nonexistent_parent").
        branch_tree = {
            "branch_K": "origin/main",
            "stale_branch": "origin/nonexistent_parent",
        }
        # Calling fetch_remotes should cleanly fetch origin for valid refs
        # without crashing on the unresolvable parent.
        self.repo.run(self.reup.fetch_remotes, branch_tree)

    def testFetchRemotesWithNoBranches(self):
        # When branch_tree is empty (e.g. no local branches), fetch_remotes
        # should still cleanly fetch default remotes (origin).
        self.repo.run(self.reup.fetch_remotes, {})
        self.assertEqual(
            self.repo.git("rev-parse", "origin/main").stdout,
            self.origin.git("rev-parse", "main").stdout,
        )

    def testRebaseUpdateStartingBranchDiffersFromCurrent(self):
        # Advance origin/main so branches need to be rebased.
        self.origin.git("checkout", "main")
        with self.origin.open("new_upstream_file", "w") as f:
            f.write("upstream change")
        self.origin.git("add", "new_upstream_file")
        self.origin.git_commit("upstream commit")
        self.repo.git("fetch", "origin")

        # Simulate resuming rebase-update where starting-branch ("branch_L")
        # differs from the currently checked-out branch ("branch_K").
        self.repo.git(
            "config", "depot-tools.rebase-update.starting-branch", "branch_L"
        )
        self.repo.git("checkout", "branch_K")

        retcode = self.repo.run(self.reup.main, ["-n"])
        self.assertEqual(0, retcode)
        self.assertEqual(
            "", self.repo.git("status", "--porcelain").stdout.strip()
        )
        self.assertEqual("branch_L", self.repo.run(self.gc.current_branch))


if __name__ == "__main__":
    sys.exit(
        coverage_utils.covered_main(
            (
                os.path.join(DEPOT_TOOLS_ROOT, "git_rebase_update.py"),
                os.path.join(DEPOT_TOOLS_ROOT, "git_new_branch.py"),
                os.path.join(DEPOT_TOOLS_ROOT, "git_reparent_branch.py"),
                os.path.join(DEPOT_TOOLS_ROOT, "git_rename_branch.py"),
            )
        )
    )
