"""Page frame: pinned header, privacy link placement, username, CSS cache-busting."""

from conftest import login, make_user

from poghiamo.webapp.app import asset_version

PRIVACY_LINK = 'href="/privacy"'


def test_privacy_link_only_in_settings_and_signup(client, db):
    assert PRIVACY_LINK in client.get("/signup").text  # GDPR art. 13: where data is collected
    assert PRIVACY_LINK not in client.get("/login").text

    make_user(db)
    login(client, "alice", "testpass123")
    for path in ("/", "/artists", "/calendario"):
        assert PRIVACY_LINK not in client.get(path).text, path
    assert PRIVACY_LINK in client.get("/settings").text
    assert client.get("/privacy").status_code == 200


def test_settings_shows_who_is_logged_in(client, db):
    make_user(db, username="masa")
    login(client, "masa", "testpass123")
    assert 'Collegato come <span class="text-cream font-medium">masa</span>' in client.get("/settings").text


def test_page_titles_live_in_the_pinned_header(client, db):
    make_user(db)
    login(client, "alice", "testpass123")
    for path, title in (("/", "Concerti"), ("/artists", "Artisti che"), ("/calendario", "Calendario")):
        page = client.get(path).text
        header = page[page.index('<header id="app-header"'):page.index("</header>")]
        assert title in header, path


def test_css_url_changes_with_its_content(tmp_path):
    css = tmp_path / "tailwind.css"
    assert asset_version(css) == "dev"  # not built (local dev uses the CDN)
    css.write_text(".a{}")
    first = asset_version(css)
    css.write_text(".a{}.b{}")
    assert len(first) == 12 and asset_version(css) != first
