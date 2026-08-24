from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_not_required
from django.urls import include, path

from library.auth_forms import InviteCapablePasswordResetForm
from isadoraair.health import healthz

# Password-reset flow. All four views must bypass LoginRequiredMiddleware
# (that's the whole point -- these are for people who can't sign in) via
# the login_not_required decorator, applied one layer up from as_view().
# email_template_name/subject_template_name are set so Django reaches for
# the ones in templates/registration/ we've provided, not the plain-text
# defaults bundled with django.contrib.auth. form_class swaps in
# InviteCapablePasswordResetForm -- the stock PasswordResetForm silently
# refuses to email any account with has_usable_password() == False, which
# is every account created via the admin's no-password add-user form
# (see library/admin.py) until they've completed their first setup.
urlpatterns = [
    path('healthz/', healthz, name='healthz'),
    path('admin/', admin.site.urls),
    path('login/', auth_views.LoginView.as_view(template_name='registration/login.html'), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('password-reset/', login_not_required(auth_views.PasswordResetView.as_view(
        form_class=InviteCapablePasswordResetForm,
        email_template_name='registration/password_reset_email.html',
        subject_template_name='registration/password_reset_subject.txt',
    )), name='password_reset'),
    path('password-reset/sent/', login_not_required(
        auth_views.PasswordResetDoneView.as_view()
    ), name='password_reset_done'),
    path('password-reset/confirm/<uidb64>/<token>/', login_not_required(
        auth_views.PasswordResetConfirmView.as_view()
    ), name='password_reset_confirm'),
    path('password-reset/complete/', login_not_required(
        auth_views.PasswordResetCompleteView.as_view()
    ), name='password_reset_complete'),
    path('monitoring/', include('monitoring.urls')),
    path('updates/', include('updatecenter.urls')),
    path('rbds/', include('rbds.urls')),
    path('wx/', include('weather.urls')),
    path('', include('webrequests.urls')),
    path('', include('aircheck.urls')),
    path('', include('library.urls')),
]
