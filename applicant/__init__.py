"""The applicant brain: one local memory of the person (docs/applicant-brain.md).

* ``keys``: the key registry (types, sensitivity, single or multi);
* ``store``: ``data/applicant.db`` and its guards;
* ``library``: ``BrainLibrary`` (the answer library over the brain) and the prefs answers;
* ``migrate``: the one-time migration from the legacy stores, with dry run, verify, mirror and
  rollback;
* ``redact``: the guard that keeps self-identification and legal values out of model payloads.
"""
