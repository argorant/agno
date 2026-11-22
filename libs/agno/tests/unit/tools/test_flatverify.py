import json
from contextlib import ExitStack
from typing import Any, Dict, List, Union
from unittest.mock import patch

import httpx
import pytest

from agno.tools.flatverify import FlatverifyTools

TEST_KEY = "fv_test_0123456789abcdef"
BASE = "https://app.flatverify.com/api/v1"
JOB_ID = "8f14e45f-ceea-4e7a-9d3b-2f1c6a1b7e21"
VALID = {
    "email": "maya.chen@amazon.com",
    "status": "valid",
    "sub_status": "accepted",
    "reason": "The address can receive mail.",
    "disposable": False,
    "role": False,
    "free": False,
    "catch_all": False,
}
ROWS = [
    {**VALID, "email": "j.ortiz@salesforce.com"},
    {**VALID, "email": "sam.lee@stripe.com", "status": "invalid", "sub_status": "mailbox_not_found"},
    {**VALID, "email": "info@shopify.com", "status": "risky", "sub_status": "role_account", "role": True},
]

Answer = Union[httpx.Response, Exception]


def job(status: str = "done", **extra: Any) -> Dict[str, Any]:
    body = {
        "id": JOB_ID,
        "name": None,
        "source": "api",
        "status": status,
        "waiting_reason": None,
        "total": 3,
        "processed": 3 if status == "done" else 1,
        "counts": {"valid": 1, "invalid": 1, "risky": 1, "unknown": 0},
        "created_at": "2026-11-22T10:00:00.000Z",
        "finished_at": None,
    }
    body.update(extra)
    return body


def error(status: int, code: str, retry_after: Union[str, None] = None) -> httpx.Response:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return httpx.Response(status, json={"error": {"code": code, "message": f"{code} message"}}, headers=headers)


class FakeAPI:
    """Routes requests to queued answers per (method, path) and records every request."""

    def __init__(self) -> None:
        self.requests: List[httpx.Request] = []
        self.routes: Dict[tuple, List[Answer]] = {
            ("POST", "/verify"): [httpx.Response(200, json=VALID)],
            ("POST", "/verify/bulk"): [httpx.Response(202, json={**job("queued"), "skipped": 1})],
            ("GET", f"/jobs/{JOB_ID}"): [httpx.Response(200, json=job("done"))],
            ("GET", f"/jobs/{JOB_ID}/results"): [httpx.Response(200, json={"results": ROWS, "next_after": None})],
            ("GET", "/account"): [
                httpx.Response(
                    200,
                    json={
                        "plan": "growth",
                        "limits": {"per_minute": 50},
                        "usage": {"today": 12, "this_minute": 0},
                        "free_checks_left": 0,
                    },
                )
            ],
        }

    def set(self, method: str, path: str, *answers: Answer) -> None:
        """Queue answers; the last one repeats."""
        self.routes[(method, path)] = list(answers)

    def calls(self, method: str, path: str) -> List[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == f"/api/v1{path}"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert str(request.url).startswith(BASE)
        answers = self.routes[(request.method, request.url.path[len("/api/v1") :])]
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer.status_code, headers=answer.headers, content=answer.content)


@pytest.fixture
def api():
    fake = FakeAPI()
    transport = httpx.MockTransport(fake.handler)
    real_client, real_async_client = httpx.Client, httpx.AsyncClient

    def client(**kwargs: Any) -> httpx.Client:
        return real_client(transport=transport, **kwargs)

    def async_client(**kwargs: Any) -> httpx.AsyncClient:
        return real_async_client(transport=transport, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(patch("agno.tools.flatverify.httpx.Client", side_effect=client))
        stack.enter_context(patch("agno.tools.flatverify.httpx.AsyncClient", side_effect=async_client))
        fake.sleep = stack.enter_context(patch("agno.tools.flatverify.time.sleep"))  # type: ignore[attr-defined]
        fake.asleep = stack.enter_context(patch("agno.tools.flatverify.asyncio.sleep"))  # type: ignore[attr-defined]
        yield fake


def tools(**kwargs: Any) -> FlatverifyTools:
    return FlatverifyTools(api_key=TEST_KEY, poll_interval=0, **kwargs)


def test_registers_sync_and_async_tools():
    toolkit = tools()
    assert toolkit.name == "flatverify_tools"
    assert set(toolkit.functions) == {"verify_email", "verify_emails", "get_job", "get_account"}
    assert set(toolkit.async_functions) == set(toolkit.functions)
    assert toolkit.timeout == 30.0


def test_disabled_tool_is_not_registered():
    toolkit = tools(enable_get_account=False)
    assert "get_account" not in toolkit.functions
    assert "verify_email" in toolkit.functions


def test_reads_api_key_from_environment():
    with patch.dict("os.environ", {"FLATVERIFY_API_KEY": "fv_env"}):
        assert FlatverifyTools().api_key == "fv_env"


def test_missing_key_returns_error_without_request(api: FakeAPI):
    with patch.dict("os.environ", {}, clear=True):
        toolkit = FlatverifyTools()
    assert "FLATVERIFY_API_KEY" in json.loads(toolkit.verify_email("maya.chen@amazon.com"))["error"]
    assert api.requests == []


def test_key_not_in_repr():
    assert TEST_KEY not in repr(tools())


def test_verify_email_sends_key_and_body(api: FakeAPI):
    out = json.loads(tools().verify_email("maya.chen@amazon.com"))
    assert out == VALID
    request = api.calls("POST", "/verify")[0]
    assert request.headers["Authorization"] == f"Bearer {TEST_KEY}"
    assert json.loads(request.content) == {"email": "maya.chen@amazon.com"}


def test_retries_rate_limited_and_busy(api: FakeAPI):
    api.set(
        "POST",
        "/verify",
        error(429, "rate_limited", "2"),
        error(503, "busy", "1"),
        httpx.Response(200, json=VALID),
    )
    out = json.loads(tools().verify_email("maya.chen@amazon.com"))
    assert out["status"] == "valid"
    assert len(api.calls("POST", "/verify")) == 3
    assert [c.args[0] for c in api.sleep.call_args_list] == [2.0, 1.0]  # type: ignore[attr-defined]


def test_retry_wait_is_capped_and_retries_stop(api: FakeAPI):
    api.set("POST", "/verify", error(503, "busy", "900"))
    out = json.loads(tools(max_retries=2, max_retry_wait=5).verify_email("maya.chen@amazon.com"))
    assert out["status_code"] == 503
    assert out["code"] == "busy"
    assert len(api.calls("POST", "/verify")) == 3
    assert [c.args[0] for c in api.sleep.call_args_list] == [5.0, 5.0]  # type: ignore[attr-defined]


def test_other_errors_are_not_retried(api: FakeAPI):
    api.set("POST", "/verify", error(401, "unauthorized"))
    raw = tools().verify_email("maya.chen@amazon.com")
    assert json.loads(raw)["code"] == "unauthorized"
    assert TEST_KEY not in raw
    assert len(api.calls("POST", "/verify")) == 1


def test_too_many_jobs_is_not_retried(api: FakeAPI):
    api.set("POST", "/verify/bulk", error(429, "too_many_jobs", "0"))
    out = json.loads(tools().verify_emails(["j.ortiz@salesforce.com"]))
    assert out["code"] == "too_many_jobs"
    assert len(api.calls("POST", "/verify/bulk")) == 1


def test_network_error(api: FakeAPI):
    api.set("POST", "/verify", httpx.ConnectError("down"))
    out = json.loads(tools().verify_email("maya.chen@amazon.com"))
    assert "Could not reach" in out["error"]


def test_verify_emails_waits_and_pages(api: FakeAPI):
    api.set("GET", f"/jobs/{JOB_ID}", httpx.Response(200, json=job("running")), httpx.Response(200, json=job("done")))
    api.set(
        "GET",
        f"/jobs/{JOB_ID}/results",
        httpx.Response(200, json={"results": ROWS[:2], "next_after": 1}),
        httpx.Response(200, json={"results": ROWS[2:], "next_after": None}),
    )
    emails = [row["email"] for row in ROWS]
    out = json.loads(tools().verify_emails(emails, name="Event list"))
    assert json.loads(api.calls("POST", "/verify/bulk")[0].content) == {"emails": emails, "name": "Event list"}
    assert out["status"] == "done"
    assert out["skipped"] == 1
    assert out["counts"]["invalid"] == 1
    assert out["results"] == ROWS
    assert out["results_truncated"] is False
    second_page = api.calls("GET", f"/jobs/{JOB_ID}/results")[1].url.params
    assert second_page["after"] == "1"
    assert second_page["format"] == "json"


def test_verify_emails_truncates(api: FakeAPI):
    out = json.loads(tools(max_results=2).verify_emails(["j.ortiz@salesforce.com"]))
    assert len(out["results"]) == 2
    assert out["results_truncated"] is True


def test_verify_emails_returns_job_id_when_wait_ends(api: FakeAPI):
    api.set("GET", f"/jobs/{JOB_ID}", httpx.Response(200, json=job("running")))
    out = json.loads(tools(wait_timeout=0).verify_emails(["j.ortiz@salesforce.com"]))
    assert out["job_id"] == JOB_ID
    assert out["status"] == "running"
    assert "results" not in out
    assert "get_job" in out["message"]
    assert api.calls("GET", f"/jobs/{JOB_ID}/results") == []


def test_get_job(api: FakeAPI):
    toolkit = tools()
    api.set("GET", f"/jobs/{JOB_ID}", httpx.Response(200, json=job("waiting", waiting_reason="rate")))
    assert json.loads(toolkit.get_job(JOB_ID))["waiting_reason"] == "rate"
    api.set("GET", f"/jobs/{JOB_ID}", httpx.Response(200, json=job("done")))
    assert json.loads(toolkit.get_job(JOB_ID))["results"] == ROWS
    api.set("GET", f"/jobs/{JOB_ID}", error(404, "not_found"))
    assert json.loads(toolkit.get_job(JOB_ID))["code"] == "not_found"


def test_get_account(api: FakeAPI):
    out = json.loads(tools().get_account())
    assert out["limits"]["per_minute"] == 50


def test_tool_descriptions_are_plain():
    toolkit = tools()
    for function in [*toolkit.functions.values(), *toolkit.async_functions.values()]:
        text = (function.entrypoint.__doc__ or "").lower()
        assert text
        assert "smtp" not in text
        assert chr(0x2014) not in text
        assert chr(0x2013) not in text


async def test_async_tools(api: FakeAPI):
    toolkit = tools(max_results=1)
    assert json.loads(await toolkit.averify_email("maya.chen@amazon.com"))["status"] == "valid"
    api.set("GET", f"/jobs/{JOB_ID}", httpx.Response(200, json=job("queued")), httpx.Response(200, json=job("done")))
    out = json.loads(await toolkit.averify_emails(["j.ortiz@salesforce.com"], name="Q4 leads"))
    assert out["results"] == ROWS[:1]
    assert out["results_truncated"] is True
    assert json.loads(await toolkit.aget_job(JOB_ID))["status"] == "done"
    assert json.loads(await toolkit.aget_account())["plan"] == "growth"


async def test_async_errors_and_retries(api: FakeAPI):
    toolkit = tools(wait_timeout=0)
    api.set("POST", "/verify", error(503, "busy", "0"), error(402, "payment_required"))
    assert json.loads(await toolkit.averify_email("maya.chen@amazon.com"))["code"] == "payment_required"
    assert len(api.calls("POST", "/verify")) == 2
    api.set("GET", f"/jobs/{JOB_ID}", httpx.Response(200, json=job("running")))
    assert json.loads(await toolkit.averify_emails(["j.ortiz@salesforce.com"]))["status"] == "running"
    assert json.loads(await toolkit.aget_job(JOB_ID))["status"] == "running"
    api.set("POST", "/verify/bulk", error(413, "too_many"))
    assert json.loads(await toolkit.averify_emails(["j.ortiz@salesforce.com"]))["code"] == "too_many"
    api.set("GET", "/account", httpx.ConnectTimeout("slow"))
    assert "Could not reach" in json.loads(await toolkit.aget_account())["error"]
