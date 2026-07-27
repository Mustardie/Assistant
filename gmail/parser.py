import base64
import re
from typing import Any


def decode_body(data: str) -> str:
    if not data:
        return ""

    try:
        padding = "=" * (-len(data) % 4)
        normalized = data.replace("-", "+").replace("_", "/") + padding
        return base64.b64decode(normalized.encode("ascii")).decode("utf-8", errors="replace")
    except Exception:
        return data


def strip_html(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"<[^>]+>", " ", text)


def _split_quoted_history(text: str) -> tuple[str, str]:
    if not text:
        return text, ""

    pattern = r"(?im)^on\s+.+?\s+wrote:\s*$"
    if not re.search(pattern, text):
        return text, ""

    parts = re.split(pattern, text, maxsplit=1)
    if len(parts) == 2:
        latest = parts[0].strip()
        history = parts[1].strip()
        return latest, history
    return text, ""


def extract_body(payload: dict[str, Any] | None) -> tuple[str, str]:
    if not payload:
        return "", ""

    body = payload.get("body", {})
    data = body.get("data")
    if data:
        text = decode_body(data)
        clean_text = strip_html(text).strip()
        newest, quoted = _split_quoted_history(clean_text)
        return newest, quoted

    parts = payload.get("parts") or []
    texts = []
    for part in parts:
        part_type = part.get("mimeType", "")
        if part_type.startswith("text/plain"):
            part_body, quoted = extract_body(part)
            if part_body:
                texts.append(part_body)
            if quoted:
                texts.append(quoted)
        elif part_type.startswith("text/html"):
            html_text = part.get("body", {}).get("data")
            if html_text:
                text = decode_body(html_text)
                clean_text = strip_html(text).strip()
                newest, quoted = _split_quoted_history(clean_text)
                if newest:
                    texts.append(newest)
                if quoted:
                    texts.append(quoted)
        elif part.get("parts"):
            nested_body, nested_quoted = extract_body(part)
            if nested_body:
                texts.append(nested_body)
            if nested_quoted:
                texts.append(nested_quoted)

    combined = "\n".join(texts).strip()
    newest, quoted = _split_quoted_history(combined)
    return newest, quoted


def parse_headers(headers: list[dict] | None) -> dict:
    parsed = {}
    for header in headers or []:
        name = header.get("name", "")
        value = header.get("value", "")
        if name:
            parsed[name] = value
    return parsed
