import os as _os
SCRIPT = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "db_lastminute_monitor.py")
import importlib.util
from datetime import datetime
from zoneinfo import ZoneInfo
spec = importlib.util.spec_from_file_location("m", SCRIPT)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
B = ZoneInfo("Europe/Berlin"); IST = ZoneInfo("Asia/Kolkata")
def b(y,mo,d,h,mi): return datetime(y,mo,d,h,mi,3,tzinfo=B)
assert m.scheduled_now(b(2026,10,16,20,0)) and m.scheduled_now(b(2026,10,16,23,0))     # Fri 20,23
assert not m.scheduled_now(b(2026,10,16,19,0)) and not m.scheduled_now(b(2026,10,16,20,15))
assert m.scheduled_now(b(2026,10,17,0,0)) and m.scheduled_now(b(2026,10,17,0,30))      # Sat 00:00, 00:30
assert not m.scheduled_now(b(2026,10,17,0,15)) and not m.scheduled_now(b(2026,10,17,0,45))
assert len(m.RUN_SLOTS) == 21
assert m.scheduled_now(b(2026,10,17,1,0)) and m.scheduled_now(b(2026,10,17,12,0)) and not m.scheduled_now(b(2026,10,17,13,0))
assert m.scheduled_now(b(2026,10,17,18,0)) and not m.scheduled_now(b(2026,10,17,18,15))
assert m.scheduled_now(b(2026,10,18,9,0)) and m.scheduled_now(b(2026,10,18,18,0)) and not m.scheduled_now(b(2026,10,19,9,0))
# cron fires at 03:30 IST on Sat = 00:00 CEST Sat
ist = datetime(2026,10,17,3,30,2,tzinfo=IST)
assert m.scheduled_now(ist.astimezone(B))
print(m.describe_schedule(datetime(2026,10,16,18,0,tzinfo=B), datetime(2026,10,16,21,30,tzinfo=IST)))
# after clock change (CET): Sat 00:00 Berlin = 04:30 IST
print(m.describe_schedule(datetime(2026,10,30,18,0,tzinfo=B), datetime(2026,10,30,22,30,tzinfo=IST)))
print("SCHEDULE TESTS PASSED")
