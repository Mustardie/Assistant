from __future__ import annotations

import argparse
import json

from .service import default_capability_service


def _print(value) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m capabilities.cli", description="Inspect JARVIS learned capabilities")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    commands.add_parser("skills")
    inspect_cmd = commands.add_parser("inspect")
    inspect_cmd.add_argument("id")
    search = commands.add_parser("search")
    search.add_argument("request")
    invalidate = commands.add_parser("invalidate")
    invalidate.add_argument("id")
    invalidate.add_argument("--reason", default="manually invalidated")
    revalidate = commands.add_parser("revalidate")
    revalidate.add_argument("id")
    revalidate.add_argument("--confirm", action="store_true")
    delete = commands.add_parser("delete")
    delete.add_argument("id")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    service = default_capability_service()
    if args.command == "list":
        _print([item.to_dict() for item in service.store.capabilities()])
    elif args.command == "skills":
        _print([item.to_dict() for item in service.store.skills()])
    elif args.command == "inspect":
        item = service.get(args.id)
        _print(item.to_dict() if item else {"error": "not found"})
        return 0 if item else 1
    elif args.command == "search":
        _print(service.search(args.request))
    elif args.command == "invalidate":
        _print(service.invalidate(args.id, args.reason))
    elif args.command == "revalidate":
        _print(service.validate(args.id, confirm=args.confirm))
    elif args.command == "delete":
        _print(service.delete(args.id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
