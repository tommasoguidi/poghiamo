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
