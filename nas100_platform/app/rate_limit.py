"""Shared rate limiter — applied to auth endpoints to slow down brute
force / credential-stuffing attempts. Uses in-memory storage, which is
fine for a single-instance deployment; if you ever run multiple server
instances behind a load balancer, point this at Redis instead
(slowapi supports it via storage_uri)."""
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
