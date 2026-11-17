from unittest.mock import patch, MagicMock
import pytest


@pytest.mark.unit
def test_root_endpoint(client):
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "AI-Powered Document Intelligence System"
    assert data["docs"] == "/docs"
    assert data["health"] == "/api/v1/health"


@pytest.mark.unit
def test_health_check_healthy(client, mock_db):
    mock_db.execute.return_value = True

    with patch("redis.Redis.from_url") as mock_redis_cls, \
         patch("shutil.which", return_value="/usr/bin/tesseract"), \
         patch("subprocess.run") as mock_subproc:

        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis_cls.return_value = mock_redis

        mock_subproc.return_value = MagicMock(
            stdout="tesseract 5.3.0\n leptonica-1.82.0\n",
            returncode=0,
        )

        response = client.get("/api/v1/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["database"] == "connected"
        assert data["redis"] == "connected"
        assert "tesseract 5.3.0" in data["tesseract"]
        assert "llm" in data
