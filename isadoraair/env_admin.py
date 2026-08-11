"""Shared Django-admin plumbing for editing Phase 1/2 managed .env
settings from an existing ModelAdmin's own change-form page, via a
separate sub-page/sub-form (see isadoraair/env_config.py's own module
docstring for why: Django admin's change_form.html wraps everything in
one <form>, so a model's own save can't safely also mutate .env in the
same POST without either nesting forms invalidly or pretending a DB
save and a filesystem write are one atomic operation).

Introduced in Phase 2 (2026-08-11) when a second and third admin
section (library.admin's CDRipConfig/StationInfo pages, weather.admin's
WeatherConfig page) needed the exact same read/compare/write/audit
shape Phase 1 built bespoke for monitoring/admin.py's SMTP sub-page --
factored out here so those three don't each reinvent it, and so a
future Phase 3 section can reuse it too.

Deliberately NOT used to retrofit monitoring/admin.py's own SMTP
sub-page -- that code already shipped and is already tested; leaving
it as its own bespoke (and slightly more involved, since it also
handles a secret/password field and a checkbox) implementation avoids
any regression risk on Phase 1. A future cleanup could fold it onto
this helper too, but that's optional, not required by Phase 2.

Every Phase 2 field so far is a plain text input (no secret, no
checkbox) -- this module doesn't attempt to generalize SMTP's
password/TLS-checkbox handling; a future secret-bearing Phase 3 section
would need its own extension here, not a forced reuse."""
from django.contrib import messages
from django.template.response import TemplateResponse

from monitoring.models import emit_event

from . import env_config


def env_subform_context(request, keys, *, title, change_url, admin_site, model, extra=None):
    """Read side: Saved-vs-Running per field, plus a non-blocking
    filesystem preflight status for any is_path setting in `keys`
    (skipped for a blank value -- nothing to inspect). `extra` is
    merged into the returned context verbatim, for page-specific
    additions the shared template doesn't know about (e.g.
    LIBRARY_ROOT's strand warning, weather's optional-file status).

    `admin_site`/`model` supply the standard admin-chrome context
    (site header/nav sidebar via each_context, and `opts` for the
    breadcrumbs block's app_label/verbose_name) -- admin/base_site.html
    and the shared template both expect these, same as any other admin
    page; easy to forget since a sub-page view builds its own context
    by hand instead of getting it for free from ModelAdmin machinery."""
    env_error = None
    saved = {}
    comparisons = {}
    try:
        saved = env_config.read_managed_values(keys)
        comparisons = env_config.compare_to_running(keys)
    except env_config.EnvConfigError as exc:
        env_error = str(exc)

    fields = []
    for key in keys:
        setting = env_config.MANAGED_SETTINGS[key]
        mv = saved.get(key)
        cmp = comparisons.get(key)
        value = mv.display_value if mv is not None else setting.default
        entry = {
            "key": key,
            "name": key.lower(),
            "label": setting.label,
            "value": value,
            "matches": cmp.matches if cmp is not None else True,
            "services_note": setting.services_note,
        }
        if setting.is_path and value:
            entry["preflight"] = env_config.path_preflight(value)
        fields.append(entry)

    context = {
        **admin_site.each_context(request),
        "title": title,
        "opts": model._meta,
        "change_url": change_url,
        "env_error": env_error,
        "fields": fields,
        "restart_required": env_error is None and any(not c.matches for c in comparisons.values()),
    }
    if extra:
        context.update(extra)
    return context


def render_env_subform(request, template_name, keys, *, title, change_url, admin_site, model, extra=None):
    context = env_subform_context(
        request, keys, title=title, change_url=change_url, admin_site=admin_site, model=model, extra=extra,
    )
    return TemplateResponse(request, template_name, context)


def handle_env_subform_post(request, message_user, values, *, audit_title, audit_category, dedupe_key,
                             restart_check_keys=None):
    """Write side: validate+write via env_config.update_managed_values,
    message the operator, emit an audit SystemEvent. `values` is a
    dict[env_key] -> raw string the caller has already pulled out of
    request.POST (Phase 2 fields are all plain text, no secret/
    checkbox handling to do here). `restart_check_keys` scopes the
    post-save restart-required computation and message -- defaults to
    just the keys being written if not given; pass the full set of keys
    this admin page displays if it shows more fields than were changed
    in this particular submission, so an unrelated already-mismatched
    field on the same page isn't silently left out of the banner."""
    try:
        result = env_config.update_managed_values(values)
    except env_config.EnvConfigError as exc:
        message_user(request, f"Could not save: {exc}", level=messages.ERROR)
        return

    if not result.changed_keys:
        message_user(
            request, "No changes to save -- submitted values already match what's saved.",
            level=messages.INFO,
        )
        return

    scope_keys = restart_check_keys if restart_check_keys is not None else list(values)
    try:
        comparisons = env_config.compare_to_running(scope_keys)
        restart_required = any(not c.matches for c in comparisons.values())
    except env_config.EnvConfigError:
        restart_required = True  # unknown -- err toward the more visible warning

    changed = sorted(result.changed_keys)
    message_user(
        request,
        f"Saved ({', '.join(changed)}). "
        + ("Restart required for these changes to take effect."
           if restart_required else "Running configuration matches saved configuration."),
        level=messages.SUCCESS,
    )
    # Audit event: key NAMES only, never a value. None of the Phase 2
    # settings are secret, but path CONTENTS still aren't dumped beyond
    # the key names -- matches Phase 1's own convention.
    emit_event(
        category=audit_category, level="info",
        title=audit_title,
        detail={
            "changed_keys": changed,
            "changed_by": request.user.get_username(),
            "restart_required": restart_required,
        },
        dedupe_key=dedupe_key,
    )
