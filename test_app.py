"""Comprehensive verification suite for Employee Attendance & Analytics API."""
import unittest
from datetime import datetime, timezone, timedelta
from app.main import (
    IST,
    truncate_to_seconds,
    round_half_up,
    determine_attendance_date,
    get_shift_instants,
    compute_late_minutes,
    compute_overtime,
    compute_work_hours,
    is_half_day,
    count_working_days,
    get_month_range,
)


class TestBusinessRules(unittest.TestCase):
    def test_r1_instant_truncation(self):
        dt = datetime(2026, 7, 6, 9, 40, 0, 999999, tzinfo=timezone.utc)
        truncated = truncate_to_seconds(dt)
        self.assertEqual(truncated.microsecond, 0)
        self.assertEqual(truncated.second, 0)

    def test_r1_overnight_shift_date(self):
        # 22:00 to 06:00
        # Punch in at 21:55 IST on July 6 -> belongs to July 6
        p1 = datetime(2026, 7, 6, 21, 55, 0, tzinfo=IST).astimezone(timezone.utc)
        self.assertEqual(determine_attendance_date(p1, "22:00", "06:00"), "2026-07-06")

        # Punch in at 05:30 IST on July 7 -> earlier than shift_end 06:00 -> belongs to July 6
        p2 = datetime(2026, 7, 7, 5, 30, 0, tzinfo=IST).astimezone(timezone.utc)
        self.assertEqual(determine_attendance_date(p2, "22:00", "06:00"), "2026-07-06")

        # Punch in at 06:15 IST on July 7 -> after shift_end -> belongs to July 7
        p3 = datetime(2026, 7, 7, 6, 15, 0, tzinfo=IST).astimezone(timezone.utc)
        self.assertEqual(determine_attendance_date(p3, "22:00", "06:00"), "2026-07-07")

    def test_r2_late_grace_period(self):
        # Shift 09:30 IST
        s_utc, _ = get_shift_instants("2026-07-06", "09:30", "18:30")

        # 09:30:00 -> 0
        p0 = datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST).astimezone(timezone.utc)
        self.assertEqual(compute_late_minutes(p0, s_utc), 0)

        # 09:40:00 -> exactly 10 min -> 0 (grace period)
        p1 = datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST).astimezone(timezone.utc)
        self.assertEqual(compute_late_minutes(p1, s_utc), 0)

        # 09:40:01 -> strictly > 10:00 -> 10 minutes late
        p2 = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST).astimezone(timezone.utc)
        self.assertEqual(compute_late_minutes(p2, s_utc), 10)

        # 10:15:59 -> 45 minutes late
        p3 = datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST).astimezone(timezone.utc)
        self.assertEqual(compute_late_minutes(p3, s_utc), 45)

    def test_r3_overtime_threshold(self):
        # Shift end 18:30 IST
        _, e_utc = get_shift_instants("2026-07-06", "09:30", "18:30")

        # 18:59:59 (29 min 59 sec) -> 0
        o1 = datetime(2026, 7, 6, 18, 59, 59, tzinfo=IST).astimezone(timezone.utc)
        self.assertEqual(compute_overtime(o1, e_utc), 0)

        # 19:00:00 (30 min) -> 30
        o2 = datetime(2026, 7, 6, 19, 0, 0, tzinfo=IST).astimezone(timezone.utc)
        self.assertEqual(compute_overtime(o2, e_utc), 30)

        # 19:10:00 (40 min) -> 40
        o3 = datetime(2026, 7, 6, 19, 10, 0, tzinfo=IST).astimezone(timezone.utc)
        self.assertEqual(compute_overtime(o3, e_utc), 40)

    def test_r3_overtime_overnight(self):
        # Shift 22:00 - 06:00 for date 2026-07-06
        _, e_utc = get_shift_instants("2026-07-06", "22:00", "06:00")

        # Punch out 06:40 on July 7 -> 40 min overtime
        o1 = datetime(2026, 7, 7, 6, 40, 0, tzinfo=IST).astimezone(timezone.utc)
        self.assertEqual(compute_overtime(o1, e_utc), 40)

        # Punch out 06:20 on July 7 -> 20 min -> 0
        o2 = datetime(2026, 7, 7, 6, 20, 0, tzinfo=IST).astimezone(timezone.utc)
        self.assertEqual(compute_overtime(o2, e_utc), 0)

    def test_r4_work_hours_half_up(self):
        p_in = datetime(2026, 7, 6, 9, 0, 0, tzinfo=timezone.utc)
        p_out = datetime(2026, 7, 6, 18, 7, 30, tzinfo=timezone.utc)  # 9 hours 7.5 min = 9.125 hours
        self.assertEqual(compute_work_hours(p_in, p_out), 9.13)

        p_out2 = datetime(2026, 7, 6, 18, 7, 26, tzinfo=timezone.utc)  # ~9.1238 hours
        self.assertEqual(compute_work_hours(p_in, p_out2), 9.12)

    def test_r5_half_day(self):
        self.assertTrue(is_half_day(3.5))
        self.assertTrue(is_half_day(4.49))
        self.assertFalse(is_half_day(4.50))
        self.assertFalse(is_half_day(8.0))
        self.assertFalse(is_half_day(None))

    def test_r7_working_days(self):
        # July 2026: 31 days, starts Wednesday (July 1), ends Friday (July 31)
        # Weekdays: 23 days total
        self.assertEqual(count_working_days("2026-01-01", "2026-07"), 23)

        # Joiner on July 13 (Monday): 3 weeks = 15 weekdays
        self.assertEqual(count_working_days("2026-07-13", "2026-07"), 15)

        # Joiner after month: 0
        self.assertEqual(count_working_days("2026-08-01", "2026-07"), 0)


if __name__ == "__main__":
    unittest.main()

