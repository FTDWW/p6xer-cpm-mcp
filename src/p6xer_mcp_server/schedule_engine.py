"""
ScheduleEngine: deterministic CPM (Critical Path Method) engine.

Phase A (MVP):
  - FS/SS/FF/SF relationships + lag (hours)
  - Duration from target_drtn_hr_cnt / remain_drtn_hr_cnt
  - Single calendar (8h/day default, or project calendar day_hr_cnt)
  - TK_Complete anchoring to actual dates
  - Milestones (TT_Mile / TT_FinMile): zero-duration
  - Forward pass (ES/EF), backward pass (LS/LF), total float

Phase B (later):
  - CalendarData working-hour patterns, exceptions
  - Dual constraints (cstr_type / cstr_type2)
  - TK_Active actual-start anchoring + remaining duration
"""

from __future__ import annotations

import datetime as dt
from typing import Any


# P6 epoch for date storage
P6_EPOCH = dt.datetime(1899, 12, 30)

# Hour constants
HOURS_PER_DAY = 8.0


class CalendarHelper:
    """Working-time calculator backed by real P6 calendar data.

    Parses Calendar objects from PyP6XER (which expose CalendarData via
    .working_days, .working_hours, .exceptions, .day_hr_cnt) and answers
    the core scheduling question: *given a start datetime and N working
    hours, what is the finish datetime?*
    """

    def __init__(
        self,
        calendars: list | None = None,
        default_hours_per_day: float = HOURS_PER_DAY,
    ) -> None:
        self._default_hpd = default_hours_per_day
        # Maps clndr_id -> parsed working-time definition
        self._calendars: dict[int, dict] = {}
        if calendars:
            for cal in calendars:
                cid = getattr(cal, "clndr_id", None)
                if cid is None:
                    continue
                self._calendars[cid] = self._parse_calendar(cal)
        # Fallback for an unknown clndr_id: prefer a REAL calendar from the
        # file (P6 resolves a dangling calendar to a real/default one). Only
        # when no real calendar exists do we fabricate the Mon-Fri default.
        self._fallback_cal: dict | None = (
            self._calendars[next(iter(self._calendars))] if self._calendars else None
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def hours_per_day(self) -> float:
        return self._default_hpd

    def day_hr_cnt(self, clndr_id: int | None) -> float:
        """Working hours in a standard day for the given calendar."""
        if clndr_id is not None and clndr_id in self._calendars:
            return self._calendars[clndr_id]["day_hr_cnt"]
        return self._default_hpd

    def _default_cal(self) -> dict:
        """Fallback calendar for an unknown/absent clndr_id.

        Prefer the FIRST REAL calendar loaded from the file — matches how P6
        resolves a dangling calendar id to a real/default one (this project is
        7-day×8h, so the old fabricated Mon-Fri fallback made the engine diverge
        from P6).  Only when no real calendar exists do we fabricate a standard
        Mon-Fri 8h/day one (08:00-12:00 + 13:00-17:00; ``work_times`` empty so
        ``_working_hours_for_day`` generates intervals from ``day_hr_cnt``).
        """
        if self._fallback_cal is not None:
            return self._fallback_cal
        return {
            "day_hr_cnt": self._default_hpd,
            "working_days": {0: True, 1: True, 2: True, 3: True, 4: True, 5: False, 6: False},
            "work_times": {},
            "exceptions": set(),
        }

    def add_hours(
        self, start: dt.datetime, hours: float, clndr_id: int | None = None,
    ) -> dt.datetime:
        """Add *hours* of working time to *start*, returning the finish datetime.

        Negative hours steps backward in working time (used by backward pass).
        """
        if hours == 0:
            return start
        cal = self._calendars.get(clndr_id) if clndr_id is not None else None
        if cal is None:
            return self._add_hours_fallback(start, hours)
        return self._add_hours_calendar(start, hours, cal)

    def ensure_working_start(
        self, dtm: dt.datetime, clndr_id: int | None = None,
    ) -> dt.datetime:
        """Ensure *dtm* is at the start of a working interval.

        If *dtm* is at or past the end of all intervals for its date,
        advance to the first working interval start of the next working day.
        If *dtm* is within a working interval (after start, before end), return it as-is.
        This is used to normalize activity start dates (ES/LS) computed from
        predecessor finish dates, matching P6's behaviour where activities
        always start at the beginning of a working period.
        """
        cal = self._calendars.get(clndr_id) if clndr_id is not None else None
        if cal is None:
            cal = self._default_cal()

        d = dtm.date()
        t = dtm.time()
        safety = 0
        while safety < 10000:
            safety += 1
            intervals = self._working_hours_for_day(d, cal)
            if not intervals:
                d += dt.timedelta(days=1)
                t = dt.time(0, 0)
                continue

            for s_time, f_time in intervals:
                if t >= f_time:
                    continue
                if t <= s_time:
                    return dt.datetime.combine(d, s_time)
                # t is within working interval (s_time < t < f_time)
                return dtm

            # t is past all intervals for this date, try next day
            d += dt.timedelta(days=1)
            t = dt.time(0, 0)

        return dtm  # safety fallback

    def ensure_working_finish(
        self, dtm: dt.datetime, clndr_id: int | None = None,
    ) -> dt.datetime:
        """Ensure *dtm* is at the **end** of a working interval.

        Mirror of ``ensure_working_start``.  If *dtm* is before the first
        interval start on its date, step back to the last interval end of the
        previous working day.  If *dtm* is within a working interval, return
        it as-is.  If *dtm* is past all intervals for its date, leave it at
        the last interval end of that day.

        Used to normalise LF dates so they land at end-of-working-day
        (matching P6's convention that Late Finish sits at the close of a
        working period), while still allowing genuinely-late projects whose
        LF exceeds the project finish date.
        """
        cal = self._calendars.get(clndr_id) if clndr_id is not None else None
        if cal is None:
            cal = self._default_cal()

        d = dtm.date()
        t = dtm.time()
        safety = 0
        while safety < 10000:
            safety += 1
            intervals = self._working_hours_for_day(d, cal)
            if not intervals:
                # Non-working day — step back to previous day
                d -= dt.timedelta(days=1)
                t = dt.time(23, 59, 59)
                continue

            # Check if t is before the first interval → step back
            first_start = intervals[0][0]
            if t < first_start:
                d -= dt.timedelta(days=1)
                t = dt.time(23, 59, 59)
                continue

            # Check each interval
            for s_time, f_time in intervals:
                if t <= s_time:
                    # At or before interval start → interval hasn't started yet
                    continue
                if t <= f_time:
                    # Within working interval → return as-is
                    return dtm
                # t > f_time: past this interval, check next

            # t is past all intervals for this date — leave at last interval end
            last_end = intervals[-1][1]
            return dt.datetime.combine(d, last_end)

        return dtm  # safety fallback

    def days_to_hours(self, days: float, clndr_id: int | None = None) -> float:
        hpd = self.day_hr_cnt(clndr_id)
        return days * hpd

    def hours_to_days(self, hours: float, clndr_id: int | None = None) -> float:
        hpd = self.day_hr_cnt(clndr_id)
        return hours / hpd if hpd > 0 else 0.0

    def working_hours_between(
        self, start: dt.datetime, end: dt.datetime, clndr_id: int | None = None,
    ) -> float:
        """Count working hours between two datetimes using the given calendar.

        Returns a signed float: positive when end > start, negative when end < start.
        Used for Total Float (= working hours between ES and LS) and
        Free Float (= working hours between EF and successor's ES).
        """
        if start == end:
            return 0.0
        if end < start:
            return -self.working_hours_between(end, start, clndr_id)

        cal = self._calendars.get(clndr_id) if clndr_id is not None else None
        if cal is None:
            cal = self._default_cal()

        total = 0.0
        current_date = start.date()
        current_dt = start

        safety = 0
        while current_date <= end.date() and safety < 10000:
            safety += 1
            intervals = self._working_hours_for_day(current_date, cal)
            if not intervals:
                current_date += dt.timedelta(days=1)
                current_dt = dt.datetime.combine(current_date, dt.time(0, 0))
                continue

            for s_time, f_time in intervals:
                seg_start = dt.datetime.combine(current_date, s_time)
                seg_end = dt.datetime.combine(current_date, f_time)
                if current_dt >= seg_end:
                    continue
                if current_dt > seg_start:
                    seg_start = current_dt
                if seg_end > end:
                    seg_end = end
                total += (seg_end - seg_start).total_seconds() / 3600.0
                current_dt = seg_end
                if current_dt >= end:
                    return total

            current_date += dt.timedelta(days=1)
            current_dt = dt.datetime.combine(current_date, dt.time(0, 0))

        return total

    # ------------------------------------------------------------------
    # Calendar parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_calendar(cal) -> dict:
        """Extract working-time rules from a PyP6XER Calendar object."""
        info: dict = {
            "day_hr_cnt":   float(getattr(cal, "day_hr_cnt", 0) or 8.0),
            "week_hr_cnt":  float(getattr(cal, "week_hr_cnt", 0) or 40.0),
            "working_days": {},       # {0..6: True/False}
            "work_times":   {},       # {0..6: [(start_time, end_time), ...]}
            "exceptions":   set(),    # set of date objects
        }

        wd = getattr(cal, "working_days", None) or {}
        # P6 CalendarData.get_days() returns {"1": True, "2": True, ...}
        # where 1=Sunday, 7=Saturday (matching P6 internal convention)
        # Python weekday(): 0=Monday, 6=Sunday
        p6_to_py = {1: 6, 2: 0, 3: 1, 4: 2, 5: 3, 6: 4, 7: 5}  # Sunday..Saturday
        for k, v in wd.items():
            py_dow = p6_to_py.get(int(k))
            if py_dow is not None:
                info["working_days"][py_dow] = bool(v)

        wh = getattr(cal, "working_hours", None) or []
        dow_name = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3,
                     "Friday": 4, "Saturday": 5, "Sunday": 6}
        for day_entry in wh:
            d = dow_name.get(day_entry.get("DayOfWeek", ""))
            if d is None:
                continue
            times = []
            for wt in day_entry.get("WorkTimes", []):
                s = wt.get("Start")
                f = wt.get("Finish")
                if s and f:
                    times.append((s, f))
            info["work_times"][d] = times

        exc = getattr(cal, "exceptions", None) or []
        for e in exc:
            if isinstance(e, dt.datetime):
                info["exceptions"].add(e.date())
            elif isinstance(e, dt.date):
                info["exceptions"].add(e)

        return info

    # ------------------------------------------------------------------
    # Working-time arithmetic (calendar-aware)
    # ------------------------------------------------------------------

    def _is_working_day(self, d: dt.date, cal: dict) -> bool:
        if d in cal["exceptions"]:
            return False
        wd = cal["working_days"]
        if wd:
            return wd.get(d.weekday(), True)
        # Default: Mon-Fri working
        return d.weekday() < 5

    def _working_hours_for_day(self, d: dt.date, cal: dict) -> list[tuple[dt.time, dt.time]]:
        """Return sorted list of (start_time, end_time) working intervals for *d*."""
        if not self._is_working_day(d, cal):
            return []
        wt = cal["work_times"].get(d.weekday())
        if wt:
            return [(s, f) for s, f in wt]
        # Default work times: P6 standard 08:00-12:00 + 13:00-17:00.
        # Morning block is 4h, afternoon block carries the remaining hours.
        day_hr = cal["day_hr_cnt"]
        if day_hr <= 4:
            # Short day: single block starting at 08:00
            return [(dt.time(8, 0), dt.time(min(23, 8 + int(day_hr)), 0))]
        else:
            morning = (dt.time(8, 0), dt.time(12, 0))  # 08:00-12:00 (4h)
            afternoon_hrs = int(day_hr) - 4
            afternoon_end = min(23, 13 + afternoon_hrs)
            afternoon = (dt.time(13, 0), dt.time(afternoon_end, 0))
            if afternoon_hrs > 0:
                return [morning, afternoon]
            return [morning]

    def _add_hours_calendar(
        self, start: dt.datetime, hours: float, cal: dict,
    ) -> dt.datetime:
        """Step through working intervals adding *hours* of productive time."""
        if hours == 0:
            return start

        if hours > 0:
            return self._add_positive_hours(start, hours, cal)
        else:
            return self._add_negative_hours(start, -hours, cal)

    def _add_positive_hours(
        self, start: dt.datetime, hours: float, cal: dict,
    ) -> dt.datetime:
        remaining = hours
        current_date = start.date()
        current_dt = start

        # Find the first working interval on or after current_dt
        intervals = self._working_hours_for_day(current_date, cal)
        # Advance through days
        safety = 0
        while remaining > 1e-9 and safety < 10000:
            safety += 1
            if not intervals:
                current_date += dt.timedelta(days=1)
                current_dt = dt.datetime.combine(current_date, dt.time(0, 0))
                intervals = self._working_hours_for_day(current_date, cal)
                continue

            for s_time, f_time in intervals:
                seg_start = dt.datetime.combine(current_date, s_time)
                seg_end = dt.datetime.combine(current_date, f_time)
                if current_dt >= seg_end:
                    continue
                if current_dt < seg_start:
                    current_dt = seg_start
                avail = (seg_end - current_dt).total_seconds() / 3600.0
                if remaining <= avail:
                    return current_dt + dt.timedelta(hours=remaining)
                remaining -= avail
                current_dt = seg_end

            current_date += dt.timedelta(days=1)
            current_dt = dt.datetime.combine(current_date, dt.time(0, 0))
            intervals = self._working_hours_for_day(current_date, cal)

        return current_dt

    def _add_negative_hours(
        self, start: dt.datetime, hours: float, cal: dict,
    ) -> dt.datetime:
        """Step backward through working intervals subtracting *hours* of productive time."""
        remaining = hours
        current_date = start.date()
        current_dt = start

        safety = 0
        while remaining > 1e-9 and safety < 10000:
            safety += 1
            intervals = self._working_hours_for_day(current_date, cal)
            if not intervals:
                current_date -= dt.timedelta(days=1)
                current_dt = dt.datetime.combine(current_date, dt.time(23, 59, 59))
                intervals = self._working_hours_for_day(current_date, cal)
                continue

            # Iterate intervals in reverse
            for s_time, f_time in reversed(intervals):
                seg_start = dt.datetime.combine(current_date, s_time)
                seg_end = dt.datetime.combine(current_date, f_time)
                if current_dt <= seg_start:
                    continue
                if current_dt > seg_end:
                    current_dt = seg_end
                avail = (current_dt - seg_start).total_seconds() / 3600.0
                if remaining <= avail:
                    return current_dt - dt.timedelta(hours=remaining)
                remaining -= avail
                current_dt = seg_start

            current_date -= dt.timedelta(days=1)
            current_dt = dt.datetime.combine(current_date, dt.time(23, 59, 59))

        return current_dt

    def _add_hours_fallback(self, start: dt.datetime, hours: float) -> dt.datetime:
        """Simple Mon-Fri arithmetic when no calendar data is available."""
        if hours == 0:
            return start
        cal = self._default_cal()
        if hours > 0:
            return self._add_positive_hours(start, hours, cal)
        else:
            return self._add_negative_hours(start, -hours, cal)


# ---------------------------------------------------------------------------
# Relation type helpers
# ---------------------------------------------------------------------------

# PR_FS: predecessor finish -> successor start
# PR_SS: predecessor start -> successor start
# PR_FF: predecessor finish -> successor finish
# PR_SF: predecessor start -> successor finish


def _successor_start_from_pred(
    pred_es: dt.datetime, pred_ef: dt.datetime, dur_succ: float,
    pred_type: str, lag_hr: float, cal: CalendarHelper,
    succ_clndr_id: int | None = None,
) -> dt.datetime:
    """Compute the successor's ES given one predecessor's dates."""
    if pred_type == "PR_FS":
        anchor = cal.add_hours(pred_ef, lag_hr, succ_clndr_id)
    elif pred_type == "PR_SS":
        anchor = cal.add_hours(pred_es, lag_hr, succ_clndr_id)
    elif pred_type == "PR_FF":
        anchor = cal.add_hours(pred_ef, lag_hr, succ_clndr_id)
        # FF: successor finish = pred_ef + lag, so successor start = finish - dur
        return cal.add_hours(anchor, -dur_succ, succ_clndr_id)
    elif pred_type == "PR_SF":
        anchor = cal.add_hours(pred_es, lag_hr, succ_clndr_id)
        return cal.add_hours(anchor, -dur_succ, succ_clndr_id)
    else:  # default FS
        anchor = cal.add_hours(pred_ef, lag_hr, succ_clndr_id)
    return anchor


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class ScheduleEngine:
    """CPM forward/backward pass over a ScheduleGraph."""

    def __init__(self, graph, calendar_helper: CalendarHelper | None = None) -> None:
        self.graph = graph
        self.cal = calendar_helper or CalendarHelper()

    # ------------------------------------------------------------------
    # Duration helpers
    # ------------------------------------------------------------------

    def _get_duration_hr(self, node: dict) -> float:
        """Return the scheduling duration in hours."""
        task_type = node.get("task_type", "")
        status = node.get("status_code", "")
        # Milestones have zero duration
        if task_type in ("TT_Mile", "TT_FinMile"):
            return 0.0
        # In progress: use remaining duration
        if status == "TK_Active":
            rem = node.get("remain_drtn_hr_cnt")
            if rem is not None and rem > 0:
                return float(rem)
        # Not started or fallback: use target/planned duration
        tgt = node.get("target_drtn_hr_cnt")
        if tgt is not None and tgt > 0:
            return float(tgt)
        return 0.0

    def _cal_id(self, node: dict) -> int | None:
        return node.get("clndr_id")

    # ------------------------------------------------------------------
    # Constraint helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_start_constraint(
        es: dt.datetime, node: dict, cal: "CalendarHelper", clndr_id: int | None,
    ) -> dt.datetime:
        """Adjust ES based on start-side constraints. Returns (possibly modified) ES."""
        cstr_type = node.get("cstr_type", "") or ""
        cstr_date = node.get("cstr_date")
        cstr_type2 = node.get("cstr_type2", "") or ""
        cstr_date2 = node.get("cstr_date2")

        ct_upper = cstr_type.upper()
        ct2_upper = cstr_type2.upper()

        # Mandatory / hard start constraints
        if ct_upper in ("CS_MANDSTART", "CS_MSO", "CS_MS"):
            d = _parse_dt(cstr_date)
            if d:
                es = d
        elif ct_upper in ("CS_MSOA", "CS_SOA", "CS_MEA"):
            d = _parse_dt(cstr_date)
            if d:
                es = max(es, d)

        # Second constraint
        if ct2_upper in ("CS_MANDSTART", "CS_MSO", "CS_MS"):
            d = _parse_dt(cstr_date2)
            if d:
                es = d
        elif ct2_upper in ("CS_MSOA", "CS_SOA", "CS_MEA"):
            d = _parse_dt(cstr_date2)
            if d:
                es = max(es, d)

        return es

    @staticmethod
    def _apply_finish_constraint(
        ef: dt.datetime, node: dict, cal: "CalendarHelper", clndr_id: int | None,
    ) -> dt.datetime:
        """Adjust EF based on finish-side constraints. Returns (possibly modified) EF."""
        cstr_type = node.get("cstr_type", "") or ""
        cstr_date = node.get("cstr_date")
        cstr_type2 = node.get("cstr_type2", "") or ""
        cstr_date2 = node.get("cstr_date2")

        ct_upper = cstr_type.upper()
        ct2_upper = cstr_type2.upper()

        # In the forward pass, finish constraints affect EF (early dates).
        # Mandatory Finish: EF forced to constraint date.
        # Finish On or After: EF cannot be before constraint (EF = max(EF, constraint)).
        # Finish On or Before constraints (CS_MEOB, CS_FNOB, etc.) do NOT
        # affect the forward pass — they only constrain the backward pass (LF).
        if ct_upper in ("CS_MANDFIN", "CS_MFO", "CS_MF", "CS_MEO"):
            d = _parse_dt(cstr_date)
            if d:
                ef = d
        elif ct_upper == "CS_MEOA":
            d = _parse_dt(cstr_date)
            if d:
                ef = max(ef, d)

        if ct2_upper in ("CS_MANDFIN", "CS_MFO", "CS_MF", "CS_MEO"):
            d = _parse_dt(cstr_date2)
            if d:
                ef = d
        elif ct2_upper == "CS_MEOA":
            d = _parse_dt(cstr_date2)
            if d:
                ef = max(ef, d)

        return ef

    @staticmethod
    def _apply_start_constraint_backward(
        ls: dt.datetime, node: dict, cal: "CalendarHelper", clndr_id: int | None,
    ) -> dt.datetime:
        """Adjust LS based on start-side constraints for backward pass.

        In the backward pass, start constraints limit how late an activity can start:
        - Mandatory start: LS = constraint date (must start exactly then)
        - Start On / Start On or After: LS cannot be before constraint
        """
        cstr_type = node.get("cstr_type", "") or ""
        cstr_date = node.get("cstr_date")
        cstr_type2 = node.get("cstr_type2", "") or ""
        cstr_date2 = node.get("cstr_date2")

        ct_upper = cstr_type.upper()
        ct2_upper = cstr_type2.upper()

        # Mandatory / hard start constraints: LS forced to constraint date
        if ct_upper in ("CS_MANDSTART", "CS_MSO"):
            d = _parse_dt(cstr_date)
            if d:
                ls = d
        elif ct_upper in ("CS_MS", "CS_MSOA", "CS_SOA", "CS_MEA"):
            d = _parse_dt(cstr_date)
            if d:
                ls = max(ls, d)

        if ct2_upper in ("CS_MANDSTART", "CS_MSO"):
            d = _parse_dt(cstr_date2)
            if d:
                ls = d
        elif ct2_upper in ("CS_MS", "CS_MSOA", "CS_SOA", "CS_MEA"):
            d = _parse_dt(cstr_date2)
            if d:
                ls = max(ls, d)
        elif ct2_upper == "CS_MSOB":
            d = _parse_dt(cstr_date2)
            if d:
                ls = min(ls, d)  # Start No Later Than: cap late start

        # CS_MSOB (Start On or Before): late start cannot be later than constraint
        if ct_upper == "CS_MSOB":
            d = _parse_dt(cstr_date)
            if d:
                ls = min(ls, d)

        return ls

    @staticmethod
    def _apply_finish_constraint_backward(
        lf: dt.datetime, node: dict, cal: "CalendarHelper", clndr_id: int | None,
    ) -> dt.datetime:
        """Adjust LF based on finish-side constraints for backward pass.

        In the backward pass, finish constraints limit how late an activity can finish:
        - Mandatory finish: LF = constraint date (must finish exactly then)
        - Finish On / Finish On or Before: LF cannot be after constraint
        """
        cstr_type = node.get("cstr_type", "") or ""
        cstr_date = node.get("cstr_date")
        cstr_type2 = node.get("cstr_type2", "") or ""
        cstr_date2 = node.get("cstr_date2")

        ct_upper = cstr_type.upper()
        ct2_upper = cstr_type2.upper()

        def _norm(d: dt.datetime | None) -> dt.datetime | None:
            """Normalise a pure-date (midnight) finish constraint to the START of
            that working day (08:00).  `ensure_working_finish` then maps 08:00 →
            16:00 SAME day (end of the constraint day).  Without this, a date-only
            "finish no later than D" parsed as midnight 00:00 is treated as "before
            the working day has begun" and steps back to D−1 16:00 — every finish
            milestone converges one day too early (calendar boundary bug)."""
            if d is not None and d.hour == 0 and d.minute == 0 and d.second == 0:
                return d.replace(hour=8, minute=0, second=0, microsecond=0)
            return d

        # In the backward pass, finish constraints affect LF (late dates).
        # Mandatory Finish: LF forced to constraint date (must finish exactly then).
        # Finish On or Before: LF cannot be after constraint (LF = min(LF, constraint)).
        # Note: CS_MEOA (Finish On or After) does NOT constrain the backward pass
        # — it says "cannot finish before", which only constrains early dates (EF).
        if ct_upper in ("CS_MANDFIN", "CS_MFO", "CS_MF", "CS_MEO"):
            d = _norm(_parse_dt(cstr_date))
            if d:
                lf = d
        elif ct_upper in ("CS_FNOB", "CS_FOB", "CS_MEB", "CS_MEOB"):
            d = _norm(_parse_dt(cstr_date))
            if d:
                lf = min(lf, d)

        if ct2_upper in ("CS_MANDFIN", "CS_MFO", "CS_MF", "CS_MEO"):
            d = _norm(_parse_dt(cstr_date2))
            if d:
                lf = d
        elif ct2_upper in ("CS_FNOB", "CS_FOB", "CS_MEB", "CS_MEOB"):
            d = _norm(_parse_dt(cstr_date2))
            if d:
                lf = min(lf, d)

        return lf

    # ------------------------------------------------------------------
    # Forward pass
    # ------------------------------------------------------------------

    def forward_pass(self, project_start: dt.datetime) -> dict[int, dt.datetime]:
        """
        Compute ES/EF for all activities.

        Returns dicts: es_map, ef_map keyed by task_id.
        Activities with no predecessors start at project_start.
        TK_Complete activities are anchored to their actual dates.
        """
        project_start = _parse_dt(project_start) or dt.datetime(2024, 1, 1)
        es_map: dict[int, dt.datetime] = {}
        ef_map: dict[int, dt.datetime] = {}

        # Build in-degree and adjacency
        in_degree: dict[int, int] = {}
        pred_edges: dict[int, list[dict]] = {}  # task_id -> list of incoming edge dicts
        for tid in self.graph.nodes:
            in_degree[tid] = 0
            pred_edges[tid] = []

        for edge in self.graph.edges:
            succ = edge["task_id"]
            pred = edge["pred_task_id"]
            if succ in self.graph.nodes and pred in self.graph.nodes:
                in_degree[succ] += 1
                pred_edges[succ].append(edge)

        # Kahn topological traversal
        from collections import deque
        queue: deque[int] = deque()

        for tid in self.graph.nodes:
            node = self.graph.nodes[tid]
            status = node.get("status_code", "")
            if status == "TK_Complete":
                # Anchor to actual dates
                act_start = node.get("act_start_date")
                act_end = node.get("act_end_date")
                if act_start:
                    es_map[tid] = _parse_dt(act_start) or project_start
                else:
                    es_map[tid] = project_start
                if act_end:
                    ef_map[tid] = _parse_dt(act_end) or es_map[tid]
                else:
                    ef_map[tid] = es_map[tid]
                queue.append(tid)
            elif in_degree[tid] == 0:
                queue.append(tid)

        while queue:
            tid = queue.popleft()
            node = self.graph.nodes[tid]

            # Compute ES if not already set (TK_Complete)
            clndr_id = self._cal_id(node)
            if tid not in es_map:
                if not pred_edges[tid]:
                    es_map[tid] = self.cal.ensure_working_start(project_start, clndr_id)
                else:
                    # ES = max of predecessor-driven start dates
                    candidate_starts = []
                    for edge in pred_edges[tid]:
                        pred_id = edge["pred_task_id"]
                        if pred_id not in es_map:
                            continue
                        pred_es = es_map[pred_id]
                        pred_ef = ef_map.get(pred_id, pred_es)
                        dur_succ = self._get_duration_hr(node)
                        pred_type = edge.get("pred_type", "PR_FS")
                        lag_hr = float(edge.get("lag_hr_cnt", 0) or 0)
                        s = _successor_start_from_pred(
                            pred_es, pred_ef, dur_succ, pred_type, lag_hr,
                            self.cal, clndr_id,
                        )
                        candidate_starts.append(s)
                    es_map[tid] = max(candidate_starts) if candidate_starts else self.cal.ensure_working_start(project_start, clndr_id)

                # Normalize ES to start of a working interval
                es_pre = es_map[tid]
                es_map[tid] = self.cal.ensure_working_start(es_map[tid], clndr_id)
                # Zero-duration milestones: P6 allows ES at the interval
                # end boundary (e.g. 16:00).  ensure_working_start treats
                # t >= f_time as "past interval" which pushes milestones
                # to the next day, but P6 keeps them at the boundary.
                dur_succ = self._get_duration_hr(node)
                if dur_succ == 0 and es_map[tid] > es_pre:
                    es_map[tid] = es_pre

                # Apply start constraints
                if tid in es_map:
                    es_map[tid] = self._apply_start_constraint(
                        es_map[tid], node, self.cal, self._cal_id(node),
                    )
                    # Re-normalize only if the constraint date was date-only
                    # (midnight).  Constraint dates that already carry a real
                    # time component (e.g. 16:00 from P6's own constraint
                    # storage) are already at valid schedule boundaries.
                    if (es_map[tid].hour == 0 and es_map[tid].minute == 0
                            and es_map[tid].second == 0):
                        es_map[tid] = self.cal.ensure_working_start(
                            es_map[tid], clndr_id,
                        )

            # Compute EF
            if tid not in ef_map:
                dur = self._get_duration_hr(node)
                ef_map[tid] = self.cal.add_hours(es_map[tid], dur, clndr_id)
                # Apply finish constraints to EF (and back-adjust ES if needed)
                constrained_ef = self._apply_finish_constraint(
                    ef_map[tid], node, self.cal, self._cal_id(node),
                )
                if constrained_ef != ef_map[tid]:
                    ef_map[tid] = constrained_ef
                    # If finish constraint pulls EF, adjust ES and re-normalize
                    es_map[tid] = self.cal.add_hours(constrained_ef, -dur, clndr_id)
                    if dur > 0:
                        es_map[tid] = self.cal.ensure_working_start(es_map[tid], clndr_id)

            # Enqueue successors
            for edge in self.graph.edges:
                if edge["pred_task_id"] != tid:
                    continue
                succ = edge["task_id"]
                if succ not in self.graph.nodes:
                    continue
                if self.graph.nodes[succ].get("status_code") == "TK_Complete":
                    continue
                in_degree[succ] -= 1
                if in_degree[succ] == 0:
                    queue.append(succ)

        return es_map, ef_map

    # ------------------------------------------------------------------
    # Backward pass
    # ------------------------------------------------------------------

    def backward_pass(
        self, es_map: dict[int, dt.datetime], ef_map: dict[int, dt.datetime],
        project_finish: dt.datetime,
    ) -> tuple[dict[int, dt.datetime], dict[int, dt.datetime]]:
        """Compute LS/LF for all activities. Returns ls_map, lf_map."""
        ls_map: dict[int, dt.datetime] = {}
        lf_map: dict[int, dt.datetime] = {}

        # Build out-degree and successor edges
        out_degree: dict[int, int] = {}
        succ_edges: dict[int, list[dict]] = {}
        for tid in self.graph.nodes:
            out_degree[tid] = 0
            succ_edges[tid] = []

        for edge in self.graph.edges:
            pred = edge["pred_task_id"]
            if pred in self.graph.nodes and edge["task_id"] in self.graph.nodes:
                out_degree[pred] += 1
                succ_edges[pred].append(edge)

        # Reverse topological order (start from leaves)
        from collections import deque
        queue: deque[int] = deque()

        for tid in self.graph.nodes:
            node = self.graph.nodes[tid]
            status = node.get("status_code", "")
            if status == "TK_Complete":
                # Anchor to actual dates
                act_start = node.get("act_start_date")
                act_end = node.get("act_end_date")
                if act_end:
                    lf_map[tid] = _parse_dt(act_end) or project_finish
                elif tid in ef_map:
                    lf_map[tid] = ef_map[tid]
                else:
                    lf_map[tid] = project_finish
                if act_start:
                    ls_map[tid] = _parse_dt(act_start) or lf_map[tid]
                elif tid in es_map:
                    ls_map[tid] = es_map[tid]
                else:
                    ls_map[tid] = lf_map[tid]
                queue.append(tid)
            elif out_degree[tid] == 0:
                if tid in ef_map:
                    lf_map[tid] = max(ef_map[tid], project_finish)
                else:
                    lf_map[tid] = project_finish
                dur = self._get_duration_hr(node)
                ls_map[tid] = self.cal.add_hours(lf_map[tid], -dur, self._cal_id(node))
                ls_pre = ls_map[tid]
                ls_map[tid] = self.cal.ensure_working_start(ls_map[tid], self._cal_id(node))
                if dur == 0 and ls_map[tid] > ls_pre:
                    ls_map[tid] = ls_pre
                queue.append(tid)

        while queue:
            tid = queue.popleft()
            node = self.graph.nodes[tid]
            dur = self._get_duration_hr(node)
            clndr_id = self._cal_id(node)

            if tid not in lf_map:
                if not succ_edges[tid]:
                    lf_map[tid] = project_finish
                else:
                    candidate_finishes = []
                    for edge in succ_edges[tid]:
                        succ_id = edge["task_id"]
                        if succ_id not in ls_map:
                            continue
                        succ_ls = ls_map[succ_id]
                        succ_node = self.graph.nodes.get(succ_id, {})
                        dur_succ = self._get_duration_hr(succ_node)
                        succ_clndr_id = self._cal_id(succ_node)
                        pred_type = edge.get("pred_type", "PR_FS")
                        lag_hr = float(edge.get("lag_hr_cnt", 0) or 0)
                        if pred_type == "PR_FS":
                            candidate = self.cal.add_hours(succ_ls, -lag_hr, succ_clndr_id)
                            candidate_finishes.append(candidate)
                        elif pred_type == "PR_SS":
                            candidate_ls = self.cal.add_hours(succ_ls, -lag_hr, succ_clndr_id)
                            candidate_finishes.append(self.cal.add_hours(candidate_ls, dur, clndr_id))
                        elif pred_type == "PR_FF":
                            if succ_id in lf_map:
                                candidate = self.cal.add_hours(lf_map[succ_id], -lag_hr, succ_clndr_id)
                                candidate_finishes.append(candidate)
                            else:
                                candidate_finishes.append(self.cal.add_hours(succ_ls, dur_succ - lag_hr, succ_clndr_id))
                        elif pred_type == "PR_SF":
                            if succ_id in lf_map:
                                candidate_ls = self.cal.add_hours(lf_map[succ_id], -lag_hr, succ_clndr_id)
                                candidate_finishes.append(self.cal.add_hours(candidate_ls, dur, clndr_id))
                            else:
                                candidate_finishes.append(self.cal.add_hours(succ_ls, dur_succ - lag_hr + dur, succ_clndr_id))
                        else:
                            candidate_finishes.append(self.cal.add_hours(succ_ls, -lag_hr, succ_clndr_id))
                    lf_map[tid] = min(candidate_finishes) if candidate_finishes else project_finish

            if tid not in ls_map:
                ls_map[tid] = self.cal.add_hours(lf_map[tid], -dur, clndr_id)
                # Normalize LS to start of a working interval
                ls_pre = ls_map[tid]
                ls_map[tid] = self.cal.ensure_working_start(ls_map[tid], clndr_id)
                if dur == 0 and ls_map[tid] > ls_pre:
                    ls_map[tid] = ls_pre

            # Apply constraints in backward pass
            # 1. Finish constraints on LF
            constrained_lf = self._apply_finish_constraint_backward(
                lf_map[tid], node, self.cal, clndr_id,
            )
            if constrained_lf != lf_map[tid]:
                # Finish constraints (CS_MEOB / CS_MEO / CS_MANDFIN ...) carry a
                # date-only midnight (the target date for a pure-date "finish no
                # later than").  Normalise LF to end-of-working-day FIRST, then derive
                # LS from the normalised LF.  Otherwise a 0-duration finish
                # milestone keeps LS at the raw constraint midnight while LF is
                # later pulled back by ensure_working_finish → LS≠LF and Total
                # Float (ES↔LS) is wrong for every finish milestone.
                lf_map[tid] = self.cal.ensure_working_finish(constrained_lf, clndr_id)
                # Recalculate LS from constrained LF
                ls_map[tid] = self.cal.add_hours(lf_map[tid], -dur, clndr_id)
                ls_pre = ls_map[tid]
                ls_map[tid] = self.cal.ensure_working_start(ls_map[tid], clndr_id)
                if dur == 0 and ls_map[tid] > ls_pre:
                    ls_map[tid] = ls_pre

            # 2. Start constraints on LS
            constrained_ls = self._apply_start_constraint_backward(
                ls_map[tid], node, self.cal, clndr_id,
            )
            if constrained_ls != ls_map[tid]:
                ls_map[tid] = constrained_ls
                # Normalize — constraint dates may be date-only (midnight)
                # while P6 constraint dates carry a real time component.
                if (ls_map[tid].hour == 0 and ls_map[tid].minute == 0
                        and ls_map[tid].second == 0):
                    ls_map[tid] = self.cal.ensure_working_start(
                        ls_map[tid], clndr_id,
                    )
                # Recalculate LF from constrained LS
                lf_map[tid] = self.cal.add_hours(ls_map[tid], dur, clndr_id)

            # Enqueue predecessors
            for edge in self.graph.edges:
                if edge["task_id"] != tid:
                    continue
                pred = edge["pred_task_id"]
                if pred not in self.graph.nodes:
                    continue
                if self.graph.nodes[pred].get("status_code") == "TK_Complete":
                    continue
                out_degree[pred] -= 1
                if out_degree[pred] == 0:
                    queue.append(pred)

        # Normalize all LF dates to end-of-working-interval (P6 convention).
        # P6 places Late Finish at the close of a working period; our raw
        # successor-driven and project_finish-based LF values may land at
        # interval starts.  This pass ensures they sit at interval ends
        # while preserving genuinely-late LF values that exceed the project
        # finish date.
        for tid in lf_map:
            clndr_id = self._cal_id(self.graph.nodes.get(tid, {}))
            lf_map[tid] = self.cal.ensure_working_finish(lf_map[tid], clndr_id)

        return ls_map, lf_map

    # ------------------------------------------------------------------
    # Full CPM run
    # ------------------------------------------------------------------

    def run(
        self, project_start: dt.datetime, project_finish: dt.datetime | None = None,
    ) -> dict:
        """
        Run full CPM forward + backward pass.

        Returns dict with keys: es, ef, ls, lf, total_float_hr, free_float_hr
        each mapping task_id -> value.
        """
        project_start = _parse_dt(project_start) or dt.datetime(2024, 1, 1)
        if project_finish is not None:
            project_finish = _parse_dt(project_finish)
        es_map, ef_map = self.forward_pass(project_start)

        if project_finish is None:
            # Default: latest EF + some buffer
            if ef_map:
                project_finish = max(ef_map.values())
            else:
                project_finish = project_start
            # A "finish no later than / on / mandatory" constraint on ANY
            # activity lets the project run until that date.  Extend
            # project_finish so a finish-constrained no-successor milestone
            # (e.g. a final milestone whose EF lands before its finish
            # constraint) gets LF = its constraint and a correct positive
            # float instead of being clamped to the max early finish (which
            # would report TF=0 while the plan actually completes early).
            for _tid, node in self.graph.nodes.items():
                for cstr_type, cstr_date in (
                    (node.get("cstr_type") or "", node.get("cstr_date")),
                    (node.get("cstr_type2") or "", node.get("cstr_date2")),
                ):
                    ct = cstr_type.upper()
                    if ct in ("CS_MANDFIN", "CS_MFO", "CS_MF", "CS_MEO",
                              "CS_FNOB", "CS_FOB", "CS_MEB", "CS_MEOB"):
                        d = _parse_dt(cstr_date)
                        if d and d > project_finish:
                            project_finish = d

        ls_map, lf_map = self.backward_pass(es_map, ef_map, project_finish)

        # Compute floats
        tf_map: dict[int, float] = {}
        ff_map: dict[int, float] = {}
        for tid in self.graph.nodes:
            cal_id = self._cal_id(self.graph.nodes[tid])
            if tid in es_map and tid in ls_map:
                tf_map[tid] = self.cal.working_hours_between(
                    es_map[tid], ls_map[tid], cal_id,
                )
            else:
                tf_map[tid] = 0.0

            # Free float: min(ES of successors) - EF of this activity
            succ_es_list = []
            for edge in self.graph.edges:
                if edge["pred_task_id"] == tid:
                    succ_id = edge["task_id"]
                    if succ_id in es_map:
                        succ_es_list.append(es_map[succ_id])
            if succ_es_list and tid in ef_map:
                min_succ_es = min(succ_es_list)
                ff_map[tid] = self.cal.working_hours_between(
                    ef_map[tid], min_succ_es, cal_id,
                )
            else:
                ff_map[tid] = tf_map.get(tid, 0.0)

        return {
            "es": {tid: str(v) for tid, v in es_map.items()},
            "ef": {tid: str(v) for tid, v in ef_map.items()},
            "ls": {tid: str(v) for tid, v in ls_map.items()},
            "lf": {tid: str(v) for tid, v in lf_map.items()},
            "total_float_hr": tf_map,
            "free_float_hr": ff_map,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_dt(val: Any) -> dt.datetime | None:
    """Parse a string/datetime to a datetime object."""
    if val is None:
        return None
    if isinstance(val, dt.datetime):
        return val
    if isinstance(val, dt.date):
        return dt.datetime.combine(val, dt.time(0, 0))
    try:
        return dt.datetime.fromisoformat(str(val))
    except (ValueError, TypeError):
        pass
    # Try common formats
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(str(val), fmt)
        except (ValueError, TypeError):
            continue
    return None
