"""LLM factory backed by SAP AI Core (generative AI hub SDK / sap-ai-sdk-gen)."""
from __future__ import annotations

import logging
from functools import lru_cache

from langchain_core.language_models import BaseChatModel

from .config import get_settings

log = logging.getLogger(__name__)

_OPENAI_PREFIXES = ("gpt-", "o1", "o3", "o4")


@lru_cache
def get_llm() -> BaseChatModel:
    s = get_settings()
    from gen_ai_hub.proxy.core.proxy_clients import get_proxy_client

    proxy_client = get_proxy_client("gen-ai-hub")
    kwargs: dict = {
        "proxy_client": proxy_client,
        "temperature": s.llm_temperature,
        "max_tokens": s.llm_max_tokens,
    }
    if s.llm_deployment_id:
        kwargs["deployment_id"] = s.llm_deployment_id

    if s.llm_model.startswith(_OPENAI_PREFIXES):
        # Native LangChain ChatOpenAI drop-in: best tool-calling + token streaming support
        from gen_ai_hub.proxy.langchain.openai import ChatOpenAI

        llm = ChatOpenAI(proxy_model_name=s.llm_model, **kwargs)
    else:
        # Harmonised init for other vendors (Anthropic/Gemini/etc. on AI Core)
        from gen_ai_hub.proxy.langchain import init_llm

        llm = init_llm(s.llm_model, **kwargs)

    log.info("LLM initialised: %s (SAP AI Core)", s.llm_model)
    return llm
