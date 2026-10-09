import json

import pytest

from scraper import source_fast_paths as paths


class _Response:
    def __init__(self, payload, status_code=200):
        self.text = json.dumps(payload)
        self.status_code = status_code
        self.headers = {}
        self.url = ""

    async def aiter_content(self):
        yield self.text.encode("utf-8")

    async def aclose(self):
        return None


class _FakeSession:
    payloads = []
    requested = []

    def __init__(self, *args, **kwargs):
        self._payloads = iter(type(self).payloads)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, method, url, **kwargs):
        assert method == "GET"
        type(self).requested.append(url)
        return _Response(next(self._payloads))


@pytest.mark.asyncio
async def test_remotive_bulk_preserves_job_and_category_identity(monkeypatch):
    long_description = "Remote role details " * 30
    feed = {
        "jobs": [
            {
                "title": "Backend Engineer",
                "company_name": "Example One",
                "job_type": "full_time",
                "category": "Software Development",
                "candidate_required_location": "Worldwide",
                "url": "https://remotive.com/remote-jobs/software-dev/backend-engineer-12345",
                "description": long_description,
            },
            {
                "title": "Account Executive",
                "company_name": "Example Two",
                "job_type": "full_time",
                "category": "Sales",
                "candidate_required_location": "Americas",
                "url": "https://remotive.com/remote-jobs/sales/account-executive-98765",
                "description": long_description,
            },
        ]
    }
    _FakeSession.payloads = [feed, feed]
    _FakeSession.requested = []
    monkeypatch.setattr(paths, "AsyncSession", _FakeSession)

    results = await paths.scrape_remotive_bulk([
        "https://remotive.com/remote-jobs/software-dev/backend-engineer-12345",
        "https://remotive.com/remote-jobs/sales",
    ])

    assert _FakeSession.requested == [
        "https://remotive.com/api/remote-jobs",
        "https://remotive.com/api/remote-jobs",
    ]
    assert all(result.success for result in results)
    assert "Backend Engineer" in results[0].html
    assert "Account Executive" not in results[0].html
    assert "Account Executive" in results[1].html
    assert "Backend Engineer" not in results[1].html


@pytest.mark.asyncio
async def test_clinicaltrials_exact_id_endpoint_and_mismatch_rejection(monkeypatch):
    def study(nct_id):
        return {
            "protocolSection": {
                "identificationModule": {
                    "nctId": nct_id,
                    "briefTitle": "A sufficiently descriptive clinical study title",
                },
                "statusModule": {"overallStatus": "RECRUITING"},
                "conditionsModule": {"conditions": ["Example condition"]},
                "descriptionModule": {"briefSummary": "Study summary " * 30},
            }
        }

    _FakeSession.payloads = [study("NCT12345678"), study("NCT00000000")]
    _FakeSession.requested = []
    monkeypatch.setattr(paths, "AsyncSession", _FakeSession)

    results = await paths.scrape_clinicaltrials_bulk([
        "https://clinicaltrials.gov/study/NCT12345678",
        "https://clinicaltrials.gov/study/NCT87654321",
    ])

    assert _FakeSession.requested == [
        "https://clinicaltrials.gov/api/v2/studies/NCT12345678",
        "https://clinicaltrials.gov/api/v2/studies/NCT87654321",
    ]
    assert results[0].success
    assert "NCT12345678" in results[0].html
    assert not results[1].success
    assert "non-matching" in results[1].reason
