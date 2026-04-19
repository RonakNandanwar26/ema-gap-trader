"""Trader daemon — standalone FastAPI service that owns all Trader threads.

Streamlit talks to this service over loopback HTTP; see RemoteSession in
remote_session.py.
"""
