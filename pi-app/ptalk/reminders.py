"""Reminder storage + due-checking (nhắc lịch / nhắc uống thuốc).

Stored as JSON in the user data dir so it survives app restarts. Times are
local "HH:MM". A reminder can repeat daily or on selected weekdays.
"""
import json
import os
import time
import uuid
from dataclasses import dataclass, asdict, field


def default_path():
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "ptalk-signature", "reminders.json")


@dataclass
class Reminder:
    id: str
    time: str                 # "HH:MM"
    label: str
    kind: str = "med"         # "med" (uống thuốc) | "appt" (lịch hẹn)
    enabled: bool = True
    days: list = field(default_factory=list)   # [] = hằng ngày; else weekday ints 0=Mon


class ReminderStore:
    def __init__(self, path=None):
        self.path = path or default_path()
        self.items = []
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                self.items = [Reminder(**d) for d in json.load(f)]
        except Exception:
            self.items = []
        self._sort()

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump([asdict(r) for r in self.items], f, ensure_ascii=False, indent=2)

    def _sort(self):
        self.items.sort(key=lambda r: r.time)

    def add(self, time_str, label, kind="med", days=None):
        r = Reminder(id=uuid.uuid4().hex[:8], time=time_str, label=label,
                     kind=kind, enabled=True, days=days or [])
        self.items.append(r)
        self._sort()
        self.save()
        return r

    def remove(self, rid):
        self.items = [r for r in self.items if r.id != rid]
        self.save()

    def toggle(self, rid):
        for r in self.items:
            if r.id == rid:
                r.enabled = not r.enabled
        self.save()

    def due_now(self, now=None):
        """Return reminders whose time == current HH:MM today and are enabled."""
        lt = time.localtime(now)
        hhmm = "%02d:%02d" % (lt.tm_hour, lt.tm_min)
        weekday = lt.tm_wday  # 0=Mon
        out = []
        for r in self.items:
            if not r.enabled or r.time != hhmm:
                continue
            if r.days and weekday not in r.days:
                continue
            out.append(r)
        return out
