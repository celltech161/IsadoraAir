"""Read-only git adapter tests -- real throwaway git repos, no mocking
of subprocess itself (proves actual git behavior, not an assumption
about it). [P0] 1.1 Phase A."""
from django.test import SimpleTestCase

from updatecenter import git_adapter as ga
from .gitfixtures import FakeRepo


class ForbiddenSubcommandTests(SimpleTestCase):
    def test_checkout_is_refused_before_any_subprocess_runs(self):
        with self.assertRaises(ValueError):
            ga.run_git(["checkout", "main"], cwd=".")

    def test_reset_is_refused(self):
        with self.assertRaises(ValueError):
            ga.run_git(["reset", "--hard"], cwd=".")

    def test_merge_is_refused(self):
        with self.assertRaises(ValueError):
            ga.run_git(["merge", "origin/main"], cwd=".")

    def test_pull_is_refused(self):
        with self.assertRaises(ValueError):
            ga.run_git(["pull"], cwd=".")

    def test_stash_is_refused(self):
        with self.assertRaises(ValueError):
            ga.run_git(["stash"], cwd=".")

    def test_clean_is_refused(self):
        with self.assertRaises(ValueError):
            ga.run_git(["clean", "-fd"], cwd=".")

    def test_branch_is_refused(self):
        with self.assertRaises(ValueError):
            ga.run_git(["branch", "-D", "main"], cwd=".")

    def test_submodule_is_refused(self):
        with self.assertRaises(ValueError):
            ga.run_git(["submodule", "update"], cwd=".")

    def test_push_is_refused(self):
        with self.assertRaises(ValueError):
            ga.run_git(["push"], cwd=".")

    def test_unrecognized_subcommand_is_refused(self):
        with self.assertRaises(ValueError):
            ga.run_git(["some-made-up-subcommand"], cwd=".")

    def test_string_args_instead_of_list_rejected(self):
        """A single shell-joined string is exactly the shape that would
        indicate a future call site drifted toward shell=True-style
        construction -- refused at the type level."""
        with self.assertRaises(TypeError):
            ga.run_git("status --porcelain", cwd=".")


class CleanRepoTests(SimpleTestCase):
    def test_clean_repo_is_not_dirty(self):
        with FakeRepo() as repo:
            self.assertFalse(ga.get_worktree_dirty(repo.work))

    def test_current_branch_is_main(self):
        with FakeRepo() as repo:
            self.assertEqual(ga.get_current_branch(repo.work), "main")

    def test_not_detached(self):
        with FakeRepo() as repo:
            self.assertFalse(ga.is_detached_head(repo.work))

    def test_origin_url_resolves(self):
        with FakeRepo() as repo:
            url = ga.get_origin_url(repo.work)
            self.assertIsNotNone(url)
            self.assertIn(str(repo.origin), url)

    def test_rev_parse_head(self):
        with FakeRepo() as repo:
            head = repo.rev_parse("HEAD")
            self.assertEqual(ga.rev_parse(repo.work, "HEAD"), head)

    def test_commit_exists_true_for_real_commit(self):
        with FakeRepo() as repo:
            self.assertTrue(ga.commit_exists(repo.work, repo.rev_parse("HEAD")))

    def test_commit_exists_false_for_fake_sha(self):
        with FakeRepo() as repo:
            self.assertFalse(ga.commit_exists(repo.work, "a" * 40))

    def test_commit_exists_false_for_malformed_input(self):
        with FakeRepo() as repo:
            self.assertFalse(ga.commit_exists(repo.work, "not-a-sha; rm -rf /"))

    def test_ahead_behind_zero_zero_when_synced(self):
        with FakeRepo() as repo:
            self.assertEqual(ga.ahead_behind(repo.work, "HEAD", "origin/main"), (0, 0))

    def test_no_origin_remote(self):
        import subprocess
        with FakeRepo() as repo:
            subprocess.run(["git", "remote", "remove", "origin"], cwd=str(repo.work), check=True, capture_output=True)
            self.assertIsNone(ga.get_origin_url(repo.work))


class DirtyAndDetachedTests(SimpleTestCase):
    def test_dirty_untracked_file_detected(self):
        with FakeRepo() as repo:
            repo.dirty_untracked()
            self.assertTrue(ga.get_worktree_dirty(repo.work))

    def test_detached_head_detected(self):
        with FakeRepo() as repo:
            sha = repo.rev_parse("HEAD")
            repo.checkout_detached(sha)
            self.assertTrue(ga.is_detached_head(repo.work))
            self.assertIsNone(ga.get_current_branch(repo.work))


class FetchTests(SimpleTestCase):
    def test_fetch_succeeds_against_real_origin(self):
        with FakeRepo() as repo:
            self.assertTrue(ga.fetch_remote(repo.work))

    def test_fetch_does_not_touch_working_tree(self):
        with FakeRepo() as repo:
            repo.diverge_origin()
            before = ga.get_worktree_dirty(repo.work)
            head_before = ga.rev_parse(repo.work, "HEAD")
            ok = ga.fetch_remote(repo.work)
            self.assertTrue(ok)
            self.assertEqual(ga.get_worktree_dirty(repo.work), before)
            self.assertEqual(ga.rev_parse(repo.work, "HEAD"), head_before)

    def test_fetch_updates_remote_tracking_ref(self):
        with FakeRepo() as repo:
            repo.diverge_origin()
            before = ga.rev_parse(repo.work, "origin/main")
            ga.fetch_remote(repo.work)
            after = ga.rev_parse(repo.work, "origin/main")
            self.assertNotEqual(before, after)

    def test_fetch_failure_returns_false_not_exception(self):
        import shutil
        with FakeRepo() as repo:
            shutil.rmtree(repo.origin)  # origin now unreachable
            self.assertFalse(ga.fetch_remote(repo.work))

    def test_divergence_detected_after_fetch(self):
        with FakeRepo() as repo:
            repo.diverge_origin()
            repo.write("local-only.txt", "x\n")
            repo.commit("local-only commit", push=False)
            ga.fetch_remote(repo.work)
            ahead, behind = ga.ahead_behind(repo.work, "HEAD", "origin/main")
            self.assertGreater(ahead, 0)
            self.assertGreater(behind, 0)


class IsAncestorTests(SimpleTestCase):
    def test_ancestor_true(self):
        with FakeRepo() as repo:
            first = repo.rev_parse("HEAD")
            repo.write("second.txt", "x\n")
            second = repo.commit("second")
            self.assertTrue(ga.is_ancestor(repo.work, first, second))

    def test_ancestor_false(self):
        with FakeRepo() as repo:
            first = repo.rev_parse("HEAD")
            repo.write("second.txt", "x\n")
            second = repo.commit("second")
            self.assertFalse(ga.is_ancestor(repo.work, second, first))

    def test_ancestor_none_for_unknown_object(self):
        with FakeRepo() as repo:
            head = repo.rev_parse("HEAD")
            self.assertIsNone(ga.is_ancestor(repo.work, "a" * 40, head))


class PathAtCommitTests(SimpleTestCase):
    def test_path_exists_at_commit(self):
        with FakeRepo() as repo:
            repo.write("deploy/releases/r0002.json", "{}")
            sha = repo.commit("add release")
            self.assertTrue(ga.path_exists_at_commit(repo.work, sha, "deploy/releases/r0002.json"))

    def test_path_does_not_exist_at_earlier_commit(self):
        with FakeRepo() as repo:
            first = repo.rev_parse("HEAD")
            repo.write("deploy/releases/r0002.json", "{}")
            repo.commit("add release")
            self.assertFalse(ga.path_exists_at_commit(repo.work, first, "deploy/releases/r0002.json"))

    def test_read_bytes_at_commit(self):
        with FakeRepo() as repo:
            repo.write("thing.txt", "hello\n")
            sha = repo.commit("add thing")
            self.assertEqual(ga.read_bytes_at_commit(repo.work, sha, "thing.txt"), b"hello\n")

    def test_read_bytes_at_commit_missing_path(self):
        with FakeRepo() as repo:
            sha = repo.rev_parse("HEAD")
            self.assertIsNone(ga.read_bytes_at_commit(repo.work, sha, "does/not/exist.txt"))

    def test_read_bytes_retention_is_actually_bounded(self):
        with FakeRepo() as repo:
            repo.write("large.txt", "x" * (ga.MAX_OUTPUT_BYTES + 4096))
            sha = repo.commit("large git object")
            data = ga.read_bytes_at_commit(repo.work, sha, "large.txt")
            self.assertEqual(len(data), ga.MAX_OUTPUT_BYTES)

    def test_path_traversal_rejected(self):
        with FakeRepo() as repo:
            with self.assertRaises(ValueError):
                ga.path_exists_at_commit(repo.work, repo.rev_parse("HEAD"), "../../etc/passwd")

    def test_find_introducing_commit(self):
        with FakeRepo() as repo:
            repo.write("deploy/releases/r0002.json", "{}")
            sha = repo.commit("add release")
            self.assertEqual(ga.find_introducing_commit(repo.work, "deploy/releases/r0002.json"), sha)

    def test_find_introducing_commit_none_when_absent(self):
        with FakeRepo() as repo:
            self.assertIsNone(ga.find_introducing_commit(repo.work, "deploy/releases/never-added.json"))

    def test_find_introducing_commit_rejects_later_manifest_modification(self):
        with FakeRepo() as repo:
            path = "deploy/releases/r0002.json"
            repo.write(path, '{"version": 1}\n')
            repo.commit("add immutable release")
            repo.write(path, '{"version": 2}\n')
            repo.commit("illegally modify immutable release")
            self.assertIsNone(ga.find_introducing_commit(repo.work, path))
