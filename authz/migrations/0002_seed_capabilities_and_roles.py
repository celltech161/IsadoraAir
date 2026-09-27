"""Seed the roadmap 2.5A capability vocabulary, the three Roles that
preserve today's existing effective behavior, and the GroupRole bindings
for the existing `Contributor`/`remote_dj` groups.

Idempotent (get_or_create/update_or_create throughout, same style as
library/migrations/0054_seed_default_group_access.py) and safe on a
production database that has gone through the full historical migration
chain: it only ever reads/creates auth.Group rows by name, never assumes
a pristine auth_group table, and never deletes or edits GroupAccess.

Existing `Contributor`/`remote_dj` accounts get IDENTICAL effective
access on the day this lands -- GroupAccess is completely untouched, and
the two Roles seeded below grant only the capabilities those accounts
already had by other means (Contributor's own-upload-delete carve-out,
remote_dj's page/token/dashboard access) plus the NEWLY-enforced
capabilities they must NOT receive (see the security-fix capabilities
below, which are granted only to Station Administrator).

Capability vocabulary notes (slugs are a code-level contract -- see
authz/evaluator.py callers):

  monitoring.restart_service -- closes the 2.5A security defect in
      monitoring/views.py::api_restart_check (previously reachable by
      any remote_dj account via the legacy /monitoring/ GroupAccess
      prefix, with zero other check).

  library.manage_categories -- category CREATE/UPDATE/DELETE and the
      analysis-recompute actions (reset-analysis, repick-cue-points).
      Closes the 2.5A security defect in
      library/views.py::api_category_detail (previously reachable by
      any Contributor account via their /api/categories/ GroupAccess
      prefix, granted only for the upload page's read-only category
      dropdown). Deliberately does NOT gate category *reads*
      (api_category_list GET, api_category_detail GET) -- Contributor's
      upload workflow depends on those and must keep working unchanged.

  library.manage_tracks -- track-level mutation actions with no
      pre-existing check at all, discovered while closing the category
      defect above (same reachability-only exploit shape, same
      /api/tracks/ prefix already granted to both Contributor and
      remote_dj): bulk actions (including bulk delete), reanalyze,
      write-metadata, repick-cue-points, and the three blocked-slot
      toggle endpoints. Deliberately separate from the existing,
      already-correct library.middleware.user_is_library_read_only()
      gate on api_track_detail/api_track_bulk-adjacent single-track
      writes -- that gate (and its Contributor own-upload carve-out) is
      untouched by 2.5A.

  schedule.edit, rotations_playlists.edit -- established now per the
      audit's capability vocabulary and the roadmap's "acceptable to
      establish rows without making them authoritative everywhere yet"
      allowance. Not yet wired into any view in 2.5A: no existing
      GroupAccess grants any group access to these endpoints today, so
      there is no currently-exploitable gap to close, and wiring them
      up is left to a later pass to keep this migration's blast radius
      to the audited findings.

  remote_dj.connect, playout.control, playout.manual_mode,
  remote_dj.mic_gate, studio.mic_ptt, fx.fire (requires_schedule=True) --
      established as ROWS ONLY, per explicit roadmap instruction: 2.5A
      must not migrate any scheduled/live capability's enforcement off
      of today's GroupAccess-only gating, since no TalentAssignment/
      schedule-window mechanism exists yet to authoritatively re-grant
      them (that would leave these MORE open, not less, mid-migration).

  library.view, voicetrack.record, library.upload, reports.view,
  webrequests.administer, fx.manage_carts, system.administer -- rows
      established for vocabulary completeness / 2.5B-C forward
      reference; not wired into any view in 2.5A (their existing
      is_staff/is_superuser/user_is_* checks are untouched and remain
      authoritative).
"""
from django.db import migrations


# (slug, label, requires_schedule)
CAPABILITIES = [
    ("library.view", "View library", False),
    ("library.upload", "Upload library material", False),
    ("library.manage_tracks", "Manage track metadata/analysis/availability", False),
    ("library.manage_categories", "Create/edit/delete categories", False),
    ("schedule.edit", "Edit the recurring/one-off schedule grid", False),
    ("rotations_playlists.edit", "Edit rotations and playlists", False),
    ("voicetrack.record", "Record/manage voice tracks", False),
    ("remote_dj.connect", "Connect as a Remote DJ", True),
    ("playout.control", "Control playout (queue/seek/deck commands)", True),
    ("playout.manual_mode", "Switch automation/manual mode", True),
    ("remote_dj.mic_gate", "Gate the connected Remote DJ's mic", True),
    ("studio.mic_ptt", "Studio microphone push-to-talk", True),
    ("fx.fire", "Fire FX hot-key carts", True),
    ("fx.manage_carts", "Upload/manage FX cart audio files", False),
    ("reports.view", "View/generate royalty and listener reports", False),
    ("webrequests.administer", "Administer Web Requests configuration", False),
    ("monitoring.restart_service", "Restart a monitored systemd service", False),
    ("system.administer", "System/configuration administration", False),
]

# role_name -> [capability_slug, ...]
ROLE_CAPABILITIES = {
    "Contributor": [
        "library.view",
        "library.upload",
    ],
    "Remote Host": [
        "library.view",
        "voicetrack.record",
        "remote_dj.connect",
        "playout.control",
        "playout.manual_mode",
        "remote_dj.mic_gate",
        "fx.fire",
    ],
    "Station Administrator": [
        "library.view",
        "library.upload",
        "library.manage_tracks",
        "library.manage_categories",
        "schedule.edit",
        "rotations_playlists.edit",
        "voicetrack.record",
        "remote_dj.connect",
        "playout.control",
        "playout.manual_mode",
        "remote_dj.mic_gate",
        "studio.mic_ptt",
        "fx.fire",
        "fx.manage_carts",
        "reports.view",
        "webrequests.administer",
        "monitoring.restart_service",
        "system.administer",
    ],
}

# role_name -> auth.Group name, only for groups that already exist. This
# is a preservation seed, not a group-creation step -- if a fresh install
# hasn't run library's own remote_dj/Contributor group-seed migrations
# yet, there's nothing to bind here (that seed runs independently and
# earlier in the chain; a truly fresh install reaching THIS migration
# has already run it, since library's migrations are a hard dependency
# below, but the lookup is still defensive .filter().first() rather than
# an assumed .get() so a hand-edited/partial dev database can't crash a
# migrate run).
GROUP_ROLE_BINDINGS = {
    "Contributor": "Contributor",
    "remote_dj": "Remote Host",
}


def seed(apps, schema_editor):
    Capability = apps.get_model("authz", "Capability")
    Role = apps.get_model("authz", "Role")
    RoleCapability = apps.get_model("authz", "RoleCapability")
    GroupRole = apps.get_model("authz", "GroupRole")
    Group = apps.get_model("auth", "Group")

    capability_by_slug = {}
    for slug, label, requires_schedule in CAPABILITIES:
        cap, _ = Capability.objects.update_or_create(
            slug=slug,
            defaults={"label": label, "requires_schedule": requires_schedule},
        )
        capability_by_slug[slug] = cap

    role_by_name = {}
    for role_name, slugs in ROLE_CAPABILITIES.items():
        role, _ = Role.objects.get_or_create(name=role_name)
        role_by_name[role_name] = role
        for slug in slugs:
            RoleCapability.objects.get_or_create(role=role, capability=capability_by_slug[slug])

    for group_name, role_name in GROUP_ROLE_BINDINGS.items():
        group = Group.objects.filter(name=group_name).first()
        if group is None:
            continue
        GroupRole.objects.update_or_create(group=group, defaults={"role": role_by_name[role_name]})


def unseed(apps, schema_editor):
    Role = apps.get_model("authz", "Role")
    Capability = apps.get_model("authz", "Capability")
    GroupRole = apps.get_model("authz", "GroupRole")

    GroupRole.objects.filter(role__name__in=ROLE_CAPABILITIES.keys()).delete()
    Role.objects.filter(name__in=ROLE_CAPABILITIES.keys()).delete()
    Capability.objects.filter(slug__in=[slug for slug, _, _ in CAPABILITIES]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("authz", "0001_initial"),
        # Hard dependency on library's own remote_dj/Contributor group
        # seed migrations, so GROUP_ROLE_BINDINGS' lookup runs after
        # those groups are guaranteed to exist on a fresh install.
        ("library", "0069_voicetracks_page_access_and_nav"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
