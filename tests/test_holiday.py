"""scheduler/holiday.py — 静态节假日表与节后合并补发天数计算。"""

from datetime import date, timedelta

import yaml

from scheduler.holiday import backfill_days, load_holidays, main


def _write_holidays(tmp_path, days):
    path = tmp_path / "holidays.yaml"
    path.write_text(yaml.safe_dump({"holidays": [str(d) for d in days]}), encoding="utf-8")
    return path


class TestLoadHolidays:
    def test_missing_file_returns_empty(self, tmp_path):
        assert load_holidays(tmp_path / "nope.yaml") == set()

    def test_empty_file_returns_empty(self, tmp_path):
        path = tmp_path / "holidays.yaml"
        path.write_text("", encoding="utf-8")
        assert load_holidays(path) == set()

    def test_no_holidays_key_returns_empty(self, tmp_path):
        path = tmp_path / "holidays.yaml"
        path.write_text("other: [1, 2]\n", encoding="utf-8")
        assert load_holidays(path) == set()

    def test_parses_date_strings(self, tmp_path):
        path = _write_holidays(tmp_path, ["2026-01-01", "2026-05-01"])
        assert load_holidays(path) == {date(2026, 1, 1), date(2026, 5, 1)}


class TestBackfillDays:
    def test_today_is_holiday_returns_zero(self):
        today = date(2026, 5, 1)
        assert backfill_days(today, {today}) == 0

    def test_normal_day_returns_one(self):
        today = date(2026, 5, 10)
        holidays = {date(2026, 5, 1), date(2026, 5, 20)}
        assert backfill_days(today, holidays) == 1

    def test_first_day_after_holidays(self):
        # 5 天节假日（05-01 ~ 05-05），节后首日 05-06 返回 1 + 5 = 6
        holidays = {date(2026, 5, 1) + timedelta(days=i) for i in range(5)}
        assert backfill_days(date(2026, 5, 6), holidays) == 6

    def test_gap_breaks_streak(self):
        # 05-04 是节假日、05-05 不是，节后首日只数连续的 1 天
        holidays = {date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 4)}
        assert backfill_days(date(2026, 5, 5), holidays) == 2

    def test_capped_at_ten(self):
        # 往前连续 20 天都是节假日，封顶 10
        today = date(2026, 1, 31)
        holidays = {today - timedelta(days=i) for i in range(1, 21)}
        assert backfill_days(today, holidays) == 10

    def test_empty_holidays_returns_one(self):
        assert backfill_days(date(2026, 7, 28), set()) == 1


class TestSkipWeekends:
    # 2026-08-07 是周五，08-08 周六、08-09 周日、08-10 周一
    def test_default_weekend_not_skipped(self):
        assert backfill_days(date(2026, 8, 8), set()) == 1

    def test_saturday_returns_zero(self):
        assert backfill_days(date(2026, 8, 8), set(), skip_weekends=True) == 0

    def test_sunday_returns_zero(self):
        assert backfill_days(date(2026, 8, 9), set(), skip_weekends=True) == 0

    def test_monday_backfills_three_days(self):
        assert backfill_days(date(2026, 8, 10), set(), skip_weekends=True) == 3

    def test_monday_after_holiday_friday_backfills_four(self):
        # 周五是法定节假日 + 周末，周一返回 1 + 3 = 4
        assert backfill_days(date(2026, 8, 10), {date(2026, 8, 7)},
                             skip_weekends=True) == 4

    def test_weekend_streak_capped_at_ten(self):
        # 连续节假日接周末超过上限仍封顶 10
        today = date(2026, 8, 10)
        holidays = {today - timedelta(days=i) for i in range(3, 13)}
        assert backfill_days(today, holidays, skip_weekends=True) == 10


class TestCli:
    def test_main_prints_integer(self, capsys):
        main([])
        out = capsys.readouterr().out.strip()
        assert out.isdigit()

    def test_main_prints_zero_on_holiday(self, capsys, monkeypatch):
        # 把节假日表替换为"今天是节假日"，验证 CLI 输出 0
        monkeypatch.setattr("scheduler.holiday.load_holidays",
                            lambda path=None: {date.today()})
        main([])
        assert capsys.readouterr().out.strip() == "0"
