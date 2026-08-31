from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple
from sqlalchemy.orm import Session
from .database import RefreshTokenRecord, UserAccount

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "akansha_dev_super_secret_jwt_key_2026")
ACCESS_TOKEN_EXPIRE_MINUTES = 15
REFRESH_TOKEN_EXPIRE_DAYS = 7
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15


class AuthService:
    """
    Enterprise-Grade Modular Auth & Security Engine.
    Handles password hashing, JWT creation/validation, refresh token rotation,
    brute-force lockout, rate limiting, and password reset flows.
    """

    @staticmethod
    def hash_password(password: str) -> str:
        salt = secrets.token_bytes(16)
        hashed = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100000)
        return f"{base64.b64encode(salt).decode('utf-8')}${base64.b64encode(hashed).decode('utf-8')}"

    @staticmethod
    def verify_password(password: str, stored_hash: str) -> bool:
        if not stored_hash or "$" not in stored_hash:
            return False
        try:
            salt_b64, hash_b64 = stored_hash.split("$", 1)
            salt = base64.b64decode(salt_b64.encode("utf-8"))
            expected_hash = base64.b64decode(hash_b64.encode("utf-8"))
            computed_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100000)
            return secrets.compare_digest(expected_hash, computed_hash)
        except Exception:
            return False

    @staticmethod
    def evaluate_password_strength(password: str) -> Dict[str, Any]:
        score = 0
        feedback = []

        if len(password) >= 8:
            score += 25
        else:
            feedback.append("At least 8 characters long")

        if re.search(r"[A-Z]", password):
            score += 25
        else:
            feedback.append("Include uppercase letters (A-Z)")

        if re.search(r"[0-9]", password):
            score += 25
        else:
            feedback.append("Include numbers (0-9)")

        if re.search(r"[^A-Za-z0-9]", password):
            score += 25
        else:
            feedback.append("Include special symbols (!@#$...)")

        if score <= 25:
            label = "Weak"
        elif score <= 50:
            label = "Fair"
        elif score <= 75:
            label = "Strong"
        else:
            label = "Excellent"

        return {"score": score, "label": label, "is_valid": score >= 50, "feedback": feedback}

    @staticmethod
    def generate_token_str() -> str:
        return secrets.token_urlsafe(32)

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @classmethod
    def create_access_token(cls, user_id: int, email: str) -> str:
        payload = f"sub:{user_id}|email:{email}|exp:{int((datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)).timestamp())}"
        signature = hmac.new(SECRET_KEY.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        raw_jwt = f"{base64.b64encode(payload.encode('utf-8')).decode('utf-8')}.{signature}"
        return raw_jwt

    @classmethod
    def verify_access_token(cls, raw_jwt: str) -> Optional[Dict[str, Any]]:
        if not raw_jwt or "." not in raw_jwt:
            return None
        try:
            payload_b64, signature = raw_jwt.split(".", 1)
            payload = base64.b64decode(payload_b64.encode("utf-8")).decode("utf-8")
            expected_sig = hmac.new(SECRET_KEY.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
            if not secrets.compare_digest(signature, expected_sig):
                return None

            parts = dict(item.split(":", 1) for item in payload.split("|"))
            exp = int(parts.get("exp", 0))
            if datetime.now(timezone.utc).timestamp() > exp:
                return None

            return {"user_id": int(parts["sub"]), "email": parts["email"]}
        except Exception:
            return None

    @classmethod
    def create_refresh_token(cls, db: Session, user_id: int, device_info: str = "Unknown Device") -> str:
        raw_token = cls.generate_token_str()
        token_hash = cls.hash_token(raw_token)
        expires_at = datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)

        record = RefreshTokenRecord(
            user_id=user_id,
            token_hash=token_hash,
            device_info=device_info,
            expires_at=expires_at,
            is_revoked=False,
        )
        db.add(record)
        db.commit()
        return raw_token

    @classmethod
    def rotate_refresh_token(cls, db: Session, raw_token: str, device_info: str = "Unknown Device") -> Optional[Tuple[str, str, int]]:
        token_hash = cls.hash_token(raw_token)
        record = db.query(RefreshTokenRecord).filter(RefreshTokenRecord.token_hash == token_hash).first()

        if not record or record.is_revoked or record.expires_at < datetime.utcnow():
            return None

        # Revoke old refresh token (Rotation)
        record.is_revoked = True
        db.commit()

        # Lookup user
        user = db.query(UserAccount).filter(UserAccount.id == record.user_id).first()
        if not user:
            return None

        new_access_token = cls.create_access_token(user.id, user.email)
        new_refresh_token = cls.create_refresh_token(db, user.id, device_info)

        return new_access_token, new_refresh_token, user.id

    @classmethod
    def is_account_locked(cls, user: UserAccount) -> bool:
        if user.locked_until and user.locked_until > datetime.utcnow():
            return True
        return False

    @classmethod
    def record_failed_login(cls, db: Session, user: UserAccount) -> None:
        user.failed_login_attempts += 1
        if user.failed_login_attempts >= MAX_FAILED_ATTEMPTS:
            user.locked_until = datetime.utcnow() + timedelta(minutes=LOCKOUT_MINUTES)
        db.commit()

    @classmethod
    def reset_failed_logins(cls, db: Session, user: UserAccount) -> None:
        user.failed_login_attempts = 0
        user.locked_until = None
        db.commit()

    @classmethod
    def generate_email_verification_token(cls, db: Session, user: UserAccount) -> str:
        token = secrets.token_urlsafe(32)
        user.verification_token = token
        db.commit()
        return token

    @classmethod
    def generate_password_reset_token(cls, db: Session, user: UserAccount) -> str:
        token = secrets.token_urlsafe(32)
        user.reset_token = token
        user.reset_token_expires_at = datetime.utcnow() + timedelta(hours=1)
        db.commit()
        return token

    @classmethod
    def create_passkey_challenge(cls, user_id: int, email: str) -> Dict[str, Any]:
        challenge = secrets.token_urlsafe(32)
        return {
            "challenge": challenge,
            "rp": {"name": "Akansha AI OS", "id": "localhost"},
            "user": {"id": str(user_id), "name": email, "displayName": email.split("@")[0]},
            "pubKeyCredParams": [{"type": "public-key", "alg": -7}],
            "authenticatorSelection": {"authenticatorAttachment": "platform", "userVerification": "preferred"},
            "timeout": 60000,
        }

    _OTP_STORE: Dict[str, Tuple[str, datetime]] = {}

    @classmethod
    def generate_otp_code(cls, email: str) -> str:
        otp = f"{secrets.randbelow(900000) + 100000}"
        expires_at = datetime.utcnow() + timedelta(minutes=10)
        cls._OTP_STORE[email.lower()] = (otp, expires_at)
        return otp

    @classmethod
    def verify_otp_code(cls, email: str, code: str) -> bool:
        email_clean = email.lower()
        if email_clean not in cls._OTP_STORE:
            return False
        stored_otp, expires_at = cls._OTP_STORE[email_clean]
        if datetime.utcnow() > expires_at:
            del cls._OTP_STORE[email_clean]
            return False
        if secrets.compare_digest(stored_otp, code.strip()):
            del cls._OTP_STORE[email_clean]
            return True
        return False
