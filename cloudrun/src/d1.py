import httpx


class D1Result:
    def __init__(self, results: list[dict]):
        self.results = results


class D1:
    """Cloudflare D1 accessed via its REST API (not a Workers binding - this runs on Cloud
    Run, a plain container, not inside a Worker). Same database itineraryManager's Cloudflare
    Workers deployment already uses (worker/wrangler.jsonc) - only the access method changes."""

    def __init__(self, account_id: str, database_id: str, api_token: str):
        self._url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{database_id}/query"
        self._headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
        # Lazy client creation: this object is built once at import time (config.get_env),
        # before there's necessarily an event loop running.
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=15.0))
        return self._client

    async def query(self, sql: str, params: list | None = None) -> D1Result:
        resp = await self._get_client().post(self._url, headers=self._headers, json={"sql": sql, "params": params or []})
        data = resp.json()
        # ponytail: check both layers - a transport-level failure (bad token, D1 down) shows up
        # as a non-2xx HTTP status; a query-level failure (bad SQL, constraint violation) can
        # come back as HTTP 200 with success:false in the body. Callers (e.g. trips.start_trip
        # catching the unique-active-trip-per-group constraint) rely on either one raising.
        if resp.status_code >= 400 or not data.get("success", True):
            raise RuntimeError(f"D1 query failed ({resp.status_code}): {data.get('errors')}")
        result = data["result"][0]
        return D1Result(result.get("results", []))
