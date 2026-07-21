from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_ok():
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"] == {"app": "ok", "database": "ok"}


def test_health_sets_and_echoes_correlation_id():
    generated = client.get("/health")
    assert generated.headers.get("X-Request-ID")

    provided = "trace-abc-123"
    echoed = client.get("/health", headers={"X-Request-ID": provided})
    assert echoed.headers.get("X-Request-ID") == provided
