"""Read/write the project .env file, preserving comments and ordering."""

from pathlib import Path

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def env_path() -> Path:
    return _ENV_PATH


def read_env() -> dict[str, str]:
    """Parse .env into {KEY: value}, ignoring comments and blanks."""
    values: dict[str, str] = {}
    try:
        text = _ENV_PATH.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _format_value(value: str) -> str:
    if any(c in value for c in " #'\""):
        return f'"{value}"'
    return value


def update_env_file(pairs: dict[str, str]) -> None:
    """Upsert KEY=VALUE entries into .env.

    Existing keys are replaced in place (comments and order preserved);
    new keys are appended under a blank-line separator. Creates the file
    if it does not exist yet.
    """
    pairs = {k.strip(): v.strip() for k, v in pairs.items() if k and k.strip()}
    if not pairs:
        return

    lines: list[str] = []
    if _ENV_PATH.exists():
        lines = _ENV_PATH.read_text(encoding="utf-8").splitlines()

    remaining = dict(pairs)
    updated: list[str] = []
    changed = False
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                value = remaining.pop(key)
                indent = line[: len(line) - len(line.lstrip())]
                new_line = f"{indent}{key}={_format_value(value)}"
                if new_line != line:
                    changed = True
                updated.append(new_line)
                continue
        updated.append(line)

    if remaining:
        if updated and updated[-1].strip():
            updated.append("")
        for key, value in remaining.items():
            updated.append(f"{key}={_format_value(value)}")
        changed = True

    if changed:
        _ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
        _ENV_PATH.write_text("\n".join(updated) + "\n", encoding="utf-8")
