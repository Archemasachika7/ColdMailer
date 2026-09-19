"""
Server-side verification of the Supabase session token sent by the frontend.

Historically every route trusted a plain `user_id` field supplied by the
client with no verification at all — anyone could impersonate any user.
This module replaces that: the frontend must send

    Authorization: Bearer <supabase access_token>

and every route resolves the *real* user id from that token via Supabase's
own /auth/v1/user endpoint (through the supabase-py client), never from a
client-supplied field. This works regardless of whether the project signs
JWTs with a shared secret or the newer asymmetric scheme, since Supabase
itself validates the token.
"""
from functools import wraps
from flask import request, jsonify, g


def init_auth(supabase_client):
    """Call once at startup with the shared supabase Client instance."""
    global _supabase
    _supabase = supabase_client


_supabase = None


def _extract_bearer_token():
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    return header[len("Bearer "):].strip()


def require_auth(fn):
    """Decorator: verifies the bearer token, sets g.user_id, 401s otherwise."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = _extract_bearer_token()
        if not token:
            return jsonify({"status": "error", "message": "Missing Authorization header."}), 401
        try:
            resp = _supabase.auth.get_user(token)
            user = getattr(resp, "user", None)
            if not user or not getattr(user, "id", None):
                return jsonify({"status": "error", "message": "Invalid or expired session."}), 401
            g.user_id = user.id
        except Exception:
            return jsonify({"status": "error", "message": "Invalid or expired session."}), 401
        return fn(*args, **kwargs)
    return wrapper
