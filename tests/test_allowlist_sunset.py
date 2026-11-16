"""An AUTO entry with a vendor-announced end date turns ASSISTED on that date by itself."""
import datetime as dt

from submit import allowlist as A


def test_recruitee_is_auto_until_its_token_deadline(monkeypatch):
    rec = next(e for e in A.REGISTRY if e.ats == "Recruitee")
    assert rec.auto_until == "2027-02-10"

    class Day(dt.date):
        today_value = dt.date(2027, 2, 9)

        @classmethod
        def today(cls):
            return cls.today_value

    monkeypatch.setattr(dt, "date", Day)
    assert rec.auto_eligible and "recruitee.com" in A.auto_hosts()
    Day.today_value = dt.date(2027, 2, 10)
    assert not rec.auto_eligible and "recruitee.com" not in A.auto_hosts()
