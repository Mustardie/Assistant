"""Write the Sources/sources.txt manifest for a research bundle."""

import logging
from datetime import datetime
from pathlib import Path

from research.models import ResearchBundle

logger = logging.getLogger(__name__)


def write_sources_txt(bundle: ResearchBundle, sources_dir: Path) -> Path:
    """Create Sources/sources.txt listing every source used in the report."""
    sources_dir.mkdir(parents=True, exist_ok=True)
    path = sources_dir / "sources.txt"

    lines = [
        f"Research topic: {bundle.topic}",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Sources: {len(bundle.sources)}",
        "",
        "=" * 60,
        "",
    ]

    for index, source in enumerate(bundle.sources, 1):
        lines.extend(
            [
                f"[{index}] {source.title}",
                f"    URL: {source.url}",
                f"    Type: {source.source_type}",
                f"    Accessed: {source.accessed_at or '—'}",
                "",
            ]
        )

    if bundle.papers:
        lines.append("=" * 60)
        lines.append("Research papers")
        lines.append("")
        for index, paper in enumerate(bundle.papers, 1):
            status = "downloaded" if paper.downloaded else "metadata only"
            lines.extend(
                [
                    f"[{index}] {paper.title}",
                    f"    Authors: {paper.authors or '—'}",
                    f"    Publication: {paper.publication or '—'}",
                    f"    URL: {paper.url}",
                    f"    Status: {status}",
                    "",
                ]
            )

    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Wrote sources manifest: %s", path)
    return path
