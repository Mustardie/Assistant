"""Universal COMMUNICATION tools.

These tools route to whatever connected adapter supports the requested
capability, instead of assuming one specific app. The agent never has to
know whether a message came from Discord, Telegram, Slack, WhatsApp,
Teams, or Gmail -- it asks for 'read_messages' and the connection manager
finds an adapter that can provide it.

Every tool degrades gracefully: if no connected adapter supports the
capability the result says so clearly (no tracebacks, no fake success).
"""

import logging

logger = logging.getLogger(__name__)


def _connected_adapters(capability: str):
    from connections.manager import connection_manager

    return connection_manager.find_adapters_with_capability(capability)


def _dispatch(capability: str, method: str, **kwargs):
    """Call `method` on every connected adapter that declares the
    capability. Returns the first successful result, else a clear
    failure."""
    adapters = _connected_adapters(capability)
    if not adapters:
        return {
            "success": False,
            "error": f"No connected service supports '{capability}'. "
                     "Check Connections to connect one.",
        }
    errors = []
    for name in adapters:
        from connections.manager import connection_manager

        adapter = connection_manager.get(name)
        if adapter is None:
            continue
        status = connection_manager.get_status(name)
        if not status.get("connected"):
            continue
        fn = getattr(adapter, method, None)
        if not callable(fn):
            continue
        try:
            result = fn(**kwargs)
        except Exception as exc:
            logger.warning("[Comm] %s.%s failed: %s", name, method, exc)
            errors.append(f"{name}: {exc}")
            continue
        if isinstance(result, dict) and result.get("success") is False:
            errors.append(f"{name}: {result.get('error', 'failed')}")
            continue
        return result
    detail = "; ".join(errors) if errors else "no connected adapter responded"
    return {"success": False, "error": f"No connected service could handle '{capability}': {detail}"}


def read_messages(source=None, limit=20, **kwargs):
    """Read recent messages from a connected messaging service. Optionally
    filter by source (e.g. 'discord', 'whatsapp', 'gmail')."""
    if source:
        from connections.manager import connection_manager

        adapter = connection_manager.get(source)
        if adapter is None:
            return {"success": False, "error": f"'{source}' is not a registered integration."}
        status = connection_manager.get_status(source)
        if not status.get("connected"):
            return {"success": False, "error": f"{source} isn't connected."}
        fn = getattr(adapter, "read_messages", None)
        if not callable(fn):
            return {"success": False, "error": f"{source} does not support read_messages."}
        return fn(limit=limit, **kwargs)
    return _dispatch("read_messages", "read_messages", limit=limit, **kwargs)


def search_messages(query, source=None, limit=20, **kwargs):
    """Search messages across connected services (or one source)."""
    if not query:
        return {"success": False, "error": "Missing 'query'."}
    if source:
        from connections.manager import connection_manager

        adapter = connection_manager.get(source)
        if adapter is None:
            return {"success": False, "error": f"'{source}' is not a registered integration."}
        status = connection_manager.get_status(source)
        if not status.get("connected"):
            return {"success": False, "error": f"{source} isn't connected."}
        fn = getattr(adapter, "search_messages", None)
        if not callable(fn):
            return {"success": False, "error": f"{source} does not support search_messages."}
        return fn(query=query, limit=limit, **kwargs)
    return _dispatch("search_messages", "search_messages", query=query, limit=limit, **kwargs)


def send_message(recipient, text, source=None, **kwargs):
    """Send a message to a recipient. HIGH-risk: callers must confirm
    before invoking unless automation policy allows it."""
    if not recipient:
        return {"success": False, "error": "Missing 'recipient'."}
    if not text:
        return {"success": False, "error": "Missing 'text'."}
    if source:
        from connections.manager import connection_manager

        adapter = connection_manager.get(source)
        if adapter is None:
            return {"success": False, "error": f"'{source}' is not a registered integration."}
        status = connection_manager.get_status(source)
        if not status.get("connected"):
            return {"success": False, "error": f"{source} isn't connected."}
        fn = getattr(adapter, "send_message", None)
        if not callable(fn):
            return {"success": False, "error": f"{source} does not support send_message."}
        return fn(recipient=recipient, text=text, **kwargs)
    return _dispatch("send_message", "send_message", recipient=recipient, text=text, **kwargs)


def reply_to_message(message_id, text, source=None, **kwargs):
    """Reply to a specific message."""
    if source:
        from connections.manager import connection_manager

        adapter = connection_manager.get(source)
        if adapter is None:
            return {"success": False, "error": f"'{source}' is not a registered integration."}
        status = connection_manager.get_status(source)
        if not status.get("connected"):
            return {"success": False, "error": f"{source} isn't connected."}
        fn = getattr(adapter, "reply_to_message", None)
        if not callable(fn):
            return {"success": False, "error": f"{source} does not support reply_to_message."}
        return fn(message_id=message_id, text=text, **kwargs)
    return _dispatch("send_message", "reply_to_message", message_id=message_id, text=text, **kwargs)


def identify_sender(message, source=None, **kwargs):
    """Resolve the sender name of a message object."""
    if source:
        from connections.manager import connection_manager

        adapter = connection_manager.get(source)
        if adapter is not None and connection_manager.get_status(source).get("connected"):
            fn = getattr(adapter, "identify_sender", None)
            if callable(fn):
                return fn(message=message, **kwargs)
    return _dispatch("read_messages", "identify_sender", message=message, **kwargs)


def inspect_attachment(message, index=0, source=None, **kwargs):
    """Inspect (metadata + content) an attachment on a message."""
    if source:
        from connections.manager import connection_manager

        adapter = connection_manager.get(source)
        if adapter is not None and connection_manager.get_status(source).get("connected"):
            fn = getattr(adapter, "inspect_attachment", None)
            if callable(fn):
                return fn(message=message, index=index, **kwargs)
    return _dispatch("read_messages", "inspect_attachment", message=message, index=index, **kwargs)


def download_attachment(message, index=0, destination=None, source=None, **kwargs):
    """Download an attachment to local disk (or a default Downloads
    folder)."""
    if source:
        from connections.manager import connection_manager

        adapter = connection_manager.get(source)
        if adapter is not None and connection_manager.get_status(source).get("connected"):
            fn = getattr(adapter, "download_attachment", None)
            if callable(fn):
                return fn(message=message, index=index, destination=destination, **kwargs)
    return _dispatch("read_messages", "download_attachment",
                     message=message, index=index, destination=destination, **kwargs)