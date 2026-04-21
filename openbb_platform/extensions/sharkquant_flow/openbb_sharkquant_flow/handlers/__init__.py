"""Locally-computed endpoint handlers.

Each module here replaces a corresponding upstream
analytics.openbb.superquant.com route with an implementation backed by
our own data sources (Theta Terminal v3 for options, FMP for spot,
Postgres sharkquant provider for fundamentals).

Routes are registered in openbb_sharkquant_flow.router so the externally
visible path stays /api/v1/sharkquant_flow/<name> regardless of whether
the endpoint proxies upstream or computes locally.
"""
