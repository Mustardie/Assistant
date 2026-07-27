"""Simple script to rebuild the file index from scratch."""
import sys
from pathlib import Path


def main():
    sys.path.insert(0, str(Path(__file__).parent.resolve()))
    from file_manager.service import SemanticFileManagerService

    service = SemanticFileManagerService()
    result = service.rebuild_index()
    status = service.show_status()

    print("=== Index Rebuild Complete ===")
    print(f"  Success:     {result.get('success')}")
    print(f"  Files added: {result.get('indexed', 'N/A')}")
    print(f"  Total files: {status.get('indexed_files', 'N/A')}")
    print(f"  Index size:  {status.get('index_size', 'N/A')} bytes")
    print(f"  Last scan:   {status.get('last_scan', 'N/A')}")


if __name__ == "__main__":
    main()
