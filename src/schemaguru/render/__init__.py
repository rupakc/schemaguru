"""Rendering (layer 6, spec 13): context views, profiles, legend, formats and fingerprints."""

from schemaguru.render.fingerprint import ContextSource, fingerprint, fingerprint_views
from schemaguru.render.formats import format_view, register_format
from schemaguru.render.fragments import render_join_snippet, validate_filter
from schemaguru.render.knowledge import TableKnowledge, collect
from schemaguru.render.legend import LEGEND, LEGEND_SENTENCE
from schemaguru.render.profiles import Profile, get_profile, profile_names, register_profile
from schemaguru.render.view import ContextView, ViewBlock, ViewRenderer, build_view

__all__ = [
    "LEGEND",
    "LEGEND_SENTENCE",
    "ContextSource",
    "ContextView",
    "Profile",
    "TableKnowledge",
    "ViewBlock",
    "ViewRenderer",
    "build_view",
    "collect",
    "fingerprint",
    "fingerprint_views",
    "format_view",
    "get_profile",
    "profile_names",
    "register_format",
    "register_profile",
    "render_join_snippet",
    "validate_filter",
]
