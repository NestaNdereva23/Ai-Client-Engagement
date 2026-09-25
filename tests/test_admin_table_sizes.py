from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.admin.table_sizes import TableSize, load_table_sizes
from app.db.models.models import Clients
from app.main import app

FAKE_SIZES = [
    TableSize(name="raw_staging", approx_rows=1200, table_mb=48.5, index_mb=1.25, total_mb=49.75),
    TableSize(name="clients", approx_rows=30, table_mb=0.02, index_mb=0.05, total_mb=0.07),
    TableSize(name="agent_run", approx_rows=None, table_mb=0.01, index_mb=0.02, total_mb=0.03),
]


def _logged_in_client(monkeypatch) -> TestClient:
    monkeypatch.setattr(
        "app.admin.auth.get_settings",
        lambda: SimpleNamespace(admin_username="tester", admin_password="secret"),
    )
    client = TestClient(app, follow_redirects=False)
    response = client.post("/admin/login", data={"username": "tester", "password": "secret"})
    assert response.status_code == 302
    return client


def test_table_sizes_page_requires_login():
    client = TestClient(app, follow_redirects=False)

    response = client.get("/admin/table-sizes")

    assert response.status_code == 302
    assert response.headers["location"].endswith("/admin/login")


def test_table_sizes_page_lists_each_table_in_mb(monkeypatch):
    monkeypatch.setattr("app.admin.table_sizes.load_table_sizes", lambda: FAKE_SIZES)
    client = _logged_in_client(monkeypatch)

    response = client.get("/admin/table-sizes")

    assert response.status_code == 200
    assert "raw_staging" in response.text
    assert "48.50" in response.text
    assert "49.75" in response.text
    assert "49.85" in response.text
    assert "n/a" in response.text


def test_table_sizes_appear_in_the_sidebar(monkeypatch):
    monkeypatch.setattr("app.admin.table_sizes.load_table_sizes", lambda: FAKE_SIZES)
    client = _logged_in_client(monkeypatch)

    response = client.get("/admin/table-sizes")

    assert 'href="http://testserver/admin/table-sizes"' in response.text


def test_load_table_sizes_lists_real_tables_largest_first(db):
    sizes = load_table_sizes()

    assert Clients.__tablename__ in [size.name for size in sizes]
    totals = [size.total_mb for size in sizes]
    assert totals == sorted(totals, reverse=True)
    assert all(size.table_mb >= 0 and size.index_mb >= 0 for size in sizes)
