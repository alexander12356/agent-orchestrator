import httpx

from app import pricing


def test_model_catalog_is_loaded_once_and_preserved_when_refresh_fails(client, monkeypatch):
    downloaded = []

    def download(url, **kwargs):
        request = httpx.Request("GET", url)
        downloaded.append(request)
        if len(downloaded) > 1:
            return httpx.Response(503, request=request)
        return httpx.Response(200, request=request, json={
            "test": {"mode": "chat", "input_cost_per_token": 0.000001, "output_cost_per_token": 0.000002},
        })

    monkeypatch.setattr(pricing.httpx, "get", download)
    initial = pricing.refresh(initial=True)
    assert initial["model_count"] == 1
    assert pricing.refresh(initial=True) == initial
    assert client.get("/api/model-prices").json() == initial
    assert len(downloaded) == 1
    assert client.post("/api/model-prices/refresh").status_code == 502
    assert len(downloaded) == 2
    assert client.get("/api/model-prices").json() == initial
