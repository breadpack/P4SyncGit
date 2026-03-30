"""status_reporter 유틸 함수 단위 테스트."""

from p4gitsync.cli.status_reporter import format_duration, format_table


class TestFormatDuration:
    def test_seconds(self):
        assert format_duration(45) == "45초"

    def test_zero(self):
        assert format_duration(0) == "0초"

    def test_minutes(self):
        assert format_duration(125) == "2분 5초"

    def test_hours_and_minutes(self):
        assert format_duration(3661) == "1시간 1분"

    def test_days_and_hours(self):
        assert format_duration(90061) == "1일 1시간"

    def test_exact_hour(self):
        assert format_duration(3600) == "1시간 0분"

    def test_exact_day(self):
        assert format_duration(86400) == "1일 0시간"


class TestFormatTable:
    def test_basic_table(self):
        headers = ["이름", "상태"]
        rows = [["svc-a", "실행중"], ["svc-b", "중지"]]
        result = format_table(headers, rows)
        assert "svc-a" in result
        assert "svc-b" in result
        assert "이름" in result

    def test_empty_rows(self):
        headers = ["A", "B"]
        result = format_table(headers, [])
        assert "A" in result
        assert "B" in result

    def test_column_alignment(self):
        headers = ["Name", "Value"]
        rows = [["short", "1"], ["a-longer-name", "2"]]
        result = format_table(headers, rows)
        lines = result.strip().splitlines()
        # header + separator + 2 data rows
        assert len(lines) == 4
