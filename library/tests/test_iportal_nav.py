"""Focused coverage for the product iPortal navigation entry."""
import importlib

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import RequestFactory, TestCase
from django.urls import resolve, reverse

from library.context_processors import nav_menu
from library.middleware import _invalidate_group_map_cache
from library.models import GroupAccess, NavMenuItem


migration = importlib.import_module("library.migrations.0090_seed_iportal_nav_item")
User = get_user_model()


class IPortalNavTests(TestCase):
    def setUp(self):
        migration.seed_iportal_nav(django_apps, None)
        self.factory = RequestFactory()

    def tearDown(self):
        _invalidate_group_map_cache()

    def menu_for(self, user, path):
        request = self.factory.get(path)
        request.user = user
        request.resolver_match = resolve(path)
        return nav_menu(request)["nav_menu_items"]

    def iportal_for(self, user, path):
        return next(
            (item for item in self.menu_for(user, path) if item.url_name == migration.URL_NAME),
            None,
        )

    def test_seeded_item_resolves_and_staff_sees_it(self):
        item = NavMenuItem.objects.get(url_name=migration.URL_NAME)
        self.assertEqual(item.resolved_url, reverse("iportal-index"))
        self.assertEqual(item.resolved_url, "/iportal/")
        self.assertTrue(item.enabled)
        self.assertFalse(item.open_in_new_tab)

        staff = User.objects.create_user("nav-staff", is_staff=True)
        self.assertIsNotNone(self.iportal_for(staff, "/iportal/"))

    def test_restricted_navigation_follows_group_access_without_widening_it(self):
        allowed_group = Group.objects.create(name="iportal-nav-allowed")
        GroupAccess.objects.create(group=allowed_group, allowed_prefixes="/iportal/")
        allowed = User.objects.create_user("iportal-allowed")
        allowed.groups.add(allowed_group)
        self.assertIsNotNone(self.iportal_for(allowed, "/iportal/"))

        other_group = Group.objects.create(name="iportal-nav-other")
        GroupAccess.objects.create(group=other_group, allowed_prefixes="/library/")
        other = User.objects.create_user("iportal-other")
        other.groups.add(other_group)
        self.assertIsNone(self.iportal_for(other, "/library/"))

    def test_iportal_section_page_names_keep_the_item_active(self):
        staff = User.objects.create_user("active-staff", is_staff=True)
        for path in (
            "/iportal/",
            "/iportal/messages/",
            "/iportal/messages/record/",
            "/voicetracks/",
            "/voicetracks/studio/",
        ):
            with self.subTest(path=path, view_name=resolve(path).view_name):
                item = self.iportal_for(staff, path)
                self.assertIsNotNone(item)
                self.assertTrue(item.is_active)


class IPortalNavMigrationTests(TestCase):
    def test_forward_is_idempotent_and_reverse_removes_only_the_seeded_row(self):
        NavMenuItem.objects.filter(url_name=migration.URL_NAME).delete()
        unrelated = NavMenuItem.objects.create(
            label="Station custom link",
            custom_url="/station-custom/",
            sort_order=migration.SORT_ORDER,
        )

        migration.seed_iportal_nav(django_apps, None)
        migration.seed_iportal_nav(django_apps, None)
        self.assertEqual(NavMenuItem.objects.filter(url_name=migration.URL_NAME).count(), 1)

        migration.unseed_iportal_nav(django_apps, None)
        self.assertFalse(NavMenuItem.objects.filter(url_name=migration.URL_NAME).exists())
        self.assertTrue(NavMenuItem.objects.filter(pk=unrelated.pk).exists())

    def test_reverse_preserves_a_customized_iportal_row(self):
        item = NavMenuItem.objects.get(url_name=migration.URL_NAME)
        item.label = "My iPortal"
        item.save(update_fields=["label"])

        migration.unseed_iportal_nav(django_apps, None)
        self.assertTrue(NavMenuItem.objects.filter(pk=item.pk, label="My iPortal").exists())
