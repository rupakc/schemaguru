"""Semantic drafting with an LLM (layer 5, spec 10, 11).

An LLM drafts the *meaning* of tables; humans accept or override. Nothing here runs on the agent
query path or from scoping/rendering: drafting is an explicit run (``draft_tables``). The core
library works without any LLM.
"""

from schemaguru.draft.accept import accept, write_accepted
from schemaguru.draft.inputs import DraftInput, build_inputs
from schemaguru.draft.pipeline import DraftReport, draft_tables, prompt_hash
from schemaguru.draft.provider import DRAFT_SCHEMA, InvalidReply, LLMProvider, Usage, validate
from schemaguru.draft.providers import (
    AnthropicProvider,
    CallableProvider,
    OpenAICompatibleProvider,
)
from schemaguru.draft.verify import verify_drafts

__all__ = [
    "DRAFT_SCHEMA",
    "AnthropicProvider",
    "CallableProvider",
    "DraftInput",
    "DraftReport",
    "InvalidReply",
    "LLMProvider",
    "OpenAICompatibleProvider",
    "Usage",
    "accept",
    "build_inputs",
    "draft_tables",
    "prompt_hash",
    "validate",
    "verify_drafts",
    "write_accepted",
]
