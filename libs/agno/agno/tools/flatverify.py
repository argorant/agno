import asyncio
import json
import time
from os import getenv
from typing import Any, Dict, List, Optional

import httpx

from agno.tools import Toolkit
from agno.utils.log import log_debug, log_error

DONE = "done"


class FlatverifyTools(Toolkit):
    """Check whether email addresses can receive mail with Flatverify.

    Set FLATVERIFY_API_KEY or pass api_key. Create keys in the Flatverify app
    (https://app.flatverify.com) under API keys.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = "https://app.flatverify.com/api/v1",
        timeout: float = 30.0,
        max_retries: int = 3,
        max_retry_wait: float = 30.0,
        wait_timeout: float = 300.0,
        poll_interval: float = 3.0,
        max_results: int = 1000,
        enable_verify_email: bool = True,
        enable_verify_emails: bool = True,
        enable_get_job: bool = True,
        enable_get_account: bool = True,
        all: bool = False,
        **kwargs: Any,
    ):
        """Initialize the Flatverify toolkit.

        Args:
            api_key: Flatverify API key. Uses ``FLATVERIFY_API_KEY`` when omitted.
            base_url: Flatverify API base URL.
            timeout: Per-request timeout in seconds.
            max_retries: Retries for rate limited (429) and busy (503) answers.
            max_retry_wait: Longest wait in seconds between two retries.
            wait_timeout: Seconds verify_emails waits for a job before returning its job_id.
            poll_interval: Seconds between two job status checks.
            max_results: Most result rows returned to the agent in one call.
            enable_verify_email: Register the single address check.
            enable_verify_emails: Register the list check.
            enable_get_job: Register the job status and results tool.
            enable_get_account: Register the account tool.
            all: Register all tools regardless of individual flags.
        """
        self.api_key = api_key or getenv("FLATVERIFY_API_KEY")
        if not self.api_key:
            log_error("FLATVERIFY_API_KEY not set. Please set the FLATVERIFY_API_KEY environment variable.")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_retry_wait = max_retry_wait
        self.wait_timeout = wait_timeout
        self.poll_interval = poll_interval
        self.max_results = max_results

        tools: List[Any] = []
        async_tools: List[tuple] = []
        if all or enable_verify_email:
            tools.append(self.verify_email)
            async_tools.append((self.averify_email, "verify_email"))
        if all or enable_verify_emails:
            tools.append(self.verify_emails)
            async_tools.append((self.averify_emails, "verify_emails"))
        if all or enable_get_job:
            tools.append(self.get_job)
            async_tools.append((self.aget_job, "get_job"))
        if all or enable_get_account:
            tools.append(self.get_account)
            async_tools.append((self.aget_account, "get_account"))

        name = kwargs.pop("name", "flatverify_tools")
        super().__init__(name=name, tools=tools, async_tools=async_tools, **kwargs)

    def __repr__(self) -> str:
        return f"FlatverifyTools(base_url={self.base_url!r})"

    # HTTP helpers

    def _headers(self) -> Dict[str, str]:
        if not self.api_key:
            raise FlatverifyError("Flatverify API key is required. Set FLATVERIFY_API_KEY or pass api_key.")
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": "agno-flatverify-tools",
        }

    def _retry_wait(self, response: httpx.Response, attempt: int) -> Optional[float]:
        """Seconds to wait before retrying, or None when the answer is final."""
        if response.status_code == 429 and _error_code(response) == "too_many_jobs":
            return None
        if response.status_code not in (429, 503) or attempt >= self.max_retries:
            return None
        try:
            wait = float(response.headers.get("Retry-After", 2**attempt))
        except ValueError:
            wait = float(2**attempt)
        return max(0.0, min(wait, self.max_retry_wait))

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = self._headers()
        attempt = 0
        with httpx.Client(timeout=self.timeout) as client:
            while True:
                try:
                    response = client.request(method, f"{self.base_url}{path}", headers=headers, **kwargs)
                except httpx.RequestError as error:
                    raise FlatverifyError(f"Could not reach the Flatverify API: {type(error).__name__}") from None
                if response.is_success:
                    return response.json()
                wait = self._retry_wait(response, attempt)
                if wait is None:
                    raise FlatverifyError.from_response(response)
                log_debug(f"Flatverify answered {response.status_code}, retrying in {wait}s")
                time.sleep(wait)
                attempt += 1

    async def _arequest(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = self._headers()
        attempt = 0
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            while True:
                try:
                    response = await client.request(method, f"{self.base_url}{path}", headers=headers, **kwargs)
                except httpx.RequestError as error:
                    raise FlatverifyError(f"Could not reach the Flatverify API: {type(error).__name__}") from None
                if response.is_success:
                    return response.json()
                wait = self._retry_wait(response, attempt)
                if wait is None:
                    raise FlatverifyError.from_response(response)
                log_debug(f"Flatverify answered {response.status_code}, retrying in {wait}s")
                await asyncio.sleep(wait)
                attempt += 1

    def _collect(self, job_id: str) -> tuple:
        rows: List[Dict[str, Any]] = []
        params: Dict[str, Any] = {"format": "json"}
        while True:
            page = self._request("GET", f"/jobs/{job_id}/results", params=params)
            for row in page.get("results") or []:
                if len(rows) >= self.max_results:
                    return rows, True
                rows.append(row)
            if page.get("next_after") is None:
                return rows, False
            params = {"format": "json", "after": page["next_after"]}

    async def _acollect(self, job_id: str) -> tuple:
        rows: List[Dict[str, Any]] = []
        params: Dict[str, Any] = {"format": "json"}
        while True:
            page = await self._arequest("GET", f"/jobs/{job_id}/results", params=params)
            for row in page.get("results") or []:
                if len(rows) >= self.max_results:
                    return rows, True
                rows.append(row)
            if page.get("next_after") is None:
                return rows, False
            params = {"format": "json", "after": page["next_after"]}

    def _summary(
        self,
        job: Dict[str, Any],
        rows: Optional[List[Dict[str, Any]]] = None,
        truncated: bool = False,
        skipped: Optional[int] = None,
    ) -> str:
        out: Dict[str, Any] = {
            "job_id": job.get("id"),
            "status": job.get("status"),
            "total": job.get("total"),
            "processed": job.get("processed"),
            "counts": job.get("counts"),
        }
        if skipped is not None:
            out["skipped"] = skipped
        if job.get("status") == DONE and rows is not None:
            out["results"] = rows
            out["results_truncated"] = truncated
        else:
            if job.get("waiting_reason"):
                out["waiting_reason"] = job["waiting_reason"]
            out["message"] = "The job is not finished yet. Call get_job with this job_id later to get the results."
        return json.dumps(out)

    # Tools

    def verify_email(self, email: str) -> str:
        """Check whether one email address can receive mail.

        Returns status (valid, invalid, risky or unknown), sub_status with the reason
        (for example accepted, mailbox_not_found, catch_all, role_account or disposable)
        and true or false flags for disposable, role, free and catch_all addresses.

        Args:
            email (str): The email address to check, for example maya.chen@amazon.com.

        Returns:
            str: JSON with email, status, sub_status, reason, disposable, role, free and catch_all.
        """
        try:
            return json.dumps(self._request("POST", "/verify", json={"email": email}))
        except FlatverifyError as error:
            return error.to_json()

    async def averify_email(self, email: str) -> str:
        """Check whether one email address can receive mail.

        Returns status (valid, invalid, risky or unknown), sub_status with the reason
        (for example accepted, mailbox_not_found, catch_all, role_account or disposable)
        and true or false flags for disposable, role, free and catch_all addresses.

        Args:
            email (str): The email address to check, for example maya.chen@amazon.com.

        Returns:
            str: JSON with email, status, sub_status, reason, disposable, role, free and catch_all.
        """
        try:
            return json.dumps(await self._arequest("POST", "/verify", json={"email": email}))
        except FlatverifyError as error:
            return error.to_json()

    def verify_emails(self, emails: List[str], name: Optional[str] = None) -> str:
        """Check a list of email addresses as one job and wait for the results.

        Use this instead of calling verify_email many times. Duplicates and malformed
        entries are skipped. If the job is not finished within the wait time, returns
        the job_id; call get_job with it later.

        Args:
            emails (List[str]): The email addresses to check, for example
                ["j.ortiz@salesforce.com", "sam.lee@stripe.com"].
            name (Optional[str]): Optional label for the job, for example the list name.

        Returns:
            str: JSON with job_id, status, counts per status, skipped, and results with
                one row per address (email, status, sub_status, flags).
        """
        body: Dict[str, Any] = {"emails": list(emails)}
        if name:
            body["name"] = name
        try:
            started = self._request("POST", "/verify/bulk", json=body)
            job_id = started["id"]
            deadline = time.monotonic() + self.wait_timeout
            job = self._request("GET", f"/jobs/{job_id}")
            while job.get("status") != DONE and time.monotonic() < deadline:
                time.sleep(self.poll_interval)
                job = self._request("GET", f"/jobs/{job_id}")
            if job.get("status") != DONE:
                return self._summary(job, skipped=started.get("skipped"))
            rows, truncated = self._collect(job_id)
            return self._summary(job, rows, truncated, started.get("skipped"))
        except FlatverifyError as error:
            return error.to_json()

    async def averify_emails(self, emails: List[str], name: Optional[str] = None) -> str:
        """Check a list of email addresses as one job and wait for the results.

        Use this instead of calling verify_email many times. Duplicates and malformed
        entries are skipped. If the job is not finished within the wait time, returns
        the job_id; call get_job with it later.

        Args:
            emails (List[str]): The email addresses to check, for example
                ["j.ortiz@salesforce.com", "sam.lee@stripe.com"].
            name (Optional[str]): Optional label for the job, for example the list name.

        Returns:
            str: JSON with job_id, status, counts per status, skipped, and results with
                one row per address (email, status, sub_status, flags).
        """
        body: Dict[str, Any] = {"emails": list(emails)}
        if name:
            body["name"] = name
        try:
            started = await self._arequest("POST", "/verify/bulk", json=body)
            job_id = started["id"]
            deadline = time.monotonic() + self.wait_timeout
            job = await self._arequest("GET", f"/jobs/{job_id}")
            while job.get("status") != DONE and time.monotonic() < deadline:
                await asyncio.sleep(self.poll_interval)
                job = await self._arequest("GET", f"/jobs/{job_id}")
            if job.get("status") != DONE:
                return self._summary(job, skipped=started.get("skipped"))
            rows, truncated = await self._acollect(job_id)
            return self._summary(job, rows, truncated, started.get("skipped"))
        except FlatverifyError as error:
            return error.to_json()

    def get_job(self, job_id: str) -> str:
        """Get the status of an email list check by its job_id, with the results once it is done.

        Args:
            job_id (str): The job_id returned by verify_emails.

        Returns:
            str: JSON with job_id, status, counts per status and, when done, one row per address.
        """
        try:
            job = self._request("GET", f"/jobs/{job_id}")
            if job.get("status") != DONE:
                return self._summary(job)
            rows, truncated = self._collect(job_id)
            return self._summary(job, rows, truncated)
        except FlatverifyError as error:
            return error.to_json()

    async def aget_job(self, job_id: str) -> str:
        """Get the status of an email list check by its job_id, with the results once it is done.

        Args:
            job_id (str): The job_id returned by verify_emails.

        Returns:
            str: JSON with job_id, status, counts per status and, when done, one row per address.
        """
        try:
            job = await self._arequest("GET", f"/jobs/{job_id}")
            if job.get("status") != DONE:
                return self._summary(job)
            rows, truncated = await self._acollect(job_id)
            return self._summary(job, rows, truncated)
        except FlatverifyError as error:
            return error.to_json()

    def get_account(self) -> str:
        """Get the Flatverify plan, its speed limit in checks per minute and the free checks left.

        Returns:
            str: JSON with plan, limits, usage and free_checks_left.
        """
        try:
            return json.dumps(self._request("GET", "/account"))
        except FlatverifyError as error:
            return error.to_json()

    async def aget_account(self) -> str:
        """Get the Flatverify plan, its speed limit in checks per minute and the free checks left.

        Returns:
            str: JSON with plan, limits, usage and free_checks_left.
        """
        try:
            return json.dumps(await self._arequest("GET", "/account"))
        except FlatverifyError as error:
            return error.to_json()


class FlatverifyError(Exception):
    """An error answer from the Flatverify API, or no answer at all."""

    def __init__(self, message: str, status_code: Optional[int] = None, code: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code

    @classmethod
    def from_response(cls, response: httpx.Response) -> "FlatverifyError":
        message = response.reason_phrase or "Request failed"
        try:
            error = response.json().get("error")
            if isinstance(error, dict) and error.get("message"):
                message = str(error["message"])
        except (ValueError, AttributeError):
            pass
        return cls(message, status_code=response.status_code, code=_error_code(response))

    def to_json(self) -> str:
        out: Dict[str, Any] = {"error": self.message}
        if self.status_code is not None:
            out["status_code"] = self.status_code
            out["code"] = self.code
        return json.dumps(out)


def _error_code(response: httpx.Response) -> str:
    try:
        error = response.json().get("error")
        if isinstance(error, dict) and error.get("code"):
            return str(error["code"])
    except (ValueError, AttributeError):
        pass
    return "error"
