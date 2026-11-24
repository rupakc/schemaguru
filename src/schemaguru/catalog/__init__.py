"""Catalog discovery (layer 3): readers, executors, scope filters and the resumable crawl."""

from schemaguru.catalog.crawl import CrawlReport, crawl
from schemaguru.catalog.executor import DBAPIExecutor, FactLangExecutor, MetadataExecutor
from schemaguru.catalog.filters import ScopeFilter
from schemaguru.catalog.protocol import CatalogError, CatalogReader, Page, ReaderCapabilities
from schemaguru.catalog.registry import dialects, reader_for, register_reader

__all__ = [
    "CatalogError",
    "CatalogReader",
    "CrawlReport",
    "DBAPIExecutor",
    "FactLangExecutor",
    "MetadataExecutor",
    "Page",
    "ReaderCapabilities",
    "ScopeFilter",
    "crawl",
    "dialects",
    "reader_for",
    "register_reader",
]
