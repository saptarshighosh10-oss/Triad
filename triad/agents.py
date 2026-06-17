"""Provider-backed agents.

Each agent wraps one provider's async SDK behind a common interface:

  * stream(user_msg)      -> async generator of text deltas (keeps conversation history)
  * complete(user_msg)    -> full string                    (keeps conversation history)
  * stream_raw(messages)  -> async generator                (ephemeral, no history mutation)
  * complete_raw(messages)-> full string                    (ephemeral)

SDKs are imported lazily so the package loads even when a provider isn't installed,
and a missing key just disables that agent instead of crashing the app.
"""
from __future__ import annotations

import inspect
import os
from typing import AsyncIterator, Dict, List, Optional

from . import config

Message = Dict[str, str]  # {"role": "user"|"assistant", "content": str}


class Agent:
    name: str = "agent"
    label: str = "Agent"
    color: str = "white"

    def __init__(self) -> None:
        cfg = config.resolve(self.name)  # reads env now, not at import — overrides always apply
        self.model: str = cfg["model"]
        self.api_key: str = os.environ.get(cfg["key_env"], "").strip()
        self.base_url: Optional[str] = cfg.get("base_url")
        self.system_prompt: str = ""
        self.history: List[Message] = []
        self._rolling_summary: str = ""  # injected as ephemeral context; rebuilt on trim
        self._client = None

    # --------------------------------------------------------------- meta
    @property
    def available(self) -> bool:
        if self.api_key:
            return True
        # A local/self-hosted gateway (free-claude-code, Ollama, LM Studio) needs no key.
        return bool(self.base_url) and any(
            h in self.base_url for h in ("localhost", "127.0.0.1", "0.0.0.0", "::1"))

    def set_system(self, text: str) -> None:
        self.system_prompt = text.strip()

    def add_skill(self, body: str) -> None:
        body = body.strip()
        self.system_prompt = f"{self.system_prompt}\n\n{body}".strip() if self.system_prompt else body

    def clear(self) -> None:
        self.history = []

    # -------------------------------------------------- persistent chat
    async def stream(self, user_msg: str, context: str = "") -> AsyncIterator[str]:
        """Stream a reply and append the full exchange to this agent's history.

        `context` is ephemeral memory (e.g. notes recalled from the vault) prepended to the system
        prompt for THIS call only. It is never stored in history, so recalled context doesn't
        compound the token cost turn after turn — that's the whole point of recall-over-re-read.
        """
        self.history.append({"role": "user", "content": user_msg})
        system = self.system_prompt
        if getattr(self, '_rolling_summary', ''):
            system = f"{self._rolling_summary}\n\n{system}".strip() if system else self._rolling_summary
        if context:
            system = f"{context}\n\n{system}".strip() if system else context
        full = ""
        try:
            async for delta in self._provider_stream(self.history, system):
                full += delta
                yield delta
        except Exception:
            # Stream failed — remove the orphan user message so the next call
            # doesn't send two consecutive user messages (API error on most providers).
            if self.history and self.history[-1].get("content") == user_msg:
                self.history.pop()
            raise
        self.history.append({"role": "assistant", "content": full})

    async def complete(self, user_msg: str) -> str:
        out = ""
        async for d in self.stream(user_msg):
            out += d
        return out

    # ------------------------------------------------ ephemeral (orchestration)
    async def stream_raw(
        self, messages: List[Message], system: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[str]:
        sys_prompt = system if system is not None else self.system_prompt
        async for delta in self._provider_stream(messages, sys_prompt, max_tokens=max_tokens):
            yield delta

    async def complete_raw(self, messages: List[Message], system: Optional[str] = None,
                           max_tokens: Optional[int] = None) -> str:
        out = ""
        async for d in self.stream_raw(messages, system, max_tokens=max_tokens):
            out += d
        return out

    # --------------------------------------------------------- provider hook
    async def _provider_stream(self, messages: List[Message], system: str,
                               max_tokens: Optional[int] = None) -> AsyncIterator[str]:
        raise NotImplementedError
        yield  # pragma: no cover  (makes this an async generator)


# ----------------------------------------------------------------------------
class OpenAICompatibleAgent(Agent):
    """Any OpenAI-compatible chat endpoint. OpenAI itself uses the default base_url;
    Groq / OpenRouter / NIM / a local gateway just set `base_url` in config. Subclasses
    only need name/label/color — model, key_env, and base_url all come from config."""

    def _obj(self):
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as e:
                raise RuntimeError("OpenAI SDK missing — run: pip install openai") from e
            kwargs = {"api_key": self.api_key or "not-needed"}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = AsyncOpenAI(**kwargs)
        return self._client

    async def _provider_stream(self, messages, system, max_tokens=None):
        client = self._obj()
        msgs: List[Message] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(messages)
        stream = await client.chat.completions.create(
            model=self.model, messages=msgs, stream=True,
            max_tokens=max_tokens or config.max_tokens(),
        )
        async for chunk in stream:
            if chunk.choices:
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    yield delta.content


class OpenAIAgent(OpenAICompatibleAgent):
    name, label, color = "chatgpt", "ChatGPT", "green"


# Free-cloud roster — all OpenAI-compatible, differing only by base_url + model.
# Keep the model lineages DISTINCT (Llama / GPT-OSS / Qwen): three finetunes of one
# base make correlated errors and then agree, which hollows out any aggregation.
class GroqAgent(OpenAICompatibleAgent):
    name, label, color = "groq", "Groq", "magenta"


class OpenRouterAgent(OpenAICompatibleAgent):
    name, label, color = "openrouter", "OpenRouter", "bright_cyan"


class NIMAgent(OpenAICompatibleAgent):
    name, label, color = "nim", "NIM", "bright_green"


# ----------------------------------------------------------------------------
class ClaudeAgent(Agent):
    name, label, color = "claude", "Claude", "dark_orange3"

    def _obj(self):
        if self._client is None:
            try:
                from anthropic import AsyncAnthropic
            except ImportError as e:
                raise RuntimeError("Anthropic SDK missing — run: pip install anthropic") from e
            kwargs = {"api_key": self.api_key or "not-needed"}
            # TRIAD_CLAUDE_BASE_URL (e.g. free-claude-code on http://localhost:8082) makes the
            # Claude slot run on a free model; fcc speaks the Anthropic API so nothing else changes.
            # Passed explicitly (not via global ANTHROPIC_BASE_URL) so it can't hijack other clients.
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = AsyncAnthropic(**kwargs)
        return self._client

    async def _provider_stream(self, messages, system, max_tokens=None):
        client = self._obj()
        kwargs = dict(model=self.model, max_tokens=max_tokens or config.max_tokens(),
                      messages=messages)
        if system:
            # cache_control marks the system prompt for Anthropic's prompt cache:
            # after the first call, re-sends cost ~10% of normal. Saves 90% on
            # system-prompt tokens for long sessions with a stable system prompt.
            kwargs["system"] = [{"type": "text", "text": system,
                                  "cache_control": {"type": "ephemeral"}}]
        async with client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                yield text


# ----------------------------------------------------------------------------
class GeminiAgent(Agent):
    name, label, color = "gemini", "Gemini", "blue"

    def _obj(self):
        if self._client is None:
            try:
                from google import genai
            except ImportError as e:
                raise RuntimeError("Google GenAI SDK missing — run: pip install google-genai") from e
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    @staticmethod
    def _to_contents(messages: List[Message]):
        contents = []
        for m in messages:
            role = "model" if m["role"] == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": m["content"]}]})
        return contents

    async def _provider_stream(self, messages, system, max_tokens=None):
        client = self._obj()
        from google.genai import types

        cfg = types.GenerateContentConfig(
            system_instruction=system or None,
            max_output_tokens=max_tokens or config.max_tokens(),
        )
        maybe = client.aio.models.generate_content_stream(
            model=self.model, contents=self._to_contents(messages), config=cfg
        )
        # SDK surface has shifted between versions: it may return a coroutine
        # (await -> async iterator) or an async iterator directly. Handle both.
        stream = await maybe if inspect.isawaitable(maybe) else maybe
        async for chunk in stream:
            text = getattr(chunk, "text", None)
            if text:
                yield text


# ----------------------------------------------------------------------------
PAID_AGENTS = (OpenAIAgent, ClaudeAgent, GeminiAgent)
FREE_AGENTS = (GroqAgent, OpenRouterAgent, NIMAgent)
ROSTERS = {"paid": PAID_AGENTS, "free": FREE_AGENTS, "all": PAID_AGENTS + FREE_AGENTS}

FREE_RANK = ("openrouter", "nim", "groq")


def build_agents(roster: str = "paid") -> List[Agent]:
    """Return the agents in `roster` whose key (or local base_url) is present, in order."""
    agents: List[Agent] = []
    for cls in ROSTERS.get(roster, PAID_AGENTS):
        a = cls()
        if a.available:
            agents.append(a)
    return agents


def build_free_swarm(n: int) -> List[Agent]:
    """Spin up n free workers SPREAD ACROSS PROVIDERS, so the swarm doesn't rate-limit itself.

    Stacking every worker on one provider (the old behavior) trips that provider's free-tier
    limit the moment the tree fans out. Instead we seed one agent per available provider (Groq /
    NIM / OpenRouter — distinct lineages, distinct rate buckets), then top up any remaining slots
    with extra distinct OpenRouter free models. Returns [] only if no free provider is configured.
    """
    from . import config as _cfg
    agents: List[Agent] = []
    # One per provider, using its config default (verified-live) model — spreads load + decorrelates.
    for cls in (GroqAgent, NIMAgent, OpenRouterAgent):
        a = cls()
        if a.available:
            a.label = f"{a.label}:{a.model.split('/')[-1].replace(':free','')[:16]}"
            agents.append(a)
    # Top up remaining slots with extra distinct OpenRouter free models, if a key is present.
    if os.environ.get("OPENROUTER_API_KEY", "").strip():
        used = {a.model for a in agents}
        for model_id in _cfg.FREE_OR_MODELS:
            if len(agents) >= n:
                break
            if model_id in used:
                continue
            a = OpenRouterAgent()
            a.model = model_id
            a.label = model_id.split("/")[-1].replace(":free", "")
            agents.append(a)
    return agents[:n]
