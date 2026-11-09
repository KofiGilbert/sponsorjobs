"""The SponsorJobs GitHub OAuth App, used for "Sign in with GitHub" (device flow).

The Client ID is PUBLIC by design: GitHub's device flow is made for apps that cannot keep a
secret (desktop apps, CLIs), so it never uses a client secret. Nothing secret lives here, and
nothing secret may ever be added (tests/test_github_device.py checks).

While CLIENT_ID is empty, Settings falls back to the advanced token box. Fill it in with the
Client ID from the company's OAuth App (github.com, Settings, Developer settings, OAuth Apps,
with "Enable Device Flow" ticked), or set GITHUB_OAUTH_CLIENT_ID in the environment.
"""

from __future__ import annotations

import os

CLIENT_ID = ""
SCOPE = "repo"


def client_id() -> str:
    """The Client ID to use: the environment override first, then the shipped constant."""
    return (os.environ.get("GITHUB_OAUTH_CLIENT_ID") or CLIENT_ID or "").strip()
