"""Notion adapter -- pages/databases as tasks + search, via the official
Notion API (integration token)."""

from __future__ import annotations

import logging

from adapters.api import RESTAdapter

logger = logging.getLogger(__name__)


class NotionAdapter(RESTAdapter):
    name = "notion"
    display_name = "Notion"
    description = ("Notes and databases through the Notion API. Create an "
                   "integration at notion.so/my-integrations and paste its "
                   "internal token; Nova can read pages, search and create "
                   "notes.")
    authentication = "api_key"
    config_key = "notion_api_token"
    api_base_url = "https://api.notion.com/v1"
    token_prefix = "Bearer "
    capabilities = [
        "create_task", "list_tasks", "create_reminder", "search_messages",
        "read_messages",
    ]

    NOTION_VERSION = "2022-06-28"

    # ------------------------------------------------------------------ #
    def _http(self):
        from adapters.api import ApiClient
        if self._client is None:
            self._client = ApiClient(self.api_base_url, token=self._api_key())
        self._client.token = self._api_key()
        return self._client

    def _verify_connection(self):
        data = self._http().post("/search", json_body={"page_size": 1})
        if not isinstance(data, dict):
            raise _NotionError()

    def status(self) -> dict:
        if not self.is_configured():
            return {"status": "not_configured",
                    "message": "Notion needs an integration token to connect."}
        return {"status": "requires_auth",
                "message": "Notion integration token configured. Click Connect to verify."}

    # ------------------------------------------------------------------ #
    def _db(self, client) -> str | None:
        data = client.post("/search", json_body={"filter": {"property": "object",
                                                            "value": "database"},
                                                 "page_size": 5})
        items = data.get("results") or []
        return items[0]["id"] if items else None

    def create_task(self, title, due=None, **kwargs):
        client = self._http()
        db = self._db(client)
        if not db:
            return self._fail("Notion needs a database to store tasks.")
        body = {"parent": {"database_id": db},
                "properties": {"Name": {"title": [{"text": {"content": title}}]}}}
        if due:
            body["properties"]["Due"] = {"date": {"start": due}}
        page = client.post("/pages", json_body=body)
        return self._ok(id=page.get("id"), title=title, due=due)

    def list_tasks(self, limit=50, **kwargs):
        client = self._http()
        db = self._db(client)
        if not db:
            return self._fail("Notion needs a database to list tasks.")
        data = client.post(f"/databases/{db}/query",
                           json_body={"page_size": min(int(limit), 100)})
        tasks = []
        for page in data.get("results") or []:
            props = page.get("properties") or {}
            title = _title_of(props.get("Name") or props.get("name"))
            due = _date_of(props.get("Due") or props.get("due"))
            tasks.append({"id": page.get("id"), "title": title, "due": due,
                          "completed": False})
        return self._ok(tasks=tasks, count=len(tasks))

    def create_reminder(self, text, when=None, **kwargs):
        return self.create_task(title=text, due=when)

    def search_messages(self, query, limit=20, **kwargs):
        client = self._http()
        data = client.post("/search", json_body={"query": query,
                                                 "page_size": min(int(limit), 100)})
        results = [{"id": r.get("id"),
                    "content": _title_of(r.get("properties") or {}) or "Untitled",
                    "type": r.get("object")} for r in data.get("results") or []]
        return self._ok(messages=results, count=len(results))

    def read_messages(self, limit=20, **kwargs):
        return self.search_messages(query="", limit=limit)


def _title_of(props: dict) -> str:
    if not isinstance(props, dict):
        return ""
    for value in props.values():
        if isinstance(value, dict) and isinstance(value.get("title"), list):
            parts = [t.get("plain_text", "") for t in value["title"]]
            return "".join(parts)
    return ""


def _date_of(prop) -> str:
    if not isinstance(prop, dict):
        return ""
    return (prop.get("date") or {}).get("start") or ""


class _NotionError(Exception):
    pass