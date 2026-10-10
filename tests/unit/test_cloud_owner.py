"""Tests for the hosting owner app: trial and payment rules, and what each action does."""

import json
import os
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from docker.errors import NotFound

from cloud.owner import Cloud, Settings, access_for, add_months, create_app
from pikaraoke.lib.admin_auth import AdminAuth
from pikaraoke.lib.preference_manager import PreferenceManager

TODAY = date(2026, 10, 10)


def trial(ends: str) -> dict:
    return {"status": "trial", "trial_ends_at": ends, "paid_until": None}


def active(paid_until: str | None) -> dict:
    return {"status": "active", "trial_ends_at": None, "paid_until": paid_until}


class TestAccess:
    def test_trial_with_time_left_is_full(self):
        assert access_for(trial("2026-11-01"), TODAY)["mode"] == "full"

    def test_trial_warns_in_its_last_week(self):
        access = access_for(trial("2026-10-11"), TODAY, "chris@example.com")

        assert access["mode"] == "warn"
        assert "1 day left" in access["message"]
        assert "chris@example.com" in access["message"]

    def test_trial_ends_read_only(self):
        assert access_for(trial("2026-10-10"), TODAY)["mode"] == "read_only"

    def test_paid_up_is_full(self):
        assert access_for(active("2026-12-01"), TODAY) == {"mode": "full", "message": ""}

    def test_overdue_warns_through_the_grace_period(self):
        access = access_for(active("2026-10-05"), TODAY)

        assert access["mode"] == "warn"
        assert "12 Oct 2026" in access["message"]

    def test_overdue_past_the_grace_period_is_read_only(self):
        assert access_for(active("2026-10-03"), TODAY)["mode"] == "read_only"

    def test_made_read_only_by_the_owner(self):
        venue = {"status": "read_only", "trial_ends_at": None, "paid_until": "2027-01-01"}

        assert access_for(venue, TODAY)["mode"] == "read_only"


def test_add_months_keeps_the_day_or_clamps_it():
    assert add_months(date(2026, 10, 10), 1) == date(2026, 11, 10)
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2026, 12, 15), 12) == date(2027, 12, 15)


@pytest.fixture
def docker_client():
    client = MagicMock()
    client.containers.get.side_effect = NotFound("no container")
    return client


@pytest.fixture
def cloud(tmp_path, docker_client):
    settings = Settings(
        base_domain="karaoke.example.com",
        owner_password="owner-secret",
        host_data_dir=tmp_path,
        owner_contact="chris@example.com",
        today=lambda: TODAY,
    )
    # Tests do not run as root, so the venue files cannot be given to uid 1000.
    with patch("cloud.owner.os.chown"):
        yield Cloud(settings, docker_client)


def add(cloud, **overrides):
    form = {
        "name": "Lakay Bar",
        "slug": "lakaybar",
        "plan": "small",
        "trial_days": "14",
        "admin_password": "venue-pass",
        **overrides,
    }
    return cloud.add_venue(form)


def subscription(cloud, slug="lakaybar") -> dict:
    return json.loads((cloud.venue_dir(slug) / "data" / "subscription.json").read_text())


class TestAddVenue:
    def test_creates_files_password_subscription_and_container(self, cloud, docker_client):
        assert add(cloud) == ""

        venue = cloud.venues["lakaybar"]
        assert venue["status"] == "trial"
        assert venue["trial_ends_at"] == "2026-10-24"
        assert (cloud.venue_dir("lakaybar") / "songs").is_dir()

        config = cloud.venue_dir("lakaybar") / "data" / "config.ini"
        assert AdminAuth(PreferenceManager(str(config))).verify("venue-pass")

        assert subscription(cloud) == {
            "mode": "full",
            "message": "Free trial until 24 Oct 2026.",
            "plan_name": "Small",
            "max_storage_bytes": 5 * 10**9,
        }

        run = docker_client.containers.run.call_args
        assert run.kwargs["name"] == "knl-lakaybar"
        assert run.kwargs["command"] == ["-u", "https://lakaybar.karaoke.example.com"]
        assert (
            run.kwargs["labels"]["traefik.http.routers.knl-lakaybar.rule"]
            == "Host(`lakaybar.karaoke.example.com`)"
        )

    def test_saved_across_restarts(self, cloud, tmp_path, docker_client):
        add(cloud)

        reloaded = Cloud(cloud.settings, docker_client)

        assert reloaded.venues["lakaybar"]["name"] == "Lakay Bar"

    @pytest.mark.parametrize(
        "overrides, error",
        [
            ({"name": ""}, "Enter the venue's name."),
            ({"slug": "Bad Address"}, "lowercase letters"),
            ({"slug": "owner"}, "lowercase letters"),
            ({"plan": "huge"}, "Pick a plan."),
            ({"admin_password": "123"}, "at least 6 characters"),
        ],
    )
    def test_refuses_bad_input(self, cloud, docker_client, overrides, error):
        assert error in add(cloud, **overrides)
        assert cloud.venues == {}
        docker_client.containers.run.assert_not_called()

    def test_refuses_a_taken_address(self, cloud):
        add(cloud)

        assert "already taken" in add(cloud, name="Another")


class TestOwnerActions:
    def test_payment_runs_from_today_then_from_paid_until(self, cloud):
        add(cloud)

        cloud.record_payment("lakaybar", 1)
        assert cloud.venues["lakaybar"]["status"] == "active"
        assert cloud.venues["lakaybar"]["paid_until"] == "2026-11-10"

        cloud.record_payment("lakaybar", 12)
        assert cloud.venues["lakaybar"]["paid_until"] == "2027-11-10"
        assert subscription(cloud)["mode"] == "full"

    def test_extend_trial_runs_from_the_current_end(self, cloud):
        add(cloud)

        cloud.extend_trial("lakaybar")

        assert cloud.venues["lakaybar"]["trial_ends_at"] == "2026-11-07"

    def test_read_only_then_back_on(self, cloud):
        add(cloud)

        cloud.update("lakaybar", status="read_only")
        assert subscription(cloud)["mode"] == "read_only"

        assert cloud.turn_back_on("lakaybar") == ""
        assert cloud.venues["lakaybar"]["status"] == "trial"
        assert subscription(cloud)["mode"] == "full"

    def test_turn_back_on_needs_time_paid_or_left(self, cloud):
        add(cloud, trial_days="0")
        cloud.update("lakaybar", status="read_only")

        assert "record a payment" in cloud.turn_back_on("lakaybar")
        assert cloud.venues["lakaybar"]["status"] == "read_only"

    def test_refresh_catches_a_trial_running_out(self, cloud):
        add(cloud, trial_days="1")
        assert subscription(cloud)["mode"] == "warn"

        cloud.settings = Settings(
            **{**cloud.settings.__dict__, "today": lambda: date(2026, 10, 11)}
        )
        cloud.refresh_all()

        assert subscription(cloud)["mode"] == "read_only"

    def test_refresh_leaves_an_unchanged_subscription_alone(self, cloud):
        add(cloud)
        target = cloud.venue_dir("lakaybar") / "data" / "subscription.json"
        os.utime(target, (1, 1))

        cloud.refresh_all()

        assert target.stat().st_mtime == 1

    def test_ensure_container_replaces_an_outdated_image(self, cloud, docker_client):
        add(cloud)
        old = MagicMock(status="running", attrs={"Image": "sha256:old"})
        docker_client.containers.get.side_effect = None
        docker_client.containers.get.return_value = old
        docker_client.images.get.return_value.id = "sha256:new"
        docker_client.containers.run.reset_mock()

        cloud.ensure_container("lakaybar")

        old.remove.assert_called_once_with(force=True)
        docker_client.containers.run.assert_called_once()


class TestOwnerPage:
    @pytest.fixture
    def client(self, cloud):
        return create_app(cloud).test_client()

    def login(self, client):
        return client.post("/login", data={"password": "owner-secret"})

    def test_requires_the_owner_password(self, client):
        assert client.get("/").headers["Location"].endswith("/login")

        with patch("cloud.owner.time.sleep"):
            client.post("/login", data={"password": "guess"})
        assert client.get("/").status_code == 302

        self.login(client)
        assert client.get("/").status_code == 200

    def test_lists_venues_with_their_state(self, client, cloud):
        add(cloud, trial_days="3")
        self.login(client)

        body = client.get("/").data.decode()

        assert "Lakay Bar" in body
        assert "lakaybar.karaoke.example.com" in body
        assert "Needs attention" in body
        assert "Free trial: 3 days left" in body

    def test_paid_button_records_a_month(self, client, cloud):
        add(cloud)
        self.login(client)

        client.post("/venues/lakaybar/paid", data={"months": "1"})

        assert cloud.venues["lakaybar"]["paid_until"] == "2026-11-10"

    def test_reset_password_restarts_the_venue(self, client, cloud, docker_client):
        add(cloud)
        self.login(client)
        docker_client.containers.get.side_effect = None

        client.post("/venues/lakaybar/admin-password", data={"password": "new-pass"})

        config = cloud.venue_dir("lakaybar") / "data" / "config.ini"
        assert AdminAuth(PreferenceManager(str(config))).verify("new-pass")
        docker_client.containers.get.return_value.restart.assert_called_once()
