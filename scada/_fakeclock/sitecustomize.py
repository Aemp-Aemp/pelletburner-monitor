"""Fake wall clock for the tests: run_tests.py --clock HH:MM puts this folder on PYTHONPATH and sets PB_FAKE_CLOCK.
Python then starts with the local time shifted to the nearest HH:MM (the clock keeps running from there).
Only the wall clock moves (time.time, localtime, datetime.now, date.today, ...); monotonic, sleep and file
times stay real. Used to find tests that only pass at some times of day."""
import datetime as _dt
import os
import time as _time

_hm = os.environ.get("PB_FAKE_CLOCK", "")
if _hm:
    _h, _m = (int(x) for x in _hm.split(":"))
    _real_time, _real_time_ns, _real_localtime = _time.time, _time.time_ns, _time.localtime
    _now = _real_time()
    _t = _real_localtime(_now)
    _target = _time.mktime((_t.tm_year, _t.tm_mon, _t.tm_mday, _h, _m, 0, 0, 0, -1))
    _shift = min((_target + d * 86400 - _now for d in (-1, 0, 1)), key=abs)   # nearest HH:MM, within 12 h

    def _fake():
        return _real_time() + _shift

    _time.time = _fake
    _time.time_ns = lambda: _real_time_ns() + int(_shift * 1e9)
    _orig = {n: getattr(_time, n) for n in ("localtime", "gmtime", "ctime")}
    for _n, _f in _orig.items():
        setattr(_time, _n, (lambda f: lambda secs=None: f(_fake() if secs is None else secs))(_f))
    _strftime, _asctime = _time.strftime, _time.asctime
    _time.strftime = lambda fmt, t=None: _strftime(fmt, _time.localtime() if t is None else t)
    _time.asctime = lambda t=None: _asctime(_time.localtime() if t is None else t)

    _RD, _RDT = _dt.date, _dt.datetime

    class _DateMeta(type):
        def __instancecheck__(cls, o): return isinstance(o, _RD)
        def __subclasscheck__(cls, c): return issubclass(c, _RD)

    class _DTMeta(type):
        def __instancecheck__(cls, o): return isinstance(o, _RDT)
        def __subclasscheck__(cls, c): return issubclass(c, _RDT)

    class date(_RD, metaclass=_DateMeta):
        @classmethod
        def today(cls):
            d = _RD.fromtimestamp(_fake())
            return cls(d.year, d.month, d.day)

    class datetime(_RDT, metaclass=_DTMeta):
        @classmethod
        def now(cls, tz=None):
            d = _RDT.fromtimestamp(_fake(), tz)
            return cls(d.year, d.month, d.day, d.hour, d.minute, d.second, d.microsecond, d.tzinfo, fold=d.fold)

        @classmethod
        def today(cls):
            return cls.now()

        @classmethod
        def utcnow(cls):
            d = _RDT.fromtimestamp(_fake(), _dt.timezone.utc).replace(tzinfo=None)
            return cls(d.year, d.month, d.day, d.hour, d.minute, d.second, d.microsecond)

    date.__module__ = datetime.__module__ = "datetime"
    _dt.date, _dt.datetime = date, datetime
