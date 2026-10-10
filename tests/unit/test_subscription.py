"""Tests for the hosted subscription: read-only mode, the storage limit and the banner."""

import itertools
import json
import os
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from flask_babel import Babel

from pikaraoke.lib.subscription import (
    PAYMENT_REQUIRED,
    Subscription,
    directory_size,
    install_subscription_gate,
)
from pikaraoke.routes.home import home_bp
from pikaraoke.routes.search_api import search_api_bp
from tests.conftest import make_route_app

READ_ONLY_MESSAGE = "The free trial has ended, so the app is read-only."


# Two writes inside one mtime tick would look unchanged to the reader, so each
# write gets its own later timestamp.
_mtimes = itertools.count(1_700_000_000)


def write_subscription(path, **data):
    path.write_text(json.dumps(data))
    stamp = next(_mtimes)
    os.utime(path, (stamp, stamp))


@pytest.fixture
def subscription_path(tmp_path):
    return tmp_path / "subscription.json"


class TestSubscriptionFile:
    def test_self_hosted_has_no_limits(self, subscription_path, tmp_path):
        subscription = Subscription(str(subscription_path))

        assert subscription.state().mode == "full"
        assert not subscription.state().is_read_only
        assert not subscription.storage_full(str(tmp_path))

    def test_reads_what_the_owner_app_wrote(self, subscription_path):
        write_subscription(
            subscription_path,
            mode="warn",
            message="Free trial: 3 days left.",
            plan_name="Small",
            max_storage_bytes=10_000,
        )

        state = Subscription(str(subscription_path)).state()

        assert state.mode == "warn"
        assert state.message == "Free trial: 3 days left."
        assert state.plan_name == "Small"
        assert state.max_storage_bytes == 10_000

    def test_picks_up_a_rewrite(self, subscription_path):
        write_subscription(subscription_path, mode="full")
        subscription = Subscription(str(subscription_path))
        assert not subscription.state().is_read_only

        write_subscription(subscription_path, mode="read_only", message=READ_ONLY_MESSAGE)

        assert subscription.state().is_read_only

    def test_a_corrupt_file_keeps_the_last_state(self, subscription_path):
        write_subscription(subscription_path, mode="read_only", message=READ_ONLY_MESSAGE)
        subscription = Subscription(str(subscription_path))
        assert subscription.state().is_read_only

        subscription_path.write_text("{not json")
        stamp = next(_mtimes)
        os.utime(subscription_path, (stamp, stamp))

        assert subscription.state().is_read_only

    def test_storage_full_at_the_plan_limit(self, subscription_path, tmp_path):
        songs = tmp_path / "songs"
        songs.mkdir()
        (songs / "a---aaaaaaaaaaa.mp4").write_bytes(b"x" * 600)
        (songs / "b---bbbbbbbbbbb.mp4").write_bytes(b"x" * 400)
        assert directory_size(str(songs)) == 1000

        write_subscription(subscription_path, mode="full", max_storage_bytes=1001)
        subscription = Subscription(str(subscription_path))
        assert not subscription.storage_full(str(songs))

        write_subscription(subscription_path, mode="full", max_storage_bytes=1000)
        assert subscription.storage_full(str(songs))


@pytest.fixture
def gated_app(subscription_path):
    app = Flask(__name__)
    app.secret_key = "test"
    Babel(app)
    app.add_url_rule("/", "home.home", lambda: "home")
    app.add_url_rule("/api/change", "change", lambda: "changed", methods=["POST"])
    app.add_url_rule("/files/delete", "delete", lambda: "deleted", methods=["POST"])
    app.add_url_rule("/api/auth", "auth_api.login", lambda: "logged in", methods=["POST"])
    app.config["SUBSCRIPTION"] = Subscription(str(subscription_path))
    install_subscription_gate(app)
    return app


class TestReadOnlyGate:
    def test_full_mode_allows_changes(self, gated_app, subscription_path):
        write_subscription(subscription_path, mode="full")

        assert gated_app.test_client().post("/api/change").data == b"changed"

    def test_read_only_refuses_api_changes_with_the_message(self, gated_app, subscription_path):
        write_subscription(subscription_path, mode="read_only", message=READ_ONLY_MESSAGE)

        response = gated_app.test_client().post("/api/change")

        assert response.status_code == PAYMENT_REQUIRED
        assert response.json == {"error": READ_ONLY_MESSAGE}

    def test_read_only_refuses_page_changes_with_a_flash(self, gated_app, subscription_path):
        write_subscription(subscription_path, mode="read_only", message=READ_ONLY_MESSAGE)
        client = gated_app.test_client()

        response = client.post("/files/delete")

        assert response.status_code == 302
        with client.session_transaction() as session:
            assert ("is-danger", READ_ONLY_MESSAGE) in session["_flashes"]

    def test_read_only_still_allows_browsing_and_logging_in(self, gated_app, subscription_path):
        write_subscription(subscription_path, mode="read_only", message=READ_ONLY_MESSAGE)
        client = gated_app.test_client()

        assert client.get("/").data == b"home"
        assert client.post("/api/auth").data == b"logged in"


class TestStorageLimit:
    @pytest.fixture
    def app(self, subscription_path):
        app = make_route_app(search_api_bp, [])
        app.config["SUBSCRIPTION"] = Subscription(str(subscription_path))
        return app

    def test_download_refused_when_storage_is_full(self, client, subscription_path, tmp_path):
        write_subscription(subscription_path, mode="full", max_storage_bytes=0)
        k = MagicMock(download_path=str(tmp_path))

        with patch("pikaraoke.routes.search_api.get_karaoke_instance", return_value=k):
            response = client.post(
                "/api/download",
                json={
                    "song_url": "https://youtu.be/aaaaaaaaaaa",
                    "song_added_by": "Ana",
                    "song_title": "x",
                },
            )

        assert response.status_code == PAYMENT_REQUIRED
        assert "storage is full" in response.json["error"]
        k.download_manager.queue_download.assert_not_called()

    def test_download_allowed_under_the_limit(self, client, subscription_path, tmp_path):
        write_subscription(subscription_path, mode="full", max_storage_bytes=10_000)
        k = MagicMock(download_path=str(tmp_path))

        with patch("pikaraoke.routes.search_api.get_karaoke_instance", return_value=k):
            response = client.post(
                "/api/download",
                json={
                    "song_url": "https://youtu.be/aaaaaaaaaaa",
                    "song_added_by": "Ana",
                    "song_title": "x",
                },
            )

        assert response.status_code == 200
        k.download_manager.queue_download.assert_called_once()


class TestBanner:
    @pytest.fixture
    def app(self, subscription_path):
        app = make_route_app(home_bp, [("/api/now_playing", "now_playing.now_playing")])
        app.config["SUBSCRIPTION"] = Subscription(str(subscription_path))
        return app

    def _home(self, client):
        k = MagicMock(volume=0.5)
        with (
            patch("pikaraoke.routes.home.get_karaoke_instance", return_value=k),
            patch("pikaraoke.routes.home.get_site_name", return_value="Karaoke ni Lakay"),
        ):
            response = client.get("/")
        assert response.status_code == 200
        return response.data.decode()

    def test_warning_shows_on_every_page(self, client, subscription_path):
        write_subscription(subscription_path, mode="warn", message="Free trial: 3 days left.")

        body = self._home(client)

        assert 'id="subscription-notice"' in body
        assert "Free trial: 3 days left." in body

    def test_no_banner_when_self_hosted(self, client):
        assert 'id="subscription-notice"' not in self._home(client)
