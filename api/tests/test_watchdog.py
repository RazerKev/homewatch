from app.alerts import LogSink, NtfySink
from app.config import Settings
from app.watchdog import build_sinks

DB = "postgresql+psycopg://user:pass@localhost:5432/test"

def test_without_ntfy_url_alerts_only_go_to_the_log():
    sinks = build_sinks(Settings(database_url=DB))
    assert [type(sink) for sink in sinks] == [LogSink]

def test_with_ntfy_url_alerts_also_go_to_the_phone():
    sinks = build_sinks(
        Settings(database_url=DB, ntfy_url="https://ntfy.sh/homewatch-test")
    )
    assert [type(sink) for sink in sinks] == [LogSink, NtfySink]
