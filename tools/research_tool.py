"""research_topic tool: run the full research pipeline for a topic."""

import logging
import threading

from research.engine import ResearchEngine
from research.providers.base import ResearchProviderError

logger = logging.getLogger(__name__)

_lock = threading.Lock()


def research_topic(topic=None, max_sources=None):
    """Run the full Tavily-based research pipeline for a topic.

    Creates Downloads/Nova Research/<Topic>/ with Research/ (DOCX report
    + downloadable papers), Images/, and Sources/ (sources.txt manifest).
    Returns the folder path and a summary of what was collected.

    Use this when the user asks to RESEARCH a topic in depth and wants a
    saved report. For quick lookups, prefer web_research instead.
    """
    if not topic or not str(topic).strip():
        return {
            "success": False,
            "error": "research_topic requires a topic argument, e.g. research_topic(topic='Quantum Computing')",
        }

    if not _lock.acquire(blocking=False):
        return {"success": False, "error": "A research task is already running."}

    try:
        engine = ResearchEngine(
            topic=str(topic),
            max_sources=int(max_sources) if max_sources else None,
        )
        bundle = engine.run()
        return {
            "success": True,
            "topic": bundle.topic,
            "folder": bundle.folder,
            "report": bundle.report_path,
            "source_count": len(bundle.sources),
            "image_count": len(bundle.images),
            "paper_count": len(bundle.papers),
            "message": (
                f"Research complete for '{bundle.topic}'. "
                f"Report saved to {bundle.report_path}. "
                f"{len(bundle.sources)} sources, {len(bundle.images)} images, "
                f"{len(bundle.papers)} papers."
            ),
        }
    except ResearchProviderError as error:
        logger.error("Research failed: %s", error)
        return {"success": False, "error": str(error)}
    except Exception as error:
        logger.exception("Research pipeline failed for topic=%r", topic)
        return {"success": False, "error": f"Research failed: {error}"}
    finally:
        _lock.release()
