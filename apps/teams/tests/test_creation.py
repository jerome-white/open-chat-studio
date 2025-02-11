import pytest
from django.test import TestCase
from django.urls import reverse

from apps.teams.backends import SUPER_ADMIN_GROUP
from apps.teams.helpers import create_default_team_for_user
from apps.teams.models import Membership, Team
from apps.teams.roles import is_admin
from apps.users.models import CustomUser
from apps.utils.factories.user import UserFactory


class TeamCreationTest(TestCase):
    def test_create_for_user(self):
        email = "alice@example.com"
        user = CustomUser.objects.create(
            username=email,
            email=email,
        )
        team = create_default_team_for_user(user)
        assert "Alice" == team.name
        assert "alice" == team.slug
        assert is_admin(user, team)
        membership = team.membership_set.filter(user=user).first()
        assert [SUPER_ADMIN_GROUP] == [group.name for group in membership.groups.all()]


@pytest.mark.django_db()
def test_slug_generation_on_team_creation(client):
    """Test to verify that the slug is autogenerated when creating a team"""
    user = UserFactory()
    client.force_login(user)

    client.post(reverse("teams:create_team"), {"name": "Foo"})
    created_team = Team.objects.get(name="Foo")
    assert created_team is not None
    assert created_team.slug is not None
    assert created_team.slug != ""


@pytest.mark.django_db()
def test_group_owner_assignment_on_team_creation(client):
    """Test to make sure that user is assigned as group owner when they create a team"""
    user = UserFactory()
    client.force_login(user)

    client.post(reverse("teams:create_team"), {"name": "Bar"})
    created_team = Team.objects.get(name="Bar")

    membership = Membership.objects.filter(team__slug=created_team.slug).first()
    permission_group = membership.groups.first()
    assert permission_group.name == SUPER_ADMIN_GROUP
