"""Shipped LLM providers. The SDK-backed ones import their SDK lazily (extras)."""

from schemaguru.draft.providers.anthropic import AnthropicProvider
from schemaguru.draft.providers.callable import CallableProvider
from schemaguru.draft.providers.openai_compat import OpenAICompatibleProvider

__all__ = ["AnthropicProvider", "CallableProvider", "OpenAICompatibleProvider"]
