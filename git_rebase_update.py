#!/usr/bin/env python3
# Copyright 2014 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""
Tool to update all branches to have the latest changes from their upstreams.
"""

import argparse
import collections
import logging
import sys
import textwrap
import os

from fnmatch import fnmatch
from pprint import pformat

import gclient_utils
import git_common as git
import setup_color
import from_third_party

colorama = from_third_party.import_module("colorama")

STARTING_BRANCH_KEY = "depot-tools.rebase-update.starting-branch"
STARTING_WORKDIR_KEY = "depot-tools.rebase-update.starting-workdir"

RESET = colorama.Fore.RESET + colorama.Back.RESET + colorama.Style.RESET_ALL
BRIGHT = colorama.Style.BRIGHT


def find_return_branch_workdir():
    """Finds the branch and working directory which we should return to after
    rebase-update completes.

    These values may persist across multiple invocations of rebase-update, if
    rebase-update runs into a conflict mid-way.
    """
    return_branch = git.get_config(STARTING_BRANCH_KEY)
    workdir = git.get_config(STARTING_WORKDIR_KEY)
    if not return_branch:
        workdir = os.getcwd()
        git.set_config(STARTING_WORKDIR_KEY, workdir)
        return_branch = git.current_branch()
        if return_branch != "HEAD":
            git.set_config(STARTING_BRANCH_KEY, return_branch)

    return return_branch, workdir


def get_branches_in_other_worktrees():
    """Returns a set of branches checked out in other worktrees."""
    output = git.run("worktree", "list", "--porcelain")
    branches = set()
    current_worktree_path = os.path.realpath(
        git.run("rev-parse", "--show-toplevel").strip()
    )

    worktree_path = None
    for line in output.splitlines():
        if line.startswith("worktree "):
            worktree_path = line[len("worktree ") :]
        elif line.startswith("branch refs/heads/"):
            worktree_branch = line[len("branch refs/heads/") :]
            if (
                worktree_path
                and os.path.realpath(worktree_path) != current_worktree_path
            ):
                branches.add(worktree_branch)

    return branches


def fetch_remotes(branch_tree):
    """Fetches all remotes which are needed to update |branch_tree|."""
    tag_set = git.tags()
    if any(parent in tag_set for parent in branch_tree.values()):
        # Need to fetch all because we don't know what remote the tag comes from.
        git.run_with_stderr(
            "fetch", "--all", stdout=sys.stdout, stderr=sys.stderr
        )
        return

    remotes = set()
    parents = set(branch_tree.values())
    if parents:
        fetchspec_map = {}
        all_fetchspec_configs = git.get_config_regexp(r"^remote\..*\.fetch$")
        for key, fetchspec in all_fetchspec_configs:
            dest_spec = fetchspec.partition(":")[2]
            remote_name = key[len("remote.") : -len(".fetch")]
            fetchspec_map[dest_spec] = remote_name

        full_refs = git.run(
            "rev-parse", "--symbolic-full-name", "--revs-only", *parents
        ).splitlines()
        for full_ref in full_refs:
            for dest_spec, remote_name in fetchspec_map.items():
                if fnmatch(full_ref, dest_spec):
                    remotes.add(remote_name)
                    break

    git.run_with_stderr(
        "fetch",
        "--multiple",
        *sorted(remotes),
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


def root_branch_name():
    """Returns the name of the local branch that mirrors the root upstream.

    For a root of "origin/master" this is "master". The remote prefix is
    matched against the configured remotes, because remote names may contain
    slashes. A root that is not under any remote is returned unchanged.
    """
    root = git.root().removeprefix("refs/remotes/")
    remotes = git.run("remote").splitlines()
    # Prefer the longest match in case one remote name prefixes another.
    for remote in sorted(remotes, key=len, reverse=True):
        if root.startswith(remote + "/"):
            return root[len(remote) + 1 :]
    return root


def remove_empty_branches(branch_tree, worktree_branches):
    tag_set = git.tags()
    ensure_root_checkout = git.once(lambda: git.run("checkout", git.root()))

    # Keep the local copy of the root (such as master for origin/master) even
    # when it has no commits of its own: it is the base that branches track.
    root_branch = root_branch_name()

    # Resolve tree SHAs for all branches and parents to check for empty diffs.
    all_refs = list(set(branch_tree.keys()).union(branch_tree.values()))
    tree_hashes = {}
    if all_refs:
        tree_hashes = dict(
            zip(all_refs, git.hash_multi(*[f"{r}:" for r in all_refs]))
        )

    deletions = {}
    reparents = {}
    downstreams = collections.defaultdict(list)
    for branch, parent in git.topo_iter(branch_tree, top_down=False):
        if git.is_dormant(branch):
            continue
        if branch == root_branch:
            continue

        downstreams[parent].append(branch)
        branch_tree_hash = tree_hashes[branch]
        parent_tree_hash = tree_hashes[parent]

        # If branch and parent have the same tree, then branch has to be marked
        # for deletion and its children and grand-children reparented to parent.
        if branch_tree_hash == parent_tree_hash:
            if branch in worktree_branches:
                print(
                    "Skipping deletion of branch checked out in another worktree",
                    format_branch_name(branch),
                )
                continue

            ensure_root_checkout()

            logging.debug("branch %s merged to %s", branch, parent)

            # Mark branch for deletion while remembering the ordering, then add
            # all its children as grand-children of its parent and record
            # reparenting information if necessary.
            deletions[branch] = len(deletions)

            for down in downstreams[branch]:
                if down in deletions:
                    continue

                # Record the new and old parent for down, or update such a
                # record if it already exists. Keep track of the ordering so
                # that reparenting happen in topological order.
                downstreams[parent].append(down)
                if down not in reparents:
                    reparents[down] = (len(reparents), parent, branch)
                else:
                    order, _, old_parent = reparents[down]
                    reparents[down] = (order, parent, old_parent)

    # Apply all reparenting recorded, in order.
    for branch, value in sorted(reparents.items(), key=lambda x: x[1][0]):
        _, parent, old_parent = value
        if parent in tag_set:
            git.set_branch_config(branch, "remote", ".")
            git.set_branch_config(branch, "merge", "refs/tags/%s" % parent)
            print(
                "Reparented %s to track %s [tag] (was tracking %s)"
                % (branch, parent, old_parent)
            )
        else:
            git.run("branch", "--set-upstream-to", parent, branch)
            print(
                "Reparented %s to track %s (was tracking %s)"
                % (branch, parent, old_parent)
            )

    # Apply all deletions recorded, in order.
    if deletions:
        branches_to_delete = [
            branch
            for branch, _ in sorted(deletions.items(), key=lambda x: x[1])
        ]
        print(git.run("branch", "-d", *branches_to_delete))


def format_branch_name(branch):
    return BRIGHT + branch + RESET


def get_descendants(branch, branch_tree):
    """Returns the descendants of `branch` in `branch_tree`, nearest first."""
    children = collections.defaultdict(list)
    for child, parent in branch_tree.items():
        children[parent].append(child)
    descendants = []
    queue = collections.deque(sorted(children[branch]))
    while queue:
        descendant = queue.popleft()
        descendants.append(descendant)
        queue.extend(sorted(children[descendant]))
    return descendants


def find_landed_with(branch, parent, start_hash, descendants):
    """Returns the branch whose changes show that `branch` landed in `parent`.

    `branch` landed if its own changes since `start_hash` are contained in
    `parent`, e.g. when it landed squashed.

    If merging these changes into `parent` conflicts instead, later commits
    that landed after `branch`, e.g. a stacked child CL, may have modified the
    same lines. In that case, the changes of the whole stack from `branch` up
    to one of its descendants are contained in `parent`.

    Returns `branch`, one of `descendants`, or None if `branch` did not land.
    """
    parent_tree = git.run("rev-parse", f"{parent}^{{tree}}")
    tree = git.merge_tree(parent, branch, start_hash)
    if tree == parent_tree:
        return branch
    if tree is not None:
        # The merge is clean but modifies `parent`: some changes of `branch`
        # did not land.
        return None
    start_tree = git.run("rev-parse", f"{start_hash}^{{tree}}")
    for descendant in descendants:
        # Ignore descendants that are not based on the current `branch`, e.g.
        # when it was amended after they were last rebased: they would not
        # reflect the latest changes of `branch`.
        if (
            git.run_with_retcode(
                "merge-base", "--is-ancestor", branch, descendant
            )
            != 0
        ):
            continue
        # Ignore descendants that undo all of the stack's changes, e.g. a
        # revert of `branch`: they are trivially contained in any target.
        if git.run("rev-parse", f"{descendant}^{{tree}}") == start_tree:
            continue
        if git.is_branch_contained_in(descendant, start_hash, parent):
            return descendant
    return None


def rebase_branch(
    branch,
    parent,
    start_hash,
    no_squash,
    branches_in_other_worktrees,
    descendants=(),
):
    """Rebases `branch` onto `parent` from `start_hash`.

    Attempts fast in-memory rebasing via `git replay` to avoid disk I/O and
    checkout churn. If that is not possible (e.g. GPG signing is enabled) or
    conflicts, checks in memory whether `branch` already landed in `parent`,
    in which case it is moved to `parent`. Otherwise, falls back to standard
    porcelain `git rebase`, which is always the case for the currently
    checked-out branch and for branches checked out in other worktrees.
    Callers can detach HEAD beforehand so that the current branch is also
    rebased in memory.

    Args:
        branch: The local branch name to rebase.
        parent: The upstream parent ref or branch name.
        start_hash: The merge-base commit SHA between branch and parent.
        no_squash: If True, does not check whether the changes of `branch`,
            alone or together with its descendants, landed squashed when it
            can't be replayed.
        branches_in_other_worktrees: Set of branch names checked out in other worktrees.
        descendants: Descendants of `branch`, nearest first. Used to detect that
            `branch` landed along with one of them.

    Returns:
        True if the branch rebased successfully, False on failure.
    """
    logging.debug(
        "considering %s(%s) -> %s(%s) : %s",
        branch,
        git.hash_one(branch),
        parent,
        git.hash_one(parent),
        start_hash,
    )

    # If parent has FROZEN commits, don't base branch on top of them. Instead,
    # base branch on top of whatever commit is before them.
    back_ups = 0
    orig_parent = parent
    while git.FREEZE_MATCHER.match(
        git.run("log", "-n1", "--format=%B", parent, "--").strip()
    ):
        back_ups += 1
        parent = git.run("rev-parse", parent + "~")

    if back_ups:
        logging.debug(
            "Backed parent up by %d from %s to %s",
            back_ups,
            orig_parent,
            parent,
        )

    if git.hash_one(parent) != start_hash:
        print("Rebasing:", format_branch_name(branch))

        # Moving a branch in memory only updates its ref, so skip branches
        # whose checked-out files would be left stale:
        # - The current branch: the porcelain rebase below updates the working
        #   tree. Callers can detach HEAD beforehand, like main() does, so the
        #   current branch can be moved in memory too.
        # - Branches checked out in other worktrees: don't move their ref behind
        #   that worktree's back. This matches `git rebase` behavior.
        if (
            branch != git.current_branch()
            and branch not in branches_in_other_worktrees
        ):
            old_sha = git.hash_one(branch)
            new_sha = None

            # Branches without commits of their own are simply fast-forwarded.
            # Otherwise, attempt in-memory replay. We bypass git replay when
            # commit.gpgsign is enabled because git replay does not sign
            # commits and would produce unsigned commits.
            # Note: git replay is experimental in upstream Git; any failure or
            # conflict gracefully falls back to the steps below.
            if old_sha == start_hash:
                new_sha = git.hash_one(parent)
            elif not git.get_gpg_sign_args():
                new_sha = git.replay_rebase(parent, start_hash, branch)

            # Rebasing a branch that landed conflicts, e.g. when it landed
            # squashed or along with later changes to the same lines. Detect
            # it in memory, before `git rebase` checks it out.
            if new_sha is None and not no_squash:
                landed_with = find_landed_with(
                    branch, parent, start_hash, descendants
                )
                if landed_with:
                    message = f"{format_branch_name(branch)} landed upstream"
                    if landed_with != branch:
                        landed_with_name = format_branch_name(landed_with)
                        message += f" along with {landed_with_name}"
                    print(f"{message} (was {old_sha[:10]})")
                    new_sha = git.hash_one(parent)

            if new_sha:
                git.update_refs_atomic(
                    [(branch, new_sha, old_sha)], "rebase-update"
                )
                git.remove_merge_base(branch)
                git.get_or_create_merge_base(branch, orig_parent)
                return True

        rebase_ret = git.rebase(parent, start_hash, branch)
        if not rebase_ret.success:
            # `git rebase` can fail before starting, e.g. when `branch` is
            # checked out in another worktree. Report git's own error then.
            if not git.in_rebase():
                print(f"Failed to rebase {format_branch_name(branch)}:")
                print((rebase_ret.stderr or rebase_ret.stdout).strip())
                if branch in branches_in_other_worktrees:
                    print(
                        textwrap.dedent(
                            f"""\

                        {branch} is checked out in another worktree. Either:
                         * detach HEAD in that worktree
                           (`git checkout --detach` there) so it can be
                           rebased here; OR
                         * skip it by passing `--skip-worktrees`.

                        And then run `git rebase-update -n` to resume.
                        """
                        )
                    )
                return False
            print(
                textwrap.dedent(
                    f"""\
                Failed! You probably have a real merge conflict.

                Your working copy is in mid-rebase. Either:
                 * completely resolve like a normal git-rebase; OR
                 * abort the rebase and mark this branch as dormant:
                       git rebase --abort && \\
                       git config branch.{branch}.dormant true

                And then run `git rebase-update -n` to resume.
                """
                )
            )
            return False
    else:
        print("%s up-to-date" % format_branch_name(branch))

    git.remove_merge_base(branch)
    git.get_or_create_merge_base(branch, orig_parent)

    return True


def with_downstream_branches(base_branches, branch_tree):
    """Returns a set of base_branches and all downstream branches."""
    downstream_branches = set()
    for branch, parent in git.topo_iter(branch_tree):
        if parent in base_branches or parent in downstream_branches:
            downstream_branches.add(branch)

    return downstream_branches.union(base_branches)


def main(args=None):
    if gclient_utils.IsEnvCog():
        print(
            "rebase-update command is not supported. Please navigate to source "
            "control view in the activity bar to rebase your changes.",
            file=sys.stderr,
        )
        return 1
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--keep-going",
        "-k",
        action="store_true",
        help="Keep processing past failed rebases.",
    )
    parser.add_argument(
        "--no_fetch",
        "--no-fetch",
        "-n",
        action="store_true",
        help="Skip fetching remotes.",
    )
    parser.add_argument(
        "--current", action="store_true", help="Only rebase the current branch."
    )
    parser.add_argument(
        "--tree",
        action="store_true",
        help="Rebase all branches downstream from the selected branch(es).",
    )
    parser.add_argument(
        "branches",
        nargs="*",
        help="Branches to be rebased. All branches are assumed "
        "if none specified.",
    )
    parser.add_argument(
        "--keep-empty",
        "-e",
        action="store_true",
        help="Do not automatically delete empty branches.",
    )
    parser.add_argument(
        "--no-squash",
        action="store_true",
        help="Do not check whether a branch's commits already landed upstream "
        "in squashed form.",
    )
    parser.add_argument(
        "--skip-worktree",
        "--skip-worktrees",
        action=argparse.BooleanOptionalAction,
        dest="skip_worktrees",
        default=True,
        help=(
            "Skip branches checked out in a different worktree. Enabled by "
            "default."
        ),
    )

    opts = parser.parse_args(args)

    if opts.verbose:  # pragma: no cover
        logging.getLogger().setLevel(logging.DEBUG)

    # TODO(iannucci): snapshot all branches somehow, so we can implement
    #                 `git rebase-update --undo`.
    #   * Perhaps just copy packed-refs + refs/ + logs/ to the side?
    #     * commit them to a secret ref?
    #       * Then we could view a summary of each run as a
    #         `diff --stat` on that secret ref.

    if git.in_rebase():
        # TODO(iannucci): Be able to resume rebase with flags like --continue,
        # etc.
        print(
            "Rebase in progress. Please complete the rebase before running "
            "`git rebase-update`."
        )
        return 1

    return_branch, return_workdir = find_return_branch_workdir()
    os.chdir(git.run("rev-parse", "--show-toplevel"))

    if git.current_branch() == "HEAD":
        if git.run("status", "--porcelain", "--ignore-submodules=all"):
            print(
                "Cannot rebase-update with detached head + uncommitted changes."
            )
            return 1
    else:
        git.freeze()  # just in case there are any local changes.

    branches_to_rebase = set(opts.branches)
    if opts.current:
        branches_to_rebase.add(git.current_branch())

    skipped, branch_tree = git.get_branch_tree(use_limit=not opts.current)
    if opts.tree:
        branches_to_rebase = with_downstream_branches(
            branches_to_rebase, branch_tree
        )

    if branches_to_rebase:
        skipped = set(skipped).intersection(branches_to_rebase)
    for branch in skipped:
        print("Skipping %s: No upstream specified" % format_branch_name(branch))

    if not opts.no_fetch:
        fetch_remotes(branch_tree)

    merge_base = {}
    for branch, parent in branch_tree.items():
        merge_base[branch] = git.get_or_create_merge_base(branch, parent)

    logging.debug("branch_tree: %s" % pformat(branch_tree))
    logging.debug("merge_base: %s" % pformat(merge_base))

    # Detach HEAD, without changing the working tree, so that every branch can
    # be rebased in memory. The return branch is checked out again at the end.
    if git.current_branch() != "HEAD":
        git.run("checkout", "--quiet", "--detach")

    retcode = 0
    unrebased_branches = []
    branches_in_other_worktrees = get_branches_in_other_worktrees()
    worktree_branches = (
        branches_in_other_worktrees if opts.skip_worktrees else set()
    )
    # Rebase each branch starting with the root-most branches and working
    # towards the leaves.
    for branch, parent in git.topo_iter(branch_tree):
        # Only rebase specified branches, unless none specified.
        if branches_to_rebase and branch not in branches_to_rebase:
            continue
        if branch in worktree_branches:
            print(
                "Skipping branch checked out in another worktree",
                format_branch_name(branch),
            )
            continue
        if git.is_dormant(branch):
            print("Skipping dormant branch", format_branch_name(branch))
        else:
            ret = rebase_branch(
                branch,
                parent,
                merge_base[branch],
                opts.no_squash,
                branches_in_other_worktrees,
                get_descendants(branch, branch_tree),
            )
            if not ret:
                retcode = 1

                if opts.keep_going:
                    print("--keep-going set, continuing with next branch.")
                    unrebased_branches.append(branch)
                    if git.in_rebase():
                        git.run_with_retcode("rebase", "--abort")
                    if git.in_rebase():  # pragma: no cover
                        print(
                            "Failed to abort rebase. Something is really wrong."
                        )
                        break
                else:
                    break

    if unrebased_branches:
        print()
        print("The following branches could not be cleanly rebased:")
        for branch in unrebased_branches:
            print("  %s" % format_branch_name(branch))

    if not retcode:
        if not opts.keep_empty:
            remove_empty_branches(branch_tree, worktree_branches)

    # Restore return_branch and thaw uncommitted changes if everything succeeded
    # OR if --keep-going was used (since --keep-going aborts mid-rebase conflicts,
    # ensuring the working tree is clean and ready to return to starting state).
    if not retcode or (opts.keep_going and not git.in_rebase()):
        # return_branch may not be there any more.
        if return_branch in git.branches(use_limit=False):
            git.run("checkout", return_branch)
            git.thaw()
        else:
            root_branch = git.root()
            if return_branch != "HEAD":
                print(
                    "%s was merged with its parent, checking out %s instead."
                    % (
                        git.unicode_repr(return_branch),
                        git.unicode_repr(root_branch),
                    )
                )
            git.run("checkout", root_branch)

        # return_workdir may also not be there any more.
        if return_workdir:
            try:
                os.chdir(return_workdir)
            except OSError as e:
                print(
                    "Unable to return to original workdir %r: %s"
                    % (return_workdir, e)
                )
        git.set_config(STARTING_BRANCH_KEY, "")
        git.set_config(STARTING_WORKDIR_KEY, "")

        print()
        print("Running `git gc --auto` - Ctrl-C to abort is OK.")
        git.run("gc", "--auto")

    return retcode


if __name__ == "__main__":  # pragma: no cover
    setup_color.init()
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.stderr.write("interrupted\n")
        sys.exit(1)
