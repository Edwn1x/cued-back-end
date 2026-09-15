"""Third-party OAuth integrations (Google Calendar, Strava, bCourses, wearables).

Shared plumbing lives here: one `integrations` table (models.Integration), one
Fernet crypto box (crypto.py), one single-use connect-token family (tokens.py),
one refresh/revoke client framework + provider registry (base.py), and the Flask
routes for the connect flow (routes.py). Provider modules (gcal.py, strava.py,
bcourses.py) register themselves with base.PROVIDERS on import; each part of the
spec ships behind its own flag.
"""
