"""Centralized configuration — all environment variables in one place."""

import os

from dotenv import load_dotenv

load_dotenv()

# Database
DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./poghiamo.db")

# Webapp
WEBAPP_URL: str = os.getenv("WEBAPP_URL", "http://localhost:8000")

# Auth
SECRET_KEY: str = os.getenv("SECRET_KEY", "CHANGE-ME-IN-PRODUCTION")
# Secure cookie: disable only for local development over plain http.
SESSION_HTTPS_ONLY: bool = os.getenv("SESSION_HTTPS_ONLY", "true").lower() == "true"

# Admin seeding (optional — set to bootstrap an admin account on startup)
ADMIN_USERNAME: str = os.getenv("ADMIN_USERNAME", "")
ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "")

# Backups
BACKUP_DIR: str = os.getenv("BACKUP_DIR", "./backups")
BACKUP_KEEP: int = int(os.getenv("BACKUP_KEEP", 14))

# Events pipeline
SCAN_INTERVAL_DAYS: int = int(os.getenv("SCAN_INTERVAL_DAYS", 7))
# Per-source global timeout (seconds) inside a single artist scan.
SOURCE_TIMEOUT_SECONDS: int = int(os.getenv("SOURCE_TIMEOUT_SECONDS", 25))
# How many artists to scan per scheduler tick (spacing to be polite to sources).
SCAN_BATCH_SIZE: int = int(os.getenv("SCAN_BATCH_SIZE", 20))
# Future events unseen for this many days get flagged "da ricontrollare".
EVENT_STALE_DAYS: int = int(os.getenv("EVENT_STALE_DAYS", 7))
# Ticketmaster Discovery API key; the adapter disables itself when empty.
TICKETMASTER_API_KEY: str = os.getenv("TICKETMASTER_API_KEY", "")
# Forward proxy on the always-on home PC, reached over the WireGuard tunnel
# (e.g. http://10.77.0.2:8888). When set, rockol is fetched through it with a
# plain GET, so the request exits from the residential line, and ZenRows becomes
# the fallback. Empty = no home path.
ROCKOL_PROXY_URL: str = os.getenv("ROCKOL_PROXY_URL", "").strip()
# Hours to keep the home line away from rockol after rockol blocks it.
ROCKOL_HOME_COOLDOWN_HOURS: float = float(os.getenv("ROCKOL_HOME_COOLDOWN_HOURS", 6))
# ZenRows key for the rockol fetch (rented residential proxy + JS render, which
# clears rockol's Cloudflare challenge). Used alone, or as the fallback when the
# home path is down or blocked. The rockol adapter is off when both this and
# ROCKOL_PROXY_URL are empty.
ZENROWS_API_KEY: str = os.getenv("ZENROWS_API_KEY", "")
# Hard monthly cap on ZenRows-served rockol fetches, to stay inside the free
# tier (5000 credits/month; js_render+premium_proxy = 25 credits each → 200/mo).
# Fetches served by the home path do not count.
ROCKOL_MONTHLY_BUDGET: int = int(os.getenv("ROCKOL_MONTHLY_BUDGET", 180))
