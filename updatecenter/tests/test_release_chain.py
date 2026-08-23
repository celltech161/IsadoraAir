"""Release-chain structural + git-resolution tests -- [P0] 1.1 Phase A."""
import json
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from updatecenter import manifest as m, release_chain as rc
from .gitfixtures import FakeRepo
from .test_manifest import _valid_bootstrap, _valid_followup


def _write_releases(releases_dir: Path, *manifests: dict):
    releases_dir.mkdir(parents=True, exist_ok=True)
    for data in manifests:
        (releases_dir / f"{data['release_id']}.json").write_text(json.dumps(data), encoding="utf-8")


class LoadManifestFilesTests(SimpleTestCase):
    def test_loads_valid_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            releases_dir = Path(tmp)
            _write_releases(releases_dir, _valid_bootstrap())
            loaded = rc.load_manifest_files(releases_dir)
            self.assertEqual(set(loaded), {"r0001"})

    def test_missing_directory_raises(self):
        with self.assertRaises(rc.ChainError):
            rc.load_manifest_files(Path("/nonexistent/path/for/sure"))

    def test_filename_release_id_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            releases_dir = Path(tmp)
            releases_dir.mkdir(exist_ok=True)
            (releases_dir / "r0002.json").write_text(json.dumps(_valid_bootstrap()), encoding="utf-8")  # content says r0001
            with self.assertRaisesMessage(rc.ChainError, "does not match"):
                rc.load_manifest_files(releases_dir)

    def test_malformed_json_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            releases_dir = Path(tmp)
            releases_dir.mkdir(exist_ok=True)
            (releases_dir / "r0001.json").write_text("{not valid json", encoding="utf-8")
            with self.assertRaises(rc.ChainError):
                rc.load_manifest_files(releases_dir)

    def test_individually_invalid_manifest_raises_manifest_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            releases_dir = Path(tmp)
            releases_dir.mkdir(exist_ok=True)
            bad = _valid_bootstrap()
            bad["schema_version"] = 999
            (releases_dir / "r0001.json").write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(m.ManifestError):
                rc.load_manifest_files(releases_dir)


class BuildChainTests(SimpleTestCase):
    def test_single_bootstrap_chain(self):
        chain = rc.build_chain({"r0001": m.validate_manifest_dict(_valid_bootstrap())})
        self.assertEqual([c.manifest.release_id for c in chain], ["r0001"])
        self.assertEqual(chain[0].index, 0)

    def test_two_release_chain_orders_correctly(self):
        manifests = {
            "r0001": m.validate_manifest_dict(_valid_bootstrap()),
            "r0002": m.validate_manifest_dict(_valid_followup()),
        }
        chain = rc.build_chain(manifests)
        self.assertEqual([c.manifest.release_id for c in chain], ["r0001", "r0002"])

    def test_skip_several_releases_chain_orders_correctly(self):
        manifests = {"r0001": m.validate_manifest_dict(_valid_bootstrap())}
        for i in range(2, 6):
            manifests[f"r{i:04d}"] = m.validate_manifest_dict(
                _valid_followup(release_id=f"r{i:04d}", previous_release_id=f"r{i-1:04d}")
            )
        chain = rc.build_chain(manifests)
        self.assertEqual([c.manifest.release_id for c in chain], ["r0001", "r0002", "r0003", "r0004", "r0005"])

    def test_empty_set_rejected(self):
        with self.assertRaisesMessage(rc.ChainError, "no release manifests"):
            rc.build_chain({})

    def test_no_bootstrap_rejected(self):
        # r0002 alone, pointing at a nonexistent r0001 -- the ZERO-
        # bootstrap check fires first (no release has
        # previous_release_id=None at all), before the chain logic
        # would otherwise separately notice the missing predecessor.
        manifests = {"r0002": m.validate_manifest_dict(_valid_followup())}
        with self.assertRaisesMessage(rc.ChainError, "no bootstrap release found"):
            rc.build_chain(manifests)

    def test_two_bootstraps_rejected(self):
        b1 = m.validate_manifest_dict(_valid_bootstrap())
        b2_data = _valid_bootstrap(release_id="r0009")
        b2 = m.validate_manifest_dict(b2_data)
        with self.assertRaisesMessage(rc.ChainError, "more than one bootstrap"):
            rc.build_chain({"r0001": b1, "r0009": b2})

    def test_missing_predecessor_rejected(self):
        manifests = {
            "r0001": m.validate_manifest_dict(_valid_bootstrap()),
            "r0003": m.validate_manifest_dict(_valid_followup(release_id="r0003", previous_release_id="r0002")),
        }
        with self.assertRaisesMessage(rc.ChainError, "does not exist"):
            rc.build_chain(manifests)

    def test_forked_chain_rejected(self):
        manifests = {
            "r0001": m.validate_manifest_dict(_valid_bootstrap()),
            "r0002": m.validate_manifest_dict(_valid_followup(release_id="r0002", previous_release_id="r0001")),
            "r0003": m.validate_manifest_dict(_valid_followup(release_id="r0003", previous_release_id="r0001")),
        }
        with self.assertRaisesMessage(rc.ChainError, "two successors"):
            rc.build_chain(manifests)

    def test_disconnected_island_rejected(self):
        """r0001 (bootstrap) -> r0002 is a normal, valid two-release
        chain. r0008 and r0009 form their OWN two-node island, each
        naming the other as predecessor -- both "exist in the loaded
        set" (so the missing-predecessor check doesn't fire) and
        neither is a second bootstrap (so the two-bootstraps check
        doesn't fire either), but neither is reachable by forward-
        walking from the true bootstrap. In a singly-linked-predecessor
        chain this is the only shape a disconnected component can take
        (anything not eventually rooted at the null-predecessor
        bootstrap must cycle among itself) -- build_chain must still
        reject it, whether the specific message says "unreachable" or
        "cycle"."""
        import dataclasses
        b = m.validate_manifest_dict(_valid_bootstrap())
        r2 = m.validate_manifest_dict(_valid_followup(release_id="r0002", previous_release_id="r0001"))
        island_a = m.validate_manifest_dict(_valid_followup(release_id="r0008", previous_release_id="r0009"))
        island_b_data = _valid_followup(release_id="r0009", previous_release_id="r0008")
        island_b = m.validate_manifest_dict(island_b_data)
        manifests = {"r0001": b, "r0002": r2, "r0008": island_a, "r0009": island_b}
        with self.assertRaises(rc.ChainError):
            rc.build_chain(manifests)

    def test_cycle_rejected(self):
        # r0001 (bootstrap) -> r0002 -> r0003 -> back to r0002 (cycle,
        # built by hand since validate_manifest_dict itself forbids a
        # release naming itself as predecessor -- a cycle needs at
        # least 2 non-bootstrap releases pointing at each other).
        b = m.validate_manifest_dict(_valid_bootstrap())
        r2_data = _valid_followup(release_id="r0002", previous_release_id="r0001")
        r3_data = _valid_followup(release_id="r0003", previous_release_id="r0002")
        r2 = m.validate_manifest_dict(r2_data)
        r3 = m.validate_manifest_dict(r3_data)
        # Manually rewrite r2's previous_release_id to point at r0003,
        # forming a cycle r0002 <-> r0003 that never reaches back to
        # the bootstrap's successor chain cleanly.
        import dataclasses
        r2_cyclic = dataclasses.replace(r2, previous_release_id="r0003")
        manifests = {"r0001": b, "r0002": r2_cyclic, "r0003": r3}
        with self.assertRaises(rc.ChainError):
            rc.build_chain(manifests)

    def test_minimum_supported_release_must_be_an_earlier_chain_member(self):
        manifests = {
            "r0001": m.validate_manifest_dict(_valid_bootstrap()),
            "r0002": m.validate_manifest_dict(
                _valid_followup(minimum_supported_release_id="r0002")
            ),
        }
        with self.assertRaisesMessage(rc.ChainError, "must be an earlier release"):
            rc.build_chain(manifests)


class ResolveReleaseCommitTests(SimpleTestCase):
    def test_bootstrap_commit_is_the_declared_field(self):
        with FakeRepo() as repo:
            data = _valid_bootstrap(bootstrap_commit=repo.rev_parse("HEAD"))
            chained = rc.ChainedRelease(manifest=m.validate_manifest_dict(data), index=0)
            self.assertEqual(rc.resolve_release_commit(chained, repo.work), repo.rev_parse("HEAD"))

    def test_followup_commit_resolved_from_git_history(self):
        with FakeRepo() as repo:
            bootstrap_sha = repo.rev_parse("HEAD")
            repo.write("deploy/releases/r0002.json", "{}")
            followup_sha = repo.commit("add r0002 manifest")
            data = _valid_followup()
            chained = rc.ChainedRelease(manifest=m.validate_manifest_dict(data), index=1)
            self.assertEqual(rc.resolve_release_commit(chained, repo.work), followup_sha)

    def test_followup_commit_unresolvable_when_file_never_added(self):
        with FakeRepo() as repo:
            data = _valid_followup()
            chained = rc.ChainedRelease(manifest=m.validate_manifest_dict(data), index=1)
            self.assertIsNone(rc.resolve_release_commit(chained, repo.work))

    def test_followup_commit_unresolvable_after_manifest_modification(self):
        with FakeRepo() as repo:
            repo.write("deploy/releases/r0002.json", "{}")
            repo.commit("add r0002 manifest")
            repo.write("deploy/releases/r0002.json", '{"changed": true}')
            repo.commit("modify immutable manifest")
            chained = rc.ChainedRelease(
                manifest=m.validate_manifest_dict(_valid_followup()), index=1,
            )
            self.assertIsNone(rc.resolve_release_commit(chained, repo.work))

    def test_two_release_manifests_in_one_commit_are_ambiguous(self):
        with FakeRepo() as repo:
            bootstrap_sha = repo.rev_parse("HEAD")
            repo.write("deploy/releases/r0002.json", "{}")
            repo.write("deploy/releases/r0003.json", "{}")
            repo.commit("incorrectly add two releases together")
            chain = rc.build_chain({
                "r0001": m.validate_manifest_dict(_valid_bootstrap(bootstrap_commit=bootstrap_sha)),
                "r0002": m.validate_manifest_dict(_valid_followup()),
                "r0003": m.validate_manifest_dict(
                    _valid_followup(release_id="r0003", previous_release_id="r0002")
                ),
            })
            with self.assertRaisesMessage(rc.ChainError, "same commit"):
                rc.resolve_unique_release_commits(chain, repo.work)


class ResolveInstalledReleaseTests(SimpleTestCase):
    def test_installed_is_bootstrap_when_head_is_bootstrap_commit(self):
        with FakeRepo() as repo:
            head = repo.rev_parse("HEAD")
            data = _valid_bootstrap(bootstrap_commit=head)
            chain = rc.build_chain({"r0001": m.validate_manifest_dict(data)})
            result = rc.resolve_installed_release(chain, repo.work, head)
            self.assertEqual(result.manifest.release_id, "r0001")

    def test_installed_is_latest_ancestor_when_ahead_of_all_releases(self):
        with FakeRepo() as repo:
            bootstrap_sha = repo.rev_parse("HEAD")
            repo.write("deploy/releases/r0002.json", "{}")
            r0002_sha = repo.commit("add r0002 manifest")
            repo.write("unrelated.txt", "x\n")
            head = repo.commit("later, unrelated commit")

            manifests = {
                "r0001": m.validate_manifest_dict(_valid_bootstrap(bootstrap_commit=bootstrap_sha)),
                "r0002": m.validate_manifest_dict(_valid_followup()),
            }
            chain = rc.build_chain(manifests)
            result = rc.resolve_installed_release(chain, repo.work, head)
            self.assertEqual(result.manifest.release_id, "r0002")

    def test_none_when_head_predates_bootstrap(self):
        with FakeRepo() as repo:
            early_sha = repo.rev_parse("HEAD")
            repo.write("later.txt", "x\n")
            later_sha = repo.commit("later commit, this is the 'bootstrap'")

            data = _valid_bootstrap(bootstrap_commit=later_sha)
            chain = rc.build_chain({"r0001": m.validate_manifest_dict(data)})
            # HEAD (early_sha) predates the declared bootstrap commit --
            # bootstrap is NOT an ancestor of early_sha, so nothing resolves.
            result = rc.resolve_installed_release(chain, repo.work, early_sha)
            self.assertIsNone(result)
