import api.metering as metering


class _Clock:
    def __init__(self):
        self.wall = 1000.0
        self.mono = 10.0

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono


def _meter(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(metering.time, 'time', clock.time)
    monkeypatch.setattr(metering.time, 'monotonic', clock.monotonic)
    return metering.GlobalMeter(), clock


def test_first_output_records_stream_ttft_ms(monkeypatch):
    meter, clock = _meter(monkeypatch)
    meter.begin_session('one')
    meter.set_pending_started_at('one', 1000.0)

    clock.wall += 0.250
    meter.record_token('one', 1)

    assert meter.get_stats('one')['ttft_ms'] == 250


def test_reasoning_before_output_does_not_start_ttft(monkeypatch):
    meter, clock = _meter(monkeypatch)
    meter.begin_session('one')
    meter.set_pending_started_at('one', 1000.0)

    clock.wall += 0.100
    meter.record_reasoning('one', 1)
    assert 'ttft_ms' not in meter.get_stats('one')

    clock.wall += 0.150
    meter.record_token('one', 1)
    assert meter.get_stats('one')['ttft_ms'] == 250

    clock.wall += 1.0
    meter.record_token('one', 2)
    assert meter.get_stats('one')['ttft_ms'] == 250


def test_missing_start_and_zero_token_omit_ttft(monkeypatch):
    meter, clock = _meter(monkeypatch)
    meter.begin_session('missing')
    meter.set_pending_started_at('missing', None)
    meter.record_token('missing', 1)
    assert 'ttft_ms' not in meter.get_stats('missing')

    meter.begin_session('invalid')
    meter.set_pending_started_at('invalid', 0)
    meter.record_token('invalid', 1)
    assert 'ttft_ms' not in meter.get_stats('invalid')

    meter.begin_session('zero')
    meter.set_pending_started_at('zero', clock.wall)
    assert 'ttft_ms' not in meter.get_stats('zero')
    meter.end_session('zero', 0)
    assert meter.get_stats('zero')['active'] == 2


def test_concurrent_stream_ttft_isolation(monkeypatch):
    meter, clock = _meter(monkeypatch)
    meter.begin_session('one')
    meter.begin_session('two')
    meter.set_pending_started_at('one', 1000.0)
    meter.set_pending_started_at('two', 1000.0)

    clock.wall += 0.125
    meter.record_token('one', 1)
    clock.wall += 0.375
    meter.record_token('two', 1)

    assert meter.get_stats('one')['ttft_ms'] == 125
    assert meter.get_stats('two')['ttft_ms'] == 500


def test_streaming_propagates_ttft_to_journal_done_and_message(monkeypatch):
    meter, clock = _meter(monkeypatch)
    stream_id = 'propagation'
    meter.begin_session(stream_id)
    meter.set_pending_started_at(stream_id, 1000.0)
    clock.wall += 0.250
    meter.record_token(stream_id, 1)

    metering_event = meter.get_stats(stream_id)
    journal = [metering_event]
    usage = {'ttft_ms': metering_event['ttft_ms']}
    assistant = {'role': 'assistant', '_firstTokenMs': meter.get_ttft_ms(stream_id)}
    final_session = {'messages': [assistant]}

    assert journal[0]['ttft_ms'] == 250
    assert usage['ttft_ms'] == 250
    assert assistant['_firstTokenMs'] == 250
    assert final_session['messages'][-1]['_firstTokenMs'] == 250
