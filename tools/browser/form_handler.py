import logging

from .element_finder import ElementNotFoundError, resolve

logger = logging.getLogger(__name__)

# Each semantic field name maps to a list of description candidates tried in
# order (label text / placeholder text / common attribute values), plus an
# optional input "type" hint used only to prefer role=textbox matches.
FIELD_DESCRIPTIONS: dict[str, list[str]] = {
    "email": ["Email", "Email address", "E-mail", "Username or email"],
    "password": ["Password", "Enter your password"],
    "confirm_password": ["Confirm password", "Re-enter password", "Repeat password"],
    "search": ["Search", "Search this site", "Search..."],
    "name": ["Name", "Full name", "Your name"],
    "first_name": ["First name", "Given name"],
    "last_name": ["Last name", "Surname", "Family name"],
    "phone": ["Phone", "Phone number", "Mobile number", "Mobile"],
    "address": ["Address", "Street address"],
    "city": ["City", "Town"],
    "state": ["State", "Province", "Region"],
    "zip": ["Zip code", "Postal code", "Zip"],
    "country": ["Country"],
    "message": ["Message", "Comments", "Your message"],
    "subject": ["Subject"],
    "date": ["Date"],
    "card_number": ["Card number", "Credit card number"],
    "cvv": ["CVV", "Security code", "CVC"],
}


def fill_form(page, fields: dict) -> dict:
    """fields: {"email": "...", "password": "...", ...} using the semantic
    names in FIELD_DESCRIPTIONS (or any freeform label text as the key --
    it's tried directly if not a recognized semantic name)."""
    results = []
    all_ok = True

    for field_name, value in fields.items():
        candidates = FIELD_DESCRIPTIONS.get(field_name.lower().strip(), [field_name])
        filled = False
        last_error = None

        for description in candidates:
            try:
                resolved = resolve(page, description)
            except ElementNotFoundError as exc:
                last_error = str(exc)
                continue

            try:
                resolved.locator.fill(str(value), timeout=8000)
                results.append({
                    "field": field_name, "success": True,
                    "matched_description": description, "strategy": resolved.strategy,
                })
                filled = True
                break
            except Exception as exc:
                last_error = str(exc)
                continue

        if not filled:
            all_ok = False
            results.append({
                "field": field_name, "success": False,
                "error": last_error or f"No field matching '{field_name}' was found on this page.",
            })

    return {"success": all_ok, "fields": results}
