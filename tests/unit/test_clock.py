"""current_datetime formatting."""

from datetime import UTC, datetime, timedelta, timezone

from aichat.tools import clock

PDT = timezone(timedelta(hours=-7), "Pacific Daylight Time")
IST = timezone(timedelta(hours=5, minutes=30), "India Standard Time")


def test_describe_format():
    info = clock.describe(datetime(2026, 9, 30, 14, 5, 9, 123456, tzinfo=PDT))
    assert info == {
        "iso": "2026-09-30T14:05:09-07:00",
        "weekday": "Wednesday",
        "timezone": "Pacific Daylight Time",
        "utc_offset": "UTC-07:00",
    }


def test_offset_with_minutes_and_utc():
    assert clock.format_utc_offset(datetime(2026, 1, 1, tzinfo=IST)) == "UTC+05:30"
    assert clock.format_utc_offset(datetime(2026, 1, 1, tzinfo=UTC)) == "UTC+00:00"


def test_describe_now_is_aware():
    info = clock.describe()
    assert info["weekday"] in {
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    }  # fmt: skip
    assert info["utc_offset"].startswith("UTC")
    assert datetime.fromisoformat(info["iso"]).tzinfo is not None
    assert info["timezone"]


def test_naive_datetime_is_treated_as_local():
    info = clock.describe(datetime(2026, 9, 30, 12, 0, 0))
    assert info["iso"].startswith("2026-09-30T12:00:00")
    assert info["weekday"] == "Wednesday"


async def test_run_content():
    result = await clock.run(datetime(2026, 9, 30, 14, 5, 9, tzinfo=PDT))
    assert result.ok
    assert result.content.splitlines() == [
        "Local time: 2026-09-30T14:05:09-07:00",
        "Weekday: Wednesday",
        "Timezone: Pacific Daylight Time (UTC-07:00)",
    ]
    assert result.summary == "Wednesday 2026-09-30 14:05"
