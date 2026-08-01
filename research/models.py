"""Data models shared across the research pipeline."""

from dataclasses import dataclass, field


@dataclass
class SearchResult:
    """One search hit from a research provider (Tavily, etc.)."""

    title: str
    url: str
    snippet: str = ""
    source_type: str = "web"
    content: str = ""


@dataclass
class SourceInfo:
    """A source page actually used in the report."""

    title: str
    url: str
    source_type: str = "web"
    accessed_at: str = ""
    snippet: str = ""
    sections: list = field(default_factory=list)


@dataclass
class ImageAsset:
    """A downloaded image with metadata for embedding."""

    path: str = ""
    url: str = ""
    caption: str = ""
    alt: str = ""


@dataclass
class PaperInfo:
    """A research paper (downloaded PDF or metadata-only entry)."""

    title: str = ""
    authors: str = ""
    abstract: str = ""
    publication: str = ""
    url: str = ""
    pdf_path: str = ""
    downloaded: bool = False


@dataclass
class ResearchBundle:
    """Everything the engine collected for one research topic."""

    topic: str
    folder: str = ""
    sources: list = field(default_factory=list)
    images: list = field(default_factory=list)
    papers: list = field(default_factory=list)
    report_path: str = ""
