from fastapi.testclient import TestClient

from app.analyzer import build_heuristic_report, parse_repository_url
from app.main import app


client = TestClient(app)


def test_parse_repository_url():
    assert parse_repository_url("https://github.com/owner/repo") == ("owner", "repo")
    assert parse_repository_url("https://github.com/owner/repo.git") == ("owner", "repo")


def test_sample_opportunities_are_valid():
    response = client.get("/api/opportunities")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 2
    assert payload[0]["is_sample"] is True
    assert len(payload[0]["scores"]) == 4


def test_license_is_a_gate():
    report = build_heuristic_report(
        {
            "full_name": "owner/no-license",
            "html_url": "https://github.com/owner/no-license",
            "description": "Self-hosted workflow tool",
            "language": "Python",
            "stargazers_count": 120,
            "updated_at": "2026-08-01T00:00:00Z",
            "topics": ["self-hosted"],
            "license": None,
        }
    )
    assert report.license.status == "red"
    assert report.verdict == "watch"


def test_health():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
