"""Todoist adapter -- tasks via the official Todoist REST API (token)."""

from __future__ import annotations

import logging

from adapters.api import RESTAdapter

logger = logging.getLogger(__name__)


class TodoistAdapter(RESTAdapter):
    name = "todoist"
    display_name = "Todoist"
    description = ("Your task list through the Todoist REST API. Paste your "
                   "API token (Settings > Integrations > Developer); Nova can "
                   "create tasks, list them and mark them done.")
    authentication = "api_key"
    config_key = "todoist_api_token"
    api_base_url = "https://api.todoist.com/rest/v2"
    capabilities = ["create_task", "update_task", "list_tasks", "create_reminder"]

    # ------------------------------------------------------------------ #
    def _verify_connection(self):
        self._http().get("/tasks", params={"limit": "1"})

    def status(self) -> dict:
        if not self.is_configured():
            return {"status": "not_configured",
                    "message": "Todoist needs an API token to connect."}
        return {"status": "requires_auth",
                "message": "Todoist token configured. Click Connect to verify."}

    # ------------------------------------------------------------------ #
    def create_task(self, title, due=None, **kwargs):
        client = self._http()
        body = {"content": title}
        if due:
            body["due_string"] = due
        task = client.post("/tasks", json_body=body)
        return self._ok(id=task.get("id"), title=title, due=due)

    def update_task(self, task_id, **kwargs):
        client = self._http()
        body = {k: v for k, v in kwargs.items() if k in ("content", "due_string", "due_date")}
        task = client.post(f"/tasks/{task_id}", json_body=body)
        return self._ok(id=task.get("id"), updated=True)

    def list_tasks(self, limit=50, **kwargs):
        client = self._http()
        data = client.get("/tasks", params={"limit": str(min(int(limit), 200))})
        tasks = [{"id": t.get("id"), "title": t.get("content"),
                  "completed": False, "due": (t.get("due") or {}).get("string")}
                 for t in data if isinstance(t, dict)]
        return self._ok(tasks=tasks, count=len(tasks))

    def create_reminder(self, text, when=None, **kwargs):
        return self.create_task(title=text, due=when)