"""Small keyless weather adapter used by the JARVIS weather widget."""

from __future__ import annotations

from urllib.parse import quote

import httpx


def current_weather(location: str, *, timeout: float = 8.0, client=None) -> dict:
    """Fetch and normalize current conditions plus a compact three-day forecast.

    wttr.in is used because it requires no account.  Failures remain explicit;
    callers must never turn a network error into a connected/demo response.
    """
    place = str(location or "").strip()
    if not place:
        return {"success": False, "error": "Choose a city or region first.", "retryable": False}
    requester = client or httpx
    try:
        response = requester.get(
            f"https://wttr.in/{quote(place)}?format=j1",
            timeout=timeout,
            headers={"User-Agent": "JARVIS-local-assistant/1.0"},
        )
        response.raise_for_status()
        payload = response.json()
        current = (payload.get("current_condition") or [{}])[0]
        nearest = (payload.get("nearest_area") or [{}])[0]
        area = ((nearest.get("areaName") or [{}])[0].get("value") or place)
        region = ((nearest.get("region") or [{}])[0].get("value") or "")
        description = ((current.get("weatherDesc") or [{}])[0].get("value") or "Conditions unavailable")
        forecast = []
        for day in (payload.get("weather") or [])[:3]:
            date = day.get("date") or ""
            high = day.get("maxtempC", "—")
            low = day.get("mintempC", "—")
            forecast.append(f"{date}  {low}–{high}°C")
        return {
            "success": True,
            "connected": True,
            "location": f"{area}{', ' + region if region else ''}",
            "temperature": current.get("temp_C", "—"),
            "unit": "°C",
            "summary": description,
            "feels_like": current.get("FeelsLikeC"),
            "humidity": current.get("humidity"),
            "forecast": forecast,
            "warning": "Live conditions via wttr.in",
        }
    except Exception as exc:
        return {
            "success": False,
            "connected": False,
            "location": place,
            "error": f"Weather service unavailable: {exc}",
            "retryable": True,
        }

