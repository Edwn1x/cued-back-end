"""
Programmable fake for the Anthropic SDK, patched centrally at
`anthropic.resources.messages.Messages.create`.

The client is a module-global built at import time in ~10 modules with no shared
factory (see rewrite/phase-0/INVESTIGATION.md §5), so one patch on the unbound
method covers every instance. Tier-1 installs it via an autouse fixture; tier-2
disables the fixture and runs the real SDK.

Response objects mimic the shape call sites actually touch: `.content[i].text`,
`.content[i].type`, `.usage.{input,output,cache_*}_tokens`, `.stop_reason`,
`.model`, `.id`. Controller lets a test script exact replies; the default keeps
the pipeline from crashing on the many JSON-parsing call sites.
"""

from __future__ import annotations


class _Block:
    def __init__(self, text: str, type: str = "text"):
        self.type = type
        self.text = text


class _ToolUseBlock:
    def __init__(self, name: str, input: dict, id: str = "toolu_stub"):
        self.type = "tool_use"
        self.name = name
        self.input = input
        self.id = id


class ToolUse:
    """Marker a test scripts to make the fake return a tool_use response
    (stop_reason='tool_use') for the agent tool-execution loop."""
    def __init__(self, name: str, input: dict, id: str = "toolu_stub"):
        self.name = name
        self.input = input
        self.id = id


class _Usage:
    def __init__(self, input_tokens=1, output_tokens=1):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = 0


class _Response:
    def __init__(self, text: str, model: str = "stub-model"):
        self.id = "msg_stub"
        self.type = "message"
        self.role = "assistant"
        self.model = model
        self.stop_reason = "end_turn"
        self.stop_sequence = None
        self.content = [_Block(text)]
        self.usage = _Usage()


class FakeAnthropicController:
    """Per-test control over what `messages.create` returns.

    - default_text: returned when no handler/script matches.
    - handler: callable(kwargs) -> str | _Response | None. First consulted.
    - script: FIFO list of (str | _Response); each call pops one if present.
    Every call's kwargs is recorded in `.calls` for assertions.
    """

    def __init__(self, default_text: str = "ok"):
        self.default_text = default_text
        self.handler = None
        self.script: list = []
        self.calls: list = []

    def reply_with(self, fn):
        self.handler = fn
        return self

    def push(self, *texts):
        self.script.extend(texts)
        return self

    def _coerce(self, value) -> _Response:
        if isinstance(value, _Response):
            return value
        if isinstance(value, ToolUse):
            r = _Response("")
            r.content = [_ToolUseBlock(value.name, value.input, value.id)]
            r.stop_reason = "tool_use"
            return r
        return _Response(str(value))

    def handle(self, kwargs) -> _Response:
        self.calls.append(kwargs)
        if self.handler is not None:
            out = self.handler(kwargs)
            if out is not None:
                return self._coerce(out)
        if self.script:
            return self._coerce(self.script.pop(0))
        return _Response(self.default_text)


def make_create(controller: FakeAnthropicController):
    """Build the replacement for Messages.create bound to a controller."""

    def _create(_messages_self, *args, **kwargs):  # unbound method: first arg is self
        return controller.handle(kwargs)

    return _create
