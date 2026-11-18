import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from pydantic import ValidationError

from app.core import security
from app.core.config import Settings, settings


@pytest.mark.unit
def test_password_hashing():
    h = security.hash_password("s3cret-pass")
    assert h != "s3cret-pass" and h.startswith("$2")
    assert security.verify_password("s3cret-pass", h)
    assert not security.verify_password("wrong", h)
    assert not security.verify_password("anything", None)          # unknown user: still false
    long = "x" * 100
    assert security.verify_password(long, security.hash_password(long))   # >72 bytes does not crash


@pytest.mark.unit
def test_token_roundtrip_expiry_and_tampering():
    uid = uuid.uuid4()
    token, lifetime = security.create_access_token(uid)
    assert lifetime == settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    assert security.decode_access_token(token) == uid
    old, _ = security.create_access_token(uid, now=datetime.now(timezone.utc) - timedelta(days=30))
    assert security.decode_access_token(old) is None
    assert security.decode_access_token(token[:-2] + "xx") is None
    no_exp = jwt.encode({"sub": str(uid)}, settings.JWT_SECRET, algorithm="HS256")
    assert security.decode_access_token(no_exp) is None            # expiry is mandatory
    not_uuid = jwt.encode({"sub": "1", "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
                          settings.JWT_SECRET, algorithm="HS256")
    assert security.decode_access_token(not_uuid) is None


@pytest.mark.unit
def test_production_refuses_the_default_or_a_short_secret():
    with pytest.raises(ValidationError):
        Settings(ENVIRONMENT="production")                         # default secret
    with pytest.raises(ValidationError):
        Settings(JWT_SECRET="short")
    assert Settings(ENVIRONMENT="production", JWT_SECRET="x" * 40).JWT_SECRET == "x" * 40
