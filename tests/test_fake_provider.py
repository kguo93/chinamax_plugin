"""The fake provider serves scripted streaming Messages the real SDK can consume."""

from __future__ import annotations

import anthropic

from fake_provider import text_block, turn


def test_scripted_stream_roundtrip(start_fake_provider):
    """A scripted text turn is served as valid SSE and consumed by a raw SDK client."""
    provider = start_fake_provider([turn([text_block("hello from the fake provider")])])
    client = anthropic.Anthropic(
        base_url=provider.base_url, auth_token="sk-test", max_retries=0
    )

    with client.messages.stream(
        model="fake-model",
        max_tokens=64,
        messages=[{"role": "user", "content": "hi"}],
    ) as stream:
        message = stream.get_final_message()

    assert [block.text for block in message.content] == ["hello from the fake provider"]
    assert message.stop_reason == "end_turn"
    assert message.role == "assistant"
    assert provider.requests[0]["body"]["messages"] == [{"role": "user", "content": "hi"}]
