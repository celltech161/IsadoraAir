"""Fixtures for the D3C produced-content tests: users with the established
Produced Content capabilities, regular/test types, isolated media and library
roots, CSRF-enforcing clients."""
from __future__ import annotations

import json

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client

from authz.models import Capability, GroupRole, Role, RoleCapability
from library.models import Category, CategoryKind, GroupAccess
from produced_content.authority import CONTROL, PRODUCE
from produced_content.models import ProducedContentType, ProducedContentTypeAssignment
from production.models import ProductionMedia
from production.tests.support import VALID_FACTS, IsolatedMediaRootMixin, fixture, make_row
from spoken.tests.projection_support import IsolatedLibraryRootMixin

User = get_user_model()
CSRF = "d3cTestCsrfSecret0123456789abcde"                    # a well-formed 32-character secret
PAGE = "/iportal/content/"
RECORDER = "/iportal/content/record/"
API = "/iportal/content/api/recorder/"


def category(code="PCNEWS"):
    kind, _ = CategoryKind.objects.get_or_create(code="spoken", defaults={"name": "Spoken"})
    return Category.objects.get_or_create(code=code, defaults={"name": code, "kind": kind})[0]


def regular_type(key, *, output_mode="single", recipe_key="lcl_psa", category_code="PCNEWS", **values):
    return ProducedContentType.objects.create(
        key=key, name=values.pop("name", key), delivery_mode="regular", output_mode=output_mode,
        recipe_key=recipe_key, target_category=category(category_code), **values)


def no_air_type(key, **values):
    return ProducedContentType.objects.create(key=key, name=values.pop("name", key), delivery_mode="test", **values)


def assign(user, *content_types):
    for content_type in content_types:
        ProducedContentTypeAssignment.objects.get_or_create(user=user, content_type=content_type)


def user_with_role(username, role_name=None, **flags):
    user = User.objects.create_user(username, **flags)
    if role_name:
        group, _ = Group.objects.get_or_create(name=f"test-{role_name}")
        GroupRole.objects.update_or_create(group=group, defaults={"role": Role.objects.get(name=role_name)})
        user.groups.add(group)
    return user


def media_row(owner=None, *, kind=ProductionMedia.KIND_RECORDING, seconds="3.000000", **overrides):
    from django.utils import timezone
    values = {**VALID_FACTS, "decoded_duration_seconds": seconds, "validated_at": timezone.now(),
              "owner": owner, "owner_username": owner.get_username() if owner else "", **overrides}
    if values.get("validation_state") == "unvalidated":
        values["validated_at"] = None
        values["validation_code"] = ""
    return make_row(kind=kind, **values)


class People(IsolatedLibraryRootMixin, IsolatedMediaRootMixin):
    """``producer``/``producer2`` (PRODUCE via a custom role), ``controller``
    (CONTROL only), ``host`` (Remote Host: neither), ``staff`` (staff, no role).
    Producer groups are granted ``/iportal/`` (GroupAccess)."""

    def setUp(self):
        super().setUp()
        produce_only, _ = Role.objects.get_or_create(name="Test Producer")
        RoleCapability.objects.get_or_create(role=produce_only, capability=Capability.objects.get(slug=PRODUCE))
        control_only, _ = Role.objects.get_or_create(name="Test Controller")
        RoleCapability.objects.get_or_create(role=control_only, capability=Capability.objects.get(slug=CONTROL))
        self.producer = user_with_role("producer", "Test Producer")
        self.producer2 = user_with_role("producer2", "Test Producer")
        self.unassigned = user_with_role("producer3", "Test Producer")
        self.controller = user_with_role("controller", "Test Controller")
        self.host = user_with_role("host", "Remote Host")
        for group in Group.objects.filter(name__startswith="test-"):
            GroupAccess.objects.update_or_create(group=group, defaults={"allowed_prefixes": "/iportal/"})
        self._clients = {}

    def client_for(self, user):
        if user.pk not in self._clients:
            client = Client(enforce_csrf_checks=True)
            client.force_login(user)
            client.cookies[settings.CSRF_COOKIE_NAME] = CSRF
            self._clients[user.pk] = client
        return self._clients[user.pk]

    def post(self, user, url, data=None):
        return self.client_for(user).post(url, data or {}, HTTP_X_CSRFTOKEN=CSRF)

    def post_json(self, user, url, data):
        return self.client_for(user).post(url, json.dumps(data), content_type="application/json",
                                          HTTP_X_CSRFTOKEN=CSRF)

    def take(self, user, query, *, mode="record", data=None, extra=""):
        return self.client_for(user).generic("POST", f"{API}take/?{query}&mode={mode}{extra}",
                                             data if data is not None else fixture("wav16_mono.wav"),
                                             content_type="audio/wav", HTTP_X_CSRFTOKEN=CSRF)

    def recorded(self, user, query, **kwargs):
        response = self.take(user, query, **kwargs)
        assert response.status_code == 201, response.content
        return response.json()["media"]["media_id"]

    def context(self, user, query):
        return self.client_for(user).get(f"{API}context/?{query}")

    def commit(self, user, subject, media_id, revision):
        return self.post_json(user, f"{API}commit/", {"subject": subject, "media_id": media_id,
                                                      "revision": revision})
