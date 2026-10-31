"""The applicant brain: one local memory of the person (docs/applicant-brain.md).

* ``keys``: the key registry (types, sensitivity, single or multi);
* ``store``: ``data/applicant.db`` and its guards;
* ``library``: ``BrainLibrary`` (the answer library over the brain) and the prefs answers;
* ``migrate``: the one-time migration from the legacy stores, with dry run, verify, mirror and
  rollback;
* ``redact``: the guard that keeps self-identification and legal values out of model payloads;
* ``facts``: the writers and readers of brain-only facts (Phase 3);
* ``mapper``: new wordings of known questions, one batched model call per page, cached (Phase 4);
* ``expiry``: when a saved value expired or does not fit the job (Phase 4).
"""
