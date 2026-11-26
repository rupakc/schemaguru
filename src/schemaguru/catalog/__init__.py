"""Catalog discovery (layer 3): readers, executors, scope filters and the resumable crawl."""

from schemaguru.catalog.crawl import CrawlReport, crawl
from schemaguru.catalog.executor import DBAPIExecutor, FactLangExecutor, MetadataExecutor
from schemaguru.catalog.filters import ScopeFilter
from schemaguru.catalog.protocol import (
    BillingReader,
    CatalogError,
    CatalogReader,
    Page,
    ReaderCapabilities,
    ViewDefinitionReader,
    reads_view_definitions,
)
from schemaguru.catalog.registry import dialects, reader_for, register_reader

__all__ = [
    "BillingReader",
    "CatalogError",
    "CatalogReader",
    "CrawlReport",
    "DBAPIExecutor",
    "FactLangExecutor",
    "MetadataExecutor",
    "Page",
    "ReaderCapabilities",
    "ScopeFilter",
    "ViewDefinitionReader",
    "crawl",
    "dialects",
    "reader_for",
    "reads_view_definitions",
    "register_reader",
]
