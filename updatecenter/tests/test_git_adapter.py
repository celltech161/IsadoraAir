"""Read-only git adapter tests -- real throwaway git repos, no mocking
of subprocess itself (proves actual git behavior, not an assumption
about it). [P0] 1.1 Phase A."""
import subprocess
from unittest import mock

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
            self.assertEqual(
                ga.find_introducing_commit(
                    repo.work, "deploy/releases/r0002.json", repo.rev_parse("origin/main"),
                ),
                sha,
            )

    def test_find_introducing_commit_none_when_absent(self):
        with FakeRepo() as repo:
            self.assertIsNone(ga.find_introducing_commit(
                repo.work, "deploy/releases/never-added.json", repo.rev_parse("origin/main"),
            ))

    def test_find_introducing_commit_rejects_later_manifest_modification(self):
        with FakeRepo() as repo:
            path = "deploy/releases/r0002.json"
            repo.write(path, '{"version": 1}\n')
            repo.commit("add immutable release")
            repo.write(path, '{"version": 2}\n')
            repo.commit("illegally modify immutable release")
            self.assertIsNone(ga.find_introducing_commit(
                repo.work, path, repo.rev_parse("origin/main"),
            ))

    def test_stale_remote_tracking_ref_does_not_change_canonical_identity(self):
        """A deleted feature's stale origin/* ref is not release authority."""
        with FakeRepo() as repo:
            path = "deploy/releases/r0002.json"
            branch_point = repo.rev_parse("HEAD")

            repo.write(path, '{"canonical": true}\n')
            canonical = repo.commit("canonical release", push=True)

            subprocess.run(
                ["git", "checkout", "-q", "-b", "deleted-feature", branch_point],
                cwd=repo.work, check=True, capture_output=True,
            )
            repo.write(path, '{"canonical": false}\n')
            stale = repo.commit("independent noncanonical release", push=False)
            subprocess.run(
                ["git", "update-ref", "refs/remotes/origin/deleted-feature", stale],
                cwd=repo.work, check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "checkout", "-q", "main"],
                cwd=repo.work, check=True, capture_output=True,
            )

            self.assertEqual(ga.find_introducing_commit(
                repo.work, path, repo.rev_parse("origin/main"),
            ), canonical)

    def test_unrelated_local_branch_does_not_change_canonical_identity(self):
        with FakeRepo() as repo:
            path = "deploy/releases/r0002.json"
            branch_point = repo.rev_parse("HEAD")
            repo.write(path, '{"canonical": true}\n')
            canonical = repo.commit("canonical release", push=True)

            subprocess.run(
                ["git", "checkout", "-q", "-b", "local-review", branch_point],
                cwd=repo.work, check=True, capture_output=True,
            )
            repo.write(path, '{"canonical": false}\n')
            repo.commit("independent local release", push=False)
            subprocess.run(
                ["git", "checkout", "-q", "main"],
                cwd=repo.work, check=True, capture_output=True,
            )

            self.assertEqual(ga.find_introducing_commit(
                repo.work, path, repo.rev_parse("origin/main"),
            ), canonical)

    def test_canonical_delete_and_readd_remains_unresolvable(self):
        with FakeRepo() as repo:
            path = "deploy/releases/r0002.json"
            repo.write(path, '{"version": 1}\n')
            repo.commit("add canonical release", push=True)
            (repo.work / path).unlink()
            repo.commit("delete canonical release", push=True)
            repo.write(path, '{"version": 1}\n')
            repo.commit("re-add canonical release", push=True)

            self.assertIsNone(ga.find_introducing_commit(
                repo.work, path, repo.rev_parse("origin/main"),
            ))

    def test_unreachable_canonical_tip_fails_closed(self):
        with FakeRepo() as repo:
            path = "deploy/releases/r0002.json"
            repo.write(path, "{}")
            repo.commit("add release", push=True)
            self.assertIsNone(ga.find_introducing_commit(repo.work, path, "f" * 40))


class BatchedIntroducingCommitTests(SimpleTestCase):
    def assert_batch_matches_scalar(self, repo, paths, tip=None):
        tip = tip or repo.rev_parse("origin/main")
        scalar = {
            path: ga.find_introducing_commit(repo.work, path, tip)
            for path in paths
        }
        self.assertEqual(ga.find_introducing_commits(repo.work, paths, tip), scalar)
        return scalar

    def test_ordinary_absent_and_two_paths_added_together_match_scalar(self):
        with FakeRepo() as repo:
            ordinary = "deploy/releases/r0002.json"
            together_a = "deploy/releases/r0003.json"
            together_b = "deploy/releases/r0004.json"
            absent = "deploy/releases/absent.json"
            repo.write(ordinary, "{}\n")
            ordinary_sha = repo.commit("ordinary release")
            repo.write(together_a, "{}\n")
            repo.write(together_b, "{}\n")
            together_sha = repo.commit("two release manifests")

            result = self.assert_batch_matches_scalar(
                repo, [ordinary, absent, together_a, together_b],
            )
            self.assertEqual(result[ordinary], ordinary_sha)
            self.assertIsNone(result[absent])
            self.assertEqual(result[together_a], together_sha)
            self.assertEqual(result[together_b], together_sha)

    def test_modified_deleted_and_readded_paths_match_scalar(self):
        with FakeRepo() as repo:
            modified = "deploy/releases/r0002.json"
            deleted = "deploy/releases/r0003.json"
            readded = "deploy/releases/r0004.json"
            for path in (modified, deleted, readded):
                repo.write(path, '{"version": 1}\n')
                repo.commit(f"add {path}")
            repo.write(modified, '{"version": 2}\n')
            repo.commit("modify immutable manifest")
            (repo.work / deleted).unlink()
            repo.commit("delete immutable manifest")
            (repo.work / readded).unlink()
            repo.commit("delete before re-add")
            repo.write(readded, '{"version": 1}\n')
            repo.commit("re-add immutable manifest")

            result = self.assert_batch_matches_scalar(repo, [modified, deleted, readded])
            self.assertEqual(result, {modified: None, deleted: None, readded: None})

    def test_unrelated_commits_and_noncanonical_refs_do_not_influence_batch(self):
        with FakeRepo() as repo:
            path = "deploy/releases/r0002.json"
            branch_point = repo.rev_parse("HEAD")
            repo.write(path, '{"canonical": true}\n')
            canonical = repo.commit("canonical release", push=True)
            repo.write("unrelated.txt", "later\n")
            repo.commit("unrelated canonical commit", push=True)
            canonical_tip = repo.rev_parse("origin/main")

            subprocess.run(
                ["git", "checkout", "-q", "-b", "local-review", branch_point],
                cwd=repo.work, check=True, capture_output=True,
            )
            repo.write(path, '{"canonical": false}\n')
            alternate = repo.commit("noncanonical alternate", push=False)
            subprocess.run(
                ["git", "update-ref", "refs/remotes/origin/stale-review", alternate],
                cwd=repo.work, check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "checkout", "-q", "main"],
                cwd=repo.work, check=True, capture_output=True,
            )

            result = self.assert_batch_matches_scalar(repo, [path], canonical_tip)
            self.assertEqual(result[path], canonical)

    def test_station_behind_uses_supplied_canonical_tip_not_head(self):
        with FakeRepo() as repo:
            behind_head = repo.rev_parse("HEAD")
            path = "deploy/releases/r0002.json"
            repo.write(path, "{}\n")
            introduced = repo.commit("release ahead of station", push=True)
            canonical_tip = repo.rev_parse("origin/main")
            repo.reset_local_to(behind_head)

            result = self.assert_batch_matches_scalar(repo, [path], canonical_tip)
            self.assertEqual(result[path], introduced)

    def test_rename_into_and_out_of_release_directory_match_scalar(self):
        with FakeRepo() as repo:
            renamed_in = "deploy/releases/r0002.json"
            repo.write("staging/r0002.json", "{}\n")
            repo.commit("stage manifest")
            (repo.work / "deploy/releases").mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "mv", "staging/r0002.json", renamed_in],
                cwd=repo.work, check=True, capture_output=True,
            )
            repo.commit("rename manifest into release directory")

            renamed_out = "deploy/releases/r0003.json"
            repo.write(renamed_out, "{}\n")
            repo.commit("add manifest that will move")
            (repo.work / "archive").mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "mv", renamed_out, "archive/r0003.json"],
                cwd=repo.work, check=True, capture_output=True,
            )
            repo.commit("rename manifest out of release directory")

            self.assert_batch_matches_scalar(repo, [renamed_in, renamed_out])

    def test_merge_ancestry_matches_scalar(self):
        with FakeRepo() as repo:
            path = "deploy/releases/r0002.json"
            subprocess.run(
                ["git", "checkout", "-q", "-b", "side"],
                cwd=repo.work, check=True, capture_output=True,
            )
            repo.write(path, "{}\n")
            introduced = repo.commit("release introduced on side branch", push=False)
            repo.write("side.txt", "side\n")
            repo.commit("side branch", push=False)
            subprocess.run(
                ["git", "checkout", "-q", "main"],
                cwd=repo.work, check=True, capture_output=True,
            )
            repo.write("main.txt", "main\n")
            repo.commit("main branch", push=False)
            subprocess.run(
                ["git", "merge", "--no-ff", "-q", "side", "-m", "merge side"],
                cwd=repo.work, check=True, capture_output=True,
            )
            tip = repo.rev_parse("HEAD")

            result = self.assert_batch_matches_scalar(repo, [path], tip)
            self.assertEqual(result[path], introduced)

    def test_invalid_tip_and_paths_fail_closed_like_scalar_contract(self):
        with FakeRepo() as repo:
            path = "deploy/releases/r0002.json"
            repo.write(path, "{}\n")
            repo.commit("release")
            self.assertEqual(
                ga.find_introducing_commits(repo.work, [path], "f" * 40),
                {path: None},
            )
            with self.assertRaises(ValueError):
                ga.find_introducing_commits(
                    repo.work, ["../../etc/passwd"], repo.rev_parse("origin/main"),
                )

    def test_malformed_or_truncated_git_output_fails_entire_batch_closed(self):
        with FakeRepo() as repo:
            paths = ["deploy/releases/r0002.json", "deploy/releases/r0003.json"]
            tip = repo.rev_parse("origin/main")
            with mock.patch.object(
                ga, "_run_git_argv", return_value=(0, b"malformed history output"),
            ):
                self.assertIsNone(
                    ga._batched_path_history(
                        repo.work, tuple(paths), tip, additions_only=True,
                    )
                )
            with mock.patch.object(
                ga, "_run_git_argv", return_value=(0, b"x" * ga.MAX_OUTPUT_BYTES),
            ):
                self.assertIsNone(
                    ga._batched_path_history(
                        repo.work, tuple(paths), tip, additions_only=False,
                    )
                )
