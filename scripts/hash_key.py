"""Generate an API key and the sha256 digest to put in API_KEYS.

Usage: python scripts/hash_key.py [existing-key]
"""
import hashlib
import secrets
import sys

key = sys.argv[1] if len(sys.argv) > 1 else secrets.token_urlsafe(32)
print(f"API key (give to the user): {key}")
print(f"Digest (put in API_KEYS):   {hashlib.sha256(key.encode()).hexdigest()}")