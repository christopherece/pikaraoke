"""Karaoke ni Lakay Cloud: the owner page, and one karaoke container per venue.

    http://owner.<BASE_DOMAIN>    the owner page (OWNER_PASSWORD)
    http://<venue>.<BASE_DOMAIN>  each venue's own Karaoke ni Lakay

One process cannot serve several venues -- each has its own queue, player and
song library -- so every venue is its own container, routed by Traefik from the
labels set here. The owner app decides what each venue may do and writes it to
the venue's subscription.json, which the venue reads (pikaraoke/lib/subscription.py).

Environment:
    BASE_DOMAIN     e.g. karaoke.example.com ("localhost" to try it on your computer)
    OWNER_PASSWORD  password for the owner page
    OWNER_CONTACT   shown to venues when they need to renew, e.g. an email address
    HOST_DATA_DIR   absolute path on the host holding owner.json and every venue's
                    files; mounted at the same path in this container, since the
                    venue containers' bind mounts are given as host paths
    PUBLIC_SCHEME   "https" behind a TLS proxy (default), "http" locally
    PUBLIC_PORT     port in the public URL, when not the scheme's default
    VENUE_IMAGE     image each venue runs (default karaoke-ni-lakay:latest)
    VENUE_NETWORK   Docker network Traefik reaches venues on (default karaoke-venues)
    TZ              timezone for the venues and for trial and payment dates
"""

import calendar
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import docker
from docker.errors import DockerException, NotFound
from docker.types import LogConfig
from flask import Flask, flash, redirect, render_template, request, session, url_for
from flask_babel import Babel

from pikaraoke.lib.admin_auth import AdminAuth
from pikaraoke.lib.preference_manager import PreferenceManager
from pikaraoke.lib.subscription import SUBSCRIPTION_FILE, directory_size


@dataclass(frozen=True)
class Plan:
    name: str
    max_storage_gb: int | None  # None = no limit


# What each plan allows. A karaoke video is roughly 30-60 MB, so 5 GB holds
# around a hundred songs.
PLANS = {
    "small": Plan("Small", 5),
    "medium": Plan("Medium", 15),
    "large": Plan("Large", None),
}
STATUSES = ("trial", "active", "read_only")
# After a payment is due, the venue keeps working this long with a warning, then turns read-only.
GRACE_DAYS = 7
# Warn this many days before a trial ends.
WARN_DAYS = 7
TRIAL_EXTENSION_DAYS = 14
SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
RESERVED = frozenset({"owner", "www", "api", "admin", "traefik", "mail", "static"})
# The uid the karaoke image runs as, which must own each venue's files.
VENUE_UID = 1000
# Short, so a venue catches a new day soon after a sleeping host wakes; a check
# that changes nothing writes nothing.
REFRESH_SECONDS = 300


def fmt_date(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%-d %b %Y")


def fmt_size(size: int) -> str:
    if size >= 10**9:
        return f"{size / 10**9:.1f} GB"
    return f"{size // 10**6} MB"


def add_months(start: date, months: int) -> date:
    """Same day of the month, months later; the 31st becomes the month's last day."""
    month_index = start.month - 1 + months
    year, month = start.year + month_index // 12, month_index % 12 + 1
    return start.replace(
        year=year, month=month, day=min(start.day, calendar.monthrange(year, month)[1])
    )


def access_for(venue: dict, today: date, contact: str = "") -> dict:
    """What a venue can do today, from its status and dates.

    mode is "full" (everything works), "warn" (works, with a banner) or
    "read_only" (browse only: no queueing, downloading or changes).
    """
    renew = f" Contact {contact} to renew." if contact else " Contact your provider to renew."
    if venue["status"] == "read_only":
        return {
            "mode": "read_only",
            "message": f"This venue's subscription is not active, so nothing can be queued or downloaded.{renew}",
        }
    if venue["status"] == "trial":
        trial_ends = venue.get("trial_ends_at")
        left = (date.fromisoformat(trial_ends) - today).days if trial_ends else 0
        if left <= 0:
            return {
                "mode": "read_only",
                "message": f"The free trial has ended, so nothing can be queued or downloaded.{renew}",
            }
        if left <= WARN_DAYS:
            days = "day" if left == 1 else "days"
            return {
                "mode": "warn",
                "message": f"Free trial: {left} {days} left (ends {fmt_date(trial_ends)}).{renew}",
            }
        return {"mode": "full", "message": f"Free trial until {fmt_date(trial_ends)}."}
    paid_until = venue.get("paid_until")
    if paid_until and today >= date.fromisoformat(paid_until):
        read_only_from = date.fromisoformat(paid_until) + timedelta(days=GRACE_DAYS)
        if today >= read_only_from:
            return {
                "mode": "read_only",
                "message": f"Payment is overdue, so nothing can be queued or downloaded.{renew}",
            }
        return {
            "mode": "warn",
            "message": f"Payment is overdue. Queueing and downloading stop on {fmt_date(read_only_from.isoformat())}.{renew}",
        }
    return {"mode": "full", "message": ""}


@dataclass(frozen=True)
class Settings:
    base_domain: str
    owner_password: str
    host_data_dir: Path
    owner_contact: str = ""
    public_scheme: str = "https"
    public_port: str = ""
    venue_image: str = "karaoke-ni-lakay:latest"
    venue_network: str = "karaoke-venues"
    timezone: str = "Asia/Manila"
    today: Callable[[], date] = field(default=date.today)

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        return cls(
            base_domain=env.get("BASE_DOMAIN", "localhost").lower(),
            owner_password=env.get("OWNER_PASSWORD", ""),
            host_data_dir=Path(env["HOST_DATA_DIR"]),
            owner_contact=env.get("OWNER_CONTACT", ""),
            public_scheme=env.get("PUBLIC_SCHEME", "https"),
            public_port=env.get("PUBLIC_PORT", ""),
            venue_image=env.get("VENUE_IMAGE", "karaoke-ni-lakay:latest"),
            venue_network=env.get("VENUE_NETWORK", "karaoke-venues"),
            timezone=env.get("TZ", "Asia/Manila"),
        )

    def public_url(self, subdomain: str) -> str:
        port = f":{self.public_port}" if self.public_port else ""
        return f"{self.public_scheme}://{subdomain}.{self.base_domain}{port}"


class Cloud:
    """The venues, their files on disk and their containers."""

    def __init__(self, settings: Settings, docker_client=None) -> None:
        self.settings = settings
        self._docker = docker_client
        self.lock = threading.RLock()
        self.owner_file = settings.host_data_dir / "owner.json"
        (settings.host_data_dir / "venues").mkdir(parents=True, exist_ok=True)
        try:
            self.venues: dict[str, dict] = json.loads(self.owner_file.read_text())["venues"]
        except FileNotFoundError:
            self.venues = {}

    @property
    def docker(self):
        # Created on first use, so the owner page and the tests need no daemon.
        if self._docker is None:
            self._docker = docker.from_env()
        return self._docker

    # --- files ---

    def venue_dir(self, slug: str) -> Path:
        return self.settings.host_data_dir / "venues" / slug

    def save(self) -> None:
        tmp = self.owner_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"venues": self.venues}, indent=1))
        os.replace(tmp, self.owner_file)

    def access(self, slug: str) -> dict:
        return access_for(self.venues[slug], self.settings.today(), self.settings.owner_contact)

    def write_subscription(self, slug: str) -> None:
        """Tell the venue what it may do now. Replaced atomically: the venue reads it any time."""
        plan = PLANS[self.venues[slug]["plan"]]
        data = {
            **self.access(slug),
            "plan_name": plan.name,
            "max_storage_bytes": plan.max_storage_gb * 10**9 if plan.max_storage_gb else None,
        }
        target = self.venue_dir(slug) / "data" / SUBSCRIPTION_FILE
        content = json.dumps(data)
        # Unchanged, it is left alone: every write makes the venue read it again.
        if target.exists() and target.read_text() == content:
            return
        tmp = target.with_suffix(".tmp")
        tmp.write_text(content)
        os.replace(tmp, target)

    def set_admin_password(self, slug: str, password: str) -> None:
        """Write the venue's admin password into its config.ini, as the venue itself would."""
        config = self.venue_dir(slug) / "data" / "config.ini"
        AdminAuth(PreferenceManager(str(config))).set_password(password)
        os.chown(config, VENUE_UID, VENUE_UID)

    def stats(self, slug: str) -> dict:
        songs = self.venue_dir(slug) / "songs"
        return {
            "storage_bytes": directory_size(str(songs)),
            "song_count": sum(1 for p in songs.iterdir() if p.is_file()) if songs.exists() else 0,
            "last_played": self._last_played(slug),
        }

    def _last_played(self, slug: str) -> str | None:
        db = self.venue_dir(slug) / "data" / "pikaraoke.db"
        if not db.exists():
            return None
        try:
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
                row = conn.execute("SELECT MAX(played_at) FROM plays").fetchone()
        except sqlite3.Error as e:
            logging.warning(f"Could not read play history for {slug}: {e}")
            return None
        return row[0] if row else None

    # --- containers ---

    def container_name(self, slug: str) -> str:
        return f"knl-{slug}"

    def container_status(self, slug: str) -> str:
        try:
            return self.docker.containers.get(self.container_name(slug)).status
        except NotFound:
            return "missing"
        except DockerException as e:
            logging.error(f"Docker is unreachable: {e}")
            return "unknown"

    def ensure_container(self, slug: str) -> None:
        """Run the venue on the current image, starting or replacing its container as needed."""
        name = self.container_name(slug)
        image_id = self.docker.images.get(self.settings.venue_image).id
        try:
            container = self.docker.containers.get(name)
        except NotFound:
            container = None
        # The ID the container records, not container.image: a rebuild can leave
        # the old image unreachable, and fetching it then fails.
        if container is not None and container.attrs["Image"] != image_id:
            logging.info(f"Updating {name} to the current image")
            container.remove(force=True)
            container = None
        if container is None:
            self._run_container(slug)
        elif container.status != "running":
            container.start()

    def restart_container(self, slug: str) -> None:
        self.docker.containers.get(self.container_name(slug)).restart()

    def _run_container(self, slug: str) -> None:
        router = f"knl-{slug}"
        venue_dir = self.venue_dir(slug)
        self.docker.containers.run(
            self.settings.venue_image,
            # The image's entrypoint supplies the song directory and --headless.
            command=["-u", self.settings.public_url(slug)],
            name=self.container_name(slug),
            detach=True,
            environment={"TZ": self.settings.timezone},
            restart_policy={"Name": "unless-stopped"},
            volumes={
                str(venue_dir / "songs"): {"bind": "/app/pikaraoke-songs", "mode": "rw"},
                str(venue_dir / "data"): {"bind": "/home/pikaraoke/.pikaraoke", "mode": "rw"},
            },
            labels={
                "traefik.enable": "true",
                f"traefik.http.routers.{router}.rule": f"Host(`{slug}.{self.settings.base_domain}`)",
                f"traefik.http.services.{router}.loadbalancer.server.port": "5555",
                "karaoke-ni-lakay.venue": slug,
            },
            network=self.settings.venue_network,
            log_config=LogConfig(type="json-file", config={"max-size": "10m", "max-file": "3"}),
        )

    # --- what the owner does ---

    def add_venue(self, form: dict) -> str:
        """Create a venue and start it. Returns an error message, or "" on success."""
        slug = form.get("slug", "").strip().lower()
        name = form.get("name", "").strip()[:80]
        password = form.get("admin_password", "")
        if not name:
            return "Enter the venue's name."
        if not SLUG.match(slug) or slug in RESERVED:
            return "The web address may use lowercase letters, numbers and dashes."
        if slug in self.venues:
            return f"{slug}.{self.settings.base_domain} is already taken."
        if form.get("plan") not in PLANS:
            return "Pick a plan."
        if len(password) < 6:
            return "The admin password needs at least 6 characters."
        try:
            trial_days = int(form.get("trial_days", TRIAL_EXTENSION_DAYS))
        except ValueError:
            return "Trial length must be a number of days."
        today = self.settings.today()
        venue = {
            "name": name,
            "plan": form["plan"],
            "status": "trial",
            "trial_ends_at": (today + timedelta(days=trial_days)).isoformat(),
            "paid_until": None,
            "contact": form.get("contact", "").strip()[:120],
            "created_at": today.isoformat(),
        }
        for sub in ("songs", "data"):
            path = self.venue_dir(slug) / sub
            path.mkdir(parents=True, exist_ok=True)
            os.chown(path, VENUE_UID, VENUE_UID)
        with self.lock:
            self.venues[slug] = venue
            self.save()
            self.write_subscription(slug)
        self.set_admin_password(slug, password)
        self.ensure_container(slug)
        return ""

    def update(self, slug: str, **changes) -> None:
        with self.lock:
            self.venues[slug] = {**self.venues[slug], **changes}
            self.save()
            self.write_subscription(slug)

    def record_payment(self, slug: str, months: int) -> None:
        """Active, and paid up for more months from today or from the current paid-until date."""
        paid_until = self.venues[slug].get("paid_until")
        start = max(
            self.settings.today(), date.fromisoformat(paid_until) if paid_until else date.min
        )
        self.update(slug, status="active", paid_until=add_months(start, months).isoformat())

    def extend_trial(self, slug: str, days: int = TRIAL_EXTENSION_DAYS) -> None:
        trial_ends = self.venues[slug].get("trial_ends_at")
        start = max(
            self.settings.today(), date.fromisoformat(trial_ends) if trial_ends else date.min
        )
        self.update(slug, status="trial", trial_ends_at=(start + timedelta(days=days)).isoformat())

    def turn_back_on(self, slug: str) -> str:
        """Undo "Make read-only". Returns an error message, or "" on success."""
        venue, today = self.venues[slug], self.settings.today()
        if venue.get("paid_until") and date.fromisoformat(venue["paid_until"]) >= today:
            self.update(slug, status="active")
        elif venue.get("trial_ends_at") and date.fromisoformat(venue["trial_ends_at"]) > today:
            self.update(slug, status="trial")
        else:
            return (
                "Nothing is paid or left of the trial: record a payment or add trial days instead."
            )
        return ""

    def refresh_all(self) -> None:
        """Rewrite every venue's subscription, since trials and payments run out with the date."""
        with self.lock:
            for slug in self.venues:
                self.write_subscription(slug)


def create_app(cloud: Cloud) -> Flask:
    app = Flask(__name__, template_folder=str(Path(__file__).parent / "templates"))
    # PreferenceManager, used to set venue passwords, translates its messages.
    Babel(app)
    key_file = cloud.settings.host_data_dir / "owner.key"
    if not key_file.exists():
        key_file.write_text(secrets.token_hex(32))
        key_file.chmod(0o600)
    app.secret_key = key_file.read_text()
    app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True)
    settings = cloud.settings

    @app.before_request
    def require_owner():
        if request.endpoint in ("login", "static") or session.get("owner"):
            return None
        return redirect(url_for("login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            if settings.owner_password and hmac.compare_digest(
                request.form.get("password", ""), settings.owner_password
            ):
                session["owner"] = True
                session.permanent = True
                return redirect(url_for("owner_page"))
            # Slows guessing without locking the owner out.
            time.sleep(1)
            flash("Wrong password.")
        return render_template("login.html", configured=bool(settings.owner_password))

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def owner_page():
        venues = []
        for slug, venue in sorted(
            cloud.venues.items(), key=lambda kv: kv[1]["created_at"], reverse=True
        ):
            plan = PLANS[venue["plan"]]
            access = cloud.access(slug)
            venues.append(
                {
                    "slug": slug,
                    "url": settings.public_url(slug),
                    "plan_name": plan.name,
                    "max_storage_gb": plan.max_storage_gb,
                    "access": access,
                    # A paying venue with no paid-until date was never really set up.
                    "needs_attention": access["mode"] == "warn"
                    or (venue["status"] == "active" and not venue.get("paid_until")),
                    "container": cloud.container_status(slug),
                    **venue,
                    **cloud.stats(slug),
                }
            )
        disk = shutil.disk_usage(settings.host_data_dir)
        return render_template(
            "owner.html",
            venues=venues,
            plans=PLANS,
            base_domain=settings.base_domain,
            disk_free=disk.free,
            disk_total=disk.total,
            fmt_date=fmt_date,
            fmt_size=fmt_size,
            trial_extension_days=TRIAL_EXTENSION_DAYS,
        )

    @app.post("/venues")
    def add_venue():
        try:
            error = cloud.add_venue(request.form)
        except DockerException as e:
            logging.error(f"Could not start the venue: {e}")
            error = f"Saved, but its container did not start: {e}"
        flash(error or "Venue added.", "error" if error else "ok")
        return redirect(url_for("owner_page"))

    @app.post("/venues/<slug>/<action>")
    def venue_action(slug: str, action: str):
        if slug not in cloud.venues:
            error = "No venue with that address."
        else:
            try:
                error = _apply(cloud, slug, action, request.form)
            except DockerException as e:
                logging.error(f"Docker action {action} on {slug} failed: {e}")
                error = f"Docker could not do that: {e}"
        if error:
            flash(error, "error")
        return redirect(url_for("owner_page") + f"#{slug}")

    return app


def _apply(cloud: Cloud, slug: str, action: str, form) -> str:
    """Carry out one owner action. Returns an error message, or "" on success."""
    if action == "paid":
        months = int(form.get("months", 1))
        if not 1 <= months <= 36:
            return "Months: 1 to 36."
        cloud.record_payment(slug, months)
    elif action == "extend-trial":
        cloud.extend_trial(slug)
    elif action == "read-only":
        cloud.update(slug, status="read_only")
    elif action == "turn-on":
        return cloud.turn_back_on(slug)
    elif action == "edit":
        name = form.get("name", "").strip()[:80]
        if not name:
            return "Enter the venue's name."
        if form.get("plan") not in PLANS:
            return "Pick a plan."
        changes = {
            "name": name,
            "plan": form["plan"],
            "contact": form.get("contact", "").strip()[:120],
        }
        for key in ("trial_ends_at", "paid_until"):
            value = form.get(key, "").strip()
            try:
                changes[key] = date.fromisoformat(value).isoformat() if value else None
            except ValueError:
                return "Dates must look like 2026-12-31."
        if cloud.venues[slug]["status"] == "trial" and not changes["trial_ends_at"]:
            return "A trial needs an end date."
        cloud.update(slug, **changes)
    elif action == "admin-password":
        password = form.get("password", "")
        if len(password) < 6:
            return "The admin password needs at least 6 characters."
        cloud.set_admin_password(slug, password)
        # The venue reads config.ini once at startup.
        cloud.restart_container(slug)
    elif action == "restart":
        cloud.ensure_container(slug)
        cloud.restart_container(slug)
    else:
        return "Unknown action."
    return ""


def keep_venues_running(cloud: Cloud) -> None:
    """At startup, bring every venue up on the current image; then keep subscriptions current."""
    for slug in list(cloud.venues):
        try:
            cloud.ensure_container(slug)
        except DockerException as e:
            logging.error(f"Could not start {slug}: {e}")
    while True:
        cloud.refresh_all()
        time.sleep(REFRESH_SECONDS)


def main() -> None:
    from gevent.pywsgi import WSGIServer

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
    settings = Settings.from_env()
    if not settings.owner_password:
        logging.warning("OWNER_PASSWORD is not set: nobody can log in to the owner page.")
    cloud = Cloud(settings)
    threading.Thread(target=keep_venues_running, args=[cloud], daemon=True).start()
    logging.info(f"Owner page: {settings.public_url('owner')}")
    WSGIServer(("0.0.0.0", 5555), create_app(cloud)).serve_forever()


if __name__ == "__main__":
    main()
