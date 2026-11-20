"""Sacred Knowledge YouTube uploader.

Private-by-default resumable uploads to the Sacred Knowledge channel
via YouTube Data API v3, coordinated through a shared Neon PostgreSQL
job queue. No real upload is possible while the kill switch is OFF
(the default) or while the owner's global stop order stands.
"""

__version__ = "0.1.0"
