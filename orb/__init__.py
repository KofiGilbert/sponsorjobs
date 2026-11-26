"""The Orb: SponsorJobs' floating assistant (orb/rules.py).

Local only. ``rules`` holds the pure decisions (which nudge, when, how often, the chat limits)
so tests can pin them; ``store`` keeps the small SQLite tables they read and write. Nothing here
calls the network or the model; ui/app.py glues it to the app's real data.
"""
