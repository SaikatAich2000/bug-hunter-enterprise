"""Out-of-band application jobs.

Each module exposes ``main()`` and runs via ``python -m app.jobs.<name>``,
invoked by an external scheduler (cron etc.), not from the request path.
"""
