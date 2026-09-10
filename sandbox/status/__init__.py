"""The status API and dashboard.

What: a FastAPI process that reads the registry, the provider, Temporal, and
the object store, and serves one page that shows the fleet, the leases, the
jobs, the sessions, and the control-plane event feed.
Why: the registry is the system's real state, but a demo needs to be watched,
not queried. Every view here is a join the operator would otherwise do by
hand across four places.
Production: an internal dashboard app over the same API.
"""
