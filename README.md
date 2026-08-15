# poghiamo

Personal, invite-only web service that tracks concerts in Italy for the niche and
indie artists you actually listen to. Users follow artists, a nightly job sweeps
Italian concert sources per artist, and the feed filters results by each user's
regions of interest.

Built with FastAPI, Jinja2, Tailwind and SQLite, deployed with docker compose
behind a shared Traefik edge. Sibling of [lesgoski](https://github.com/tommasoguidi/lesgoski),
whose architecture it mirrors.

## Development

```bash
uv sync                                   # create .venv and install deps
cp .env.example .env                      # then edit: SECRET_KEY, admin creds,
                                          # and SESSION_HTTPS_ONLY=false for http dev
uv run poghiamo-web                       # http://127.0.0.1:8000, auto-reload
npm install && npm run watch:css          # optional: real Tailwind instead of the CDN fallback
uv run pytest                             # tests
```

## Deployment

GitHub is the source of truth; the server holds a clone. TLS, HTTP→HTTPS
redirect, security headers and auth rate limiting are handled by the shared
Traefik edge via the labels in `docker-compose.yml`.

First deploy on the server:

1. `cp .env.example .env` in the clone, then set: a generated `SECRET_KEY`,
   `ADMIN_USERNAME`/`ADMIN_PASSWORD`, `WEBAPP_URL=https://poghiamo.quest`,
   and keep `SESSION_HTTPS_ONLY=true`.
2. `docker compose up -d --build`, then verify locally:
   `curl -s http://127.0.0.1:8001/login` (the app also wins the public Host
   rule immediately thanks to its explicit router priority).
3. Retire the edge's temporary placeholder: delete the `placeholder` service
   from the edge project's `docker-compose.yml` and run
   `docker compose up -d --remove-orphans` there.

Updates: commit + push locally, `git pull` on the server,
`docker compose up -d --build`.

Nightly gzip'd SQLite backups land in the `backup-data` volume with two weeks
of retention (plus a catch-up backup at scheduler start if today's is missing).
They live on the same disk as the database: they protect against application
mistakes, not against losing the server. Copy them elsewhere periodically if
the data ever becomes hard to rebuild.
