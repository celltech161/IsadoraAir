from django.contrib.auth import get_user_model
from django.contrib.auth.forms import PasswordResetForm, _unicode_ci_compare

UserModel = get_user_model()


class InviteCapablePasswordResetForm(PasswordResetForm):
    """PasswordResetForm's own get_users() silently excludes any account
    with has_usable_password() == False -- exactly the state every
    admin-created account is in here (see library.admin's no-password
    add-user form and the Django ticket this behavior comes from:
    protecting SSO-only accounts from getting a password-reset link
    they can't use). That's the wrong default for us: our accounts are
    ALWAYS created without a usable password specifically so the invite
    link is the intended, only way in.

    Used for both the public /password-reset/ flow (someone forgot
    their password, or -- just as likely here -- hasn't set one yet)
    and the admin's "Send password setup email" button, so the two
    stay behaviorally identical as the button's own label promises
    ("Sends the same link as 'Forgot password?' on the login page").

    Copied from PasswordResetForm.get_users(), minus the
    has_usable_password() filter -- that's the only change."""
    def get_users(self, email):
        email_field_name = UserModel.get_email_field_name()
        active_users = UserModel._default_manager.filter(**{
            f"{email_field_name}__iexact": email,
            "is_active": True,
        })
        return (
            u for u in active_users
            if _unicode_ci_compare(email, getattr(u, email_field_name))
        )
