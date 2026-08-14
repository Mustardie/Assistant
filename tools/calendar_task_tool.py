"""Universal CALENDAR / TASKS tools.

Route to connected adapters that expose the requested capability
(Google Calendar, Microsoft Calendar, Todoist, Notion, local
todo files, ...). Same graceful-degradation rules as communication_tool.
"""

import logging

logger = logging.getLogger(__name__)


def _dispatch(capability: str, method: str, **kwargs):
    from connections.manager import connection_manager

    adapters = connection_manager.find_adapters_with_capability(capability)
    if not adapters:
        return {
            "success": False,
            "error": f"No connected service supports '{capability}'. "
                     "Check Connections to connect one.",
        }
    errors = []
    for name in adapters:
        adapter = connection_manager.get(name)
        if adapter is None:
            continue
        if not connection_manager.get_status(name).get("connected"):
            continue
        fn = getattr(adapter, method, None)
        if not callable(fn):
            continue
        try:
            result = fn(**kwargs)
        except Exception as exc:
            logger.warning("[Tasks] %s.%s failed: %s", name, method, exc)
            errors.append(f"{name}: {exc}")
            continue
        if isinstance(result, dict) and result.get("success") is False:
            errors.append(f"{name}: {result.get('error', 'failed')}")
            continue
        return result
    detail = "; ".join(errors) if errors else "no connected adapter responded"
    return {"success": False, "error": f"No connected service could handle '{capability}': {detail}"}


def create_event(summary, start=None, end=None, source=None, **kwargs):
    """Create a calendar event. MEDIUM risk."""
    if not summary:
        return {"success": False, "error": "Missing 'summary'."}
    if source:
        from connections.manager import connection_manager

        adapter = connection_manager.get(source)
        if adapter is None:
            return {"success": False, "error": f"'{source}' is not a registered integration."}
        if not connection_manager.get_status(source).get("connected"):
            return {"success": False, "error": f"{source} isn't connected."}
        fn = getattr(adapter, "create_event", None)
        if not callable(fn):
            return {"success": False, "error": f"{source} does not support create_event."}
        return fn(summary=summary, start=start, end=end, **kwargs)
    return _dispatch("create_event", "create_event", summary=summary, start=start, end=end, **kwargs)


def update_event(event_id, source=None, **kwargs):
    """Update an existing calendar event."""
    if not event_id:
        return {"success": False, "error": "Missing 'event_id'."}
    if source:
        from connections.manager import connection_manager

        adapter = connection_manager.get(source)
        if adapter is not None and connection_manager.get_status(source).get("connected"):
            fn = getattr(adapter, "update_event", None)
            if callable(fn):
                return fn(event_id=event_id, **kwargs)
    return _dispatch("create_event", "update_event", event_id=event_id, **kwargs)


def delete_event(event_id, source=None, **kwargs):
    """Delete a calendar event. HIGH risk: confirm first."""
    if not event_id:
        return {"success": False, "error": "Missing 'event_id'."}
    if source:
        from connections.manager import connection_manager

        adapter = connection_manager.get(source)
        if adapter is not None and connection_manager.get_status(source).get("connected"):
            fn = getattr(adapter, "delete_event", None)
            if callable(fn):
                return fn(event_id=event_id, **kwargs)
    return _dispatch("create_event", "delete_event", event_id=event_id, **kwargs)


def list_events(start=None, end=None, limit=20, source=None, **kwargs):
    """List calendar events in a time range."""
    if source:
        from connections.manager import connection_manager

        adapter = connection_manager.get(source)
        if adapter is not None and connection_manager.get_status(source).get("connected"):
            fn = getattr(adapter, "list_events", None)
            if callable(fn):
                return fn(start=start, end=end, limit=limit, **kwargs)
    return _dispatch("create_event", "list_events", start=start, end=end, limit=limit, **kwargs)


def create_task(title, due=None, source=None, **kwargs):
    """Create a task / todo / reminder. MEDIUM risk."""
    if not title:
        return {"success": False, "error": "Missing 'title'."}
    if source:
        from connections.manager import connection_manager

        adapter = connection_manager.get(source)
        if adapter is None:
            return {"success": False, "error": f"'{source}' is not a registered integration."}
        if not connection_manager.get_status(source).get("connected"):
            return {"success": False, "error": f"{source} isn't connected."}
        fn = getattr(adapter, "create_task", None)
        if not callable(fn):
            return {"success": False, "error": f"{source} does not support create_task."}
        return fn(title=title, due=due, **kwargs)
    return _dispatch("create_task", "create_task", title=title, due=due, **kwargs)


def update_task(task_id, source=None, **kwargs):
    """Update an existing task."""
    if not task_id:
        return {"success": False, "error": "Missing 'task_id'."}
    if source:
        from connections.manager import connection_manager

        adapter = connection_manager.get(source)
        if adapter is not None and connection_manager.get_status(source).get("connected"):
            fn = getattr(adapter, "update_task", None)
            if callable(fn):
                return fn(task_id=task_id, **kwargs)
    return _dispatch("create_task", "update_task", task_id=task_id, **kwargs)


def list_tasks(limit=50, source=None, **kwargs):
    """List current tasks."""
    if source:
        from connections.manager import connection_manager

        adapter = connection_manager.get(source)
        if adapter is not None and connection_manager.get_status(source).get("connected"):
            fn = getattr(adapter, "list_tasks", None)
            if callable(fn):
                return fn(limit=limit, **kwargs)
    return _dispatch("create_task", "list_tasks", limit=limit, **kwargs)


def create_reminder(text, when=None, source=None, **kwargs):
    """Create a reminder. MEDIUM risk. A reminder is a lightweight task;
    adapters that expose create_task can satisfy it."""
    if not text:
        return {"success": False, "error": "Missing 'text'."}
    if source:
        from connections.manager import connection_manager

        adapter = connection_manager.get(source)
        if adapter is not None and connection_manager.get_status(source).get("connected"):
            fn = getattr(adapter, "create_reminder", None)
            if callable(fn):
                return fn(text=text, when=when, **kwargs)
    return _dispatch("create_task", "create_reminder", text=text, when=when, **kwargs)