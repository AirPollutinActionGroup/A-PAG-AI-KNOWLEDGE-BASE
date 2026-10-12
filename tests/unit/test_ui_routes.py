"""Which page each address serves.

`/` used to be the Studio page, so anyone typing the bare address opened an outdated developer
tool and concluded the application was old. Pinned so it does not drift back.
"""

from starlette.testclient import TestClient

from src.api.v1.router import app

client = TestClient(app)


def _title(path: str) -> str:
    html = client.get(path).text
    return html.split("<title>", 1)[1].split("</title>", 1)[0]


def test_the_bare_address_opens_the_knowledge_base():
    assert _title("/") == "A-PAG Knowledge Base"


def test_search_still_opens_it_for_existing_bookmarks():
    assert _title("/search") == _title("/")


def test_the_studio_page_moved_rather_than_vanished():
    assert "Ingestion Pipeline Studio" in _title("/studio")
