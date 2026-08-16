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
# ScraperAPI key for the rockol fetch (residential IP + JS render, to clear
# rockol's Cloudflare). The rockol adapter disables itself when empty.
SCRAPERAPI_KEY: str = os.getenv("SCRAPERAPI_KEY", "")
# Proxy tier for rockol: "" (standard), "premium" (+25 credits w/ render) or
# "ultra_premium" (+75 credits w/ render). Cloudflare usually needs ultra.
ROCKOL_PROXY_TIER: str = os.getenv("ROCKOL_PROXY_TIER", "ultra_premium")
# Hard monthly cap on rockol scraping requests, to stay inside the ScraperAPI
# free tier (1000 credits/month; ultra+render = 75 credits each).
ROCKOL_MONTHLY_BUDGET: int = int(os.getenv("ROCKOL_MONTHLY_BUDGET", 12))
