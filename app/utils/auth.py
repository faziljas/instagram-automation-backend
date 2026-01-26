import os
from datetime import datetime, timedelta
from passlib.context import CryptContext
from jose import jwt, JWTError

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

JWT_SECRET = os.getenv("JWT_SECRET", "your-secret-key-here-change-in-production")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = 30

# Supabase JWT configuration
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def create_access_token(data: dict, expires_delta: timedelta = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return encoded_jwt


def verify_token(token: str) -> dict:
    """
    Verify JWT token using the application's own JWT secret.
    Used for legacy authentication tokens.
    """
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except JWTError:
        return None


def verify_supabase_token(token: str) -> dict:
    """
    Verify Supabase JWT token using Supabase JWT secret.
    Returns the decoded payload if valid, None otherwise.
    """
    if not SUPABASE_JWT_SECRET:
        print("[AUTH] WARNING: SUPABASE_JWT_SECRET is not set. Cannot verify Supabase tokens.")
        return None
    
    try:
        # Supabase tokens use HS256 algorithm with "authenticated" as audience
        payload = jwt.decode(
            token,
            SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            audience="authenticated",
            options={"verify_aud": True}
        )
        print(f"[AUTH] Successfully verified Supabase token for user: {payload.get('email', 'unknown')}")
        return payload
    except JWTError as e:
        print(f"[AUTH] Failed to verify Supabase token: {str(e)}")
        return None


def verify_token_flexible(token: str) -> dict:
    """
    Try to verify token as Supabase token first, then fall back to app token.
    Returns the decoded payload if valid, None otherwise.
    """
    # Try Supabase token first
    payload = verify_supabase_token(token)
    if payload:
        return payload
    
    # Fall back to app token
    return verify_token(token)


def get_user_id_from_token(token: str, db_session=None) -> int:
    """
    Extract user ID from token (Supabase or legacy).
    For Supabase tokens, looks up user by email in database.
    For legacy tokens, extracts user ID directly from token.
    
    Args:
        token: JWT token string
        db_session: Optional database session for Supabase token lookup
    
    Returns:
        User ID (int) if found, None otherwise
    """
    # Try Supabase token first
    supabase_payload = verify_supabase_token(token)
    if supabase_payload and db_session:
        # Supabase tokens contain email, not backend user ID
        # Look up user by email
        from app.models.user import User
        email = supabase_payload.get("email")
        if email:
            user = db_session.query(User).filter(
                User.email.ilike(email)
            ).first()
            if user:
                return user.id
    
    # Fall back to legacy token
    legacy_payload = verify_token(token)
    if legacy_payload:
        user_id = legacy_payload.get("sub")
        if user_id:
            try:
                return int(user_id)
            except (ValueError, TypeError):
                return None
    
    return None
