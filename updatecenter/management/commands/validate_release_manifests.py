"""Read-only release-manifest-set validator -- [P0] 1.1 Phase A.

Validates the WHOLE `deploy/releases/` set: every individual manifest
(manifest.validate_manifest_dict), the chain they form
(release_chain.build_chain), and -- for every release whose commit can
be resolved in THIS checkout's git history -- cross-checks its claims
and predecessor-diff intent against actual Git content
(cross_check.cross_check_release).

Never modifies anything. Exit code 0 iff everything validates cleanly;
nonzero otherwise -- same convention as
monitoring/management/commands/check_deploy_baseline.py, deliberately,
so this can be run the same way in CI/at release-review time."""
from pathlib import Path

from django.core.management.base import BaseCommand

from updatecenter import (
    cross_check, git_adapter, manifest as manifest_mod, migration_authorization, planner, release_chain,
)


class Command(BaseCommand):
    help = (
        "Read-only: validates every deploy/releases/*.json manifest, the chain "
        "they form, and (where resolvable) their claims against actual git "
        "content. Never writes anything. Exit code 0 iff everything is valid."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--releases-dir", default=None,
            help="Override the releases directory (default: <checkout>/deploy/releases)",
        )

    def handle(self, *args, **options):
        checkout_root = Path(__file__).resolve().parents[3]
        releases_dir = Path(options["releases_dir"]) if options["releases_dir"] else checkout_root / "deploy" / "releases"

        self.stdout.write(f"Validating release manifests in {releases_dir} ...")

        try:
            manifests = release_chain.load_manifest_files(releases_dir)
        except (manifest_mod.ManifestError, release_chain.ChainError) as exc:
            self.stderr.write(self.style.ERROR(f"FAIL: {exc}"))
            raise SystemExit(1)

        self.stdout.write(self.style.SUCCESS(f"PASS: {len(manifests)} manifest(s) individually well-formed."))

        try:
            chain = release_chain.build_chain(manifests)
        except release_chain.ChainError as exc:
            self.stderr.write(self.style.ERROR(f"FAIL: chain structure invalid -- {exc}"))
            raise SystemExit(1)

        self.stdout.write(self.style.SUCCESS(
            f"PASS: {len(chain)} release(s) form one unambiguous chain: "
            f"{' -> '.join(c.manifest.release_id for c in chain)}"
        ))

        app_label_paths = planner.app_label_paths_for_checkout(checkout_root)
        any_cross_check_failure = False
        resolved_release_by_commit = {}
        head_sha = git_adapter.rev_parse(checkout_root, "HEAD")
        canonical_tip = head_sha or "HEAD"
        for position, chained in enumerate(chain):
            commit = release_chain.resolve_release_commit(chained, checkout_root, canonical_tip)
            if commit is None:
                relative_path = f"{release_chain.RELEASES_DIRNAME_DEFAULT}/{chained.manifest.release_id}.json"
                committed_at_head = bool(
                    head_sha and git_adapter.path_exists_at_commit(checkout_root, head_sha, relative_path)
                )
                if committed_at_head:
                    any_cross_check_failure = True
                    self.stderr.write(self.style.ERROR(
                        f"  {chained.manifest.release_id}: committed manifest has no unique immutable "
                        "introducing commit (it may have been modified/deleted/re-added)"
                    ))
                else:
                    self.stdout.write(
                        f"  {chained.manifest.release_id}: not committed/fetched yet -- skipping commit cross-check"
                    )
                continue
            if not git_adapter.commit_exists(checkout_root, commit):
                self.stdout.write(f"  {chained.manifest.release_id}: commit {commit[:12]} not present locally -- skipping cross-check")
                continue
            other = resolved_release_by_commit.get(commit)
            if other is not None:
                any_cross_check_failure = True
                self.stderr.write(self.style.ERROR(
                    f"  {chained.manifest.release_id}: shares introducing commit {commit[:12]} with "
                    f"{other}; release identity is ambiguous"
                ))
                continue
            resolved_release_by_commit[commit] = chained.manifest.release_id
            previous_commit = None
            if position:
                previous_commit = release_chain.resolve_release_commit(
                    chain[position - 1], checkout_root, canonical_tip,
                )
                if previous_commit is None:
                    any_cross_check_failure = True
                    self.stderr.write(self.style.ERROR(
                        f"  {chained.manifest.release_id}: predecessor commit could not be "
                        "resolved for protected-runtime diff validation"
                    ))
                    continue
            findings = cross_check.cross_check_release(
                chained.manifest, commit, checkout_root, app_label_paths,
                previous_commit,
            )
            if findings:
                any_cross_check_failure = True
                self.stderr.write(self.style.ERROR(f"  {chained.manifest.release_id} @ {commit[:12]}: {len(findings)} finding(s):"))
                for f in findings:
                    self.stderr.write(self.style.ERROR(f"    - [{f.field}] {f.detail}"))
            else:
                self.stdout.write(self.style.SUCCESS(f"  {chained.manifest.release_id} @ {commit[:12]}: PASS"))
            # Trusted companion migration authorization (runtime 11): the
            # same rules protected runtime 11 applies when it discovers it.
            if head_sha:
                try:
                    authorized = migration_authorization.validate_release_companion(
                        checkout_root, head_sha, release_id=chained.manifest.release_id,
                        release_commit=commit, migrations_required=chained.manifest.migrations_required,
                        app_label_paths=app_label_paths,
                    )
                except migration_authorization.CompanionError as exc:
                    any_cross_check_failure = True
                    self.stderr.write(self.style.ERROR(f"  {chained.manifest.release_id}: companion: {exc}"))
                else:
                    if authorized is not None:
                        self.stdout.write(self.style.SUCCESS(
                            f"  {chained.manifest.release_id}: companion authorization PASS "
                            f"({len(authorized)} reviewed manual operation(s))"
                        ))

        if head_sha:
            orphans = migration_authorization.orphan_companions(
                checkout_root, head_sha, [c.manifest.release_id for c in chain],
            )
            if orphans:
                any_cross_check_failure = True
                self.stderr.write(self.style.ERROR(
                    f"FAIL: {migration_authorization.AUTHORIZATION_DIR}/ holds files that are not a "
                    f"<release_id>.json companion of a release in the chain: {orphans!r}"
                ))

        if any_cross_check_failure:
            self.stderr.write(self.style.ERROR("FAIL: one or more releases' manifest claims disagree with actual repository content."))
            raise SystemExit(1)

        self.stdout.write(self.style.SUCCESS("PASS: release manifest set is fully valid."))
