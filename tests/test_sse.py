from app.utils.sse import iter_sse_events, parse_tbox_sse_event


def test_parse_tbox_sse_event_old_style_data_envelope():
    raw_event = 'data: {"event":"chunk","payload":{"text":"hello"}}'
    parsed = parse_tbox_sse_event(raw_event)
    assert parsed == {"event": "chunk", "payload": {"text": "hello"}}


def test_parse_tbox_sse_event_new_style_event_data():
    raw_event = (
        "id:17\n"
        "event:message\n"
        'data:{"lane":"default","payload":"{\\"text\\":\\"模型\\"}","type":"chunk"}'
    )
    parsed = parse_tbox_sse_event(raw_event)
    assert parsed == {"event": "chunk", "payload": {"text": "模型"}}


def test_parse_tbox_sse_event_end_event():
    raw_event = 'event:end\ndata:{"type":"end"}'
    parsed = parse_tbox_sse_event(raw_event)
    assert parsed == {"event": "end", "payload": {}}


async def test_iter_sse_events_splits_by_double_newline():
    async def _byte_iter():
        yield b"event:message\ndata:{\"type\":\"chunk\",\"payload\":\"{\\\"text\\\":\\\"A\\\"}\"}\n\n"
        yield b"event:message\ndata:{\"type\":\"chunk\",\"payload\":\"{\\\"text\\\":\\\"B\\\"}\"}\n\n"

    events = []
    async for event in iter_sse_events(_byte_iter()):
        events.append(event)

    assert len(events) == 2
    assert "text" in events[0]
    assert "text" in events[1]
