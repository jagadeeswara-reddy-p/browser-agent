"""Thin async client for TypeSafe's Jev /v1/systemone endpoint."""
import os
import time
import httpx

BASE_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"


class JevError(Exception):
    pass


class JevClient:
    def __init__(self, api_key: str | None = None, model: str = MODEL, timeout: float = 15.0):
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY")
        if not self.api_key:
            raise JevError("TYPESAFE_API_KEY not set")
        self.model = model
        self._client = httpx.AsyncClient(timeout=timeout)
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_calls = 0

    async def close(self):
        await self._client.aclose()

    async def ask(self, state, questions: dict, retries: int = 3):
        """POST /v1/systemone. `questions` is the raw {id: {type, instructions, criteria}} map.

        Returns dict with: answers, usage, latency_ms, raw_request, raw_response.
        """
        payload = {"state": state, "model": self.model, "questions": questions}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        last_err = None
        for attempt in range(retries):
            t0 = time.perf_counter()
            try:
                resp = await self._client.post(BASE_URL, json=payload, headers=headers)
            except httpx.HTTPError as e:
                last_err = e
                continue
            latency_ms = (time.perf_counter() - t0) * 1000
            if resp.status_code in (429, 529):
                last_err = JevError(f"HTTP {resp.status_code}: {resp.text}")
                continue
            if resp.status_code != 200:
                raise JevError(f"HTTP {resp.status_code}: {resp.text}")
            data = resp.json()
            usage = data.get("usage", {})
            self.total_input_tokens += usage.get("input_tokens", 0)
            self.total_output_tokens += usage.get("output_tokens", 0)
            self.total_calls += 1
            return {
                "answers": data.get("answers", {}),
                "usage": usage,
                "latency_ms": round(latency_ms, 1),
                "raw_request": payload,
                "raw_response": data,
            }
        raise JevError(f"Jev request failed after {retries} attempts: {last_err}")

    def cost_estimate_usd(self) -> float:
        # $42 per 1,000,000,000 input tokens; output tokens are free.
        return self.total_input_tokens * (42.0 / 1_000_000_000)
