"""Hosted subscription state, written into the data directory by the owner app.

A venue hosted by the Karaoke ni Lakay owner app (see cloud/) has a
subscription.json beside its config.ini. A self-hosted install has none and
runs with no banner and no limits.
"""

import json
import logging
import os
from dataclasses import dataclass

import flask_babel
from flask import Flask, flash, jsonify, redirect, request, url_for
from flask.typing import ResponseReturnValue

_ = flask_babel.gettext

SUBSCRIPTION_FILE = "subscription.json"

# Logging in and out must still work while read-only, or the host could never
# get back in to see why.
_ALWAYS_ALLOWED = frozenset({"admin.auth", "admin.logout", "auth_api.login", "static"})

# Mirrors HTTP 402 Payment Required: what base.html's ajaxError handler shows.
PAYMENT_REQUIRED = 402


@dataclass(frozen=True)
class SubscriptionState:
    """What the venue may do right now, as the owner app decided it.

    mode is "full", "warn" (works, with a banner) or "read_only" (browse only).
    """

    mode: str = "full"
    message: str = ""
    plan_name: str = ""
    max_storage_bytes: int | None = None

    @property
    def is_read_only(self) -> bool:
        return self.mode == "read_only"


class Subscription:
    """Reads subscription.json, again only when the owner app has rewritten it."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._mtime: float | None = None
        self._state = SubscriptionState()

    def state(self) -> SubscriptionState:
        try:
            mtime = os.stat(self.path).st_mtime
        except FileNotFoundError:
            self._mtime, self._state = None, SubscriptionState()
            return self._state
        if mtime != self._mtime:
            self._mtime = mtime
            self._state = self._load()
        return self._state

    def _load(self) -> SubscriptionState:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return SubscriptionState(
                mode=data["mode"],
                message=data.get("message", ""),
                plan_name=data.get("plan_name", ""),
                max_storage_bytes=data.get("max_storage_bytes"),
            )
        except (OSError, ValueError, KeyError, TypeError) as e:
            # The owner app replaces the file atomically, so this is corruption
            # or a hand edit. Keep what was last read rather than unlocking.
            logging.error(f"Ignoring unreadable {self.path}: {e}")
            return self._state

    def storage_full(self, songs_dir: str) -> bool:
        """True when the plan has a storage limit and the song library has reached it."""
        limit = self.state().max_storage_bytes
        return limit is not None and directory_size(songs_dir) >= limit


def directory_size(path: str) -> int:
    """Total size in bytes of the files under path."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                # Deleted or renamed between listing and measuring.
                continue
    return total


def refuse(message: str) -> ResponseReturnValue:
    """Answer a request the subscription does not allow, in the caller's medium."""
    if request.path.startswith("/api/"):
        return jsonify({"error": message}), PAYMENT_REQUIRED
    flash(message, "is-danger")
    return redirect(url_for("home.home"))


def install_subscription_gate(app: Flask) -> None:
    """While read-only, refuse every request that would change something.

    Reads the Subscription from app.config["SUBSCRIPTION"].

    Every route that changes state is a POST, PUT or DELETE, so the method alone
    decides. Socket events stay open: they only report playback, which lets a
    song already playing finish.
    """

    @app.before_request
    def require_active_subscription() -> ResponseReturnValue | None:
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        if request.endpoint is None or request.endpoint in _ALWAYS_ALLOWED:
            return None
        state = app.config["SUBSCRIPTION"].state()
        if not state.is_read_only:
            return None
        return refuse(state.message)

    @app.context_processor
    def inject_subscription() -> dict[str, SubscriptionState]:
        return {"subscription": app.config["SUBSCRIPTION"].state()}
