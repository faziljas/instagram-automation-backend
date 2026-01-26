import os
import requests
import base64
from datetime import datetime, timedelta
from passlib.context import CryptContext
from jose import jwt, JWTError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.backends import default_backend
import json

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


def get_supabase_jwks():
    """
    Fetch Supabase JWKS (JSON Web Key Set) for ECC P-256 public keys.
    """
    if not SUPABASE_URL:
        return None
    
    try:
        jwks_url = f"{SUPABASE_URL}/.well-known/jwks.json"
        response = requests.get(jwks_url, timeout=5)
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        print(f"[AUTH] Failed to fetch JWKS: {str(e)}")
    return None


def base64url_decode(data):
    """
    Decode Base64URL encoded string.
    """
    # Add padding if needed
    padding = 4 - len(data) % 4
    if padding != 4:
        data += "=" * padding
    # Replace URL-safe characters
    data = data.replace("-", "+").replace("_", "/")
    return base64.b64decode(data)


def get_public_key_from_jwks(jwks, kid):
    """
    Extract public key from JWKS for a given key ID.
    """
    if not jwks or not kid:
        return None
    
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            try:
                # Convert JWK to PEM format for ECC P-256
                # Base64URL decode the x and y coordinates
                x_bytes = base64url_decode(key["x"])
                y_bytes = base64url_decode(key["y"])
                
                # Reconstruct the public key
                public_numbers = ec.EllipticCurvePublicNumbers(
                    int.from_bytes(x_bytes, "big"),
                    int.from_bytes(y_bytes, "big"),
                    ec.SECP256R1()
                )
                public_key = public_numbers.public_key(default_backend())
                
                # Serialize to PEM
                pem = public_key.public_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PublicFormat.SubjectPublicKeyInfo
                )
                return pem.decode()
            except Exception as e:
                print(f"[AUTH] Error extracting public key from JWKS: {str(e)}")
                return None
    
    return None


def verify_supabase_token(token: str) -> dict:
    """
    Verify Supabase JWT token - supports both HS256 (Legacy) and ES256 (ECC P-256).
    Returns the decoded payload if valid, None otherwise.
    """
    if not token:
        return None
    
    try:
        # Decode header to check algorithm
        header = jwt.get_unverified_header(token)
        alg = header.get("alg")
        kid = header.get("kid")  # Key ID for ECC keys
        
        print(f"[AUTH] Token algorithm: {alg}, Key ID: {kid}")
        
        # Try ES256 (ECC P-256) first if key ID is present
        if alg == "ES256" and kid:
            if not SUPABASE_URL:
                print("[AUTH] SUPABASE_URL not set. Cannot verify ES256 tokens.")
                return None
            
            # Fetch JWKS and get public key
            jwks = get_supabase_jwks()
            if not jwks:
                print("[AUTH] Failed to fetch JWKS for ES256 verification")
                return None
            
            public_key_pem = get_public_key_from_jwks(jwks, kid)
            if not public_key_pem:
                print(f"[AUTH] Public key not found for kid: {kid}")
                return None
            
            # Verify with ES256
            try:
                payload = jwt.decode(
                    token,
                    public_key_pem,
                    algorithms=["ES256"],
                    audience="authenticated",
                    options={"verify_aud": True}
                )
                print(f"[AUTH] Successfully verified Supabase ES256 token for user: {payload.get('email', 'unknown')}")
                return payload
            except JWTError as e:
                print(f"[AUTH] ES256 verification failed: {str(e)}")
                return None
        
        # Fall back to HS256 (Legacy) if algorithm is HS256 or if ES256 failed
        if alg == "HS256" or not SUPABASE_JWT_SECRET:
            if not SUPABASE_JWT_SECRET:
                print("[AUTH] WARNING: SUPABASE_JWT_SECRET is not set. Cannot verify HS256 tokens.")
                return None
            
            try:
                payload = jwt.decode(
                    token,
                    SUPABASE_JWT_SECRET,
                    algorithms=["HS256"],
                    audience="authenticated",
                    options={"verify_aud": True}
                )
                print(f"[AUTH] Successfully verified Supabase HS256 token for user: {payload.get('email', 'unknown')}")
                return payload
            except JWTError as e:
                print(f"[AUTH] HS256 verification failed: {str(e)}")
                return None
        
        # If algorithm is neither ES256 nor HS256, try both
        print(f"[AUTH] Unknown algorithm {alg}, trying both ES256 and HS256")
        
        # Try ES256
        if kid and SUPABASE_URL:
            jwks = get_supabase_jwks()
            if jwks:
                public_key_pem = get_public_key_from_jwks(jwks, kid)
                if public_key_pem:
                    try:
                        payload = jwt.decode(
                            token,
                            public_key_pem,
                            algorithms=["ES256"],
                            audience="authenticated",
                            options={"verify_aud": True}
                        )
                        print(f"[AUTH] Successfully verified token with ES256")
                        return payload
                    except JWTError:
                        pass
        
        # Try HS256
        if SUPABASE_JWT_SECRET:
            try:
                payload = jwt.decode(
                    token,
                    SUPABASE_JWT_SECRET,
                    algorithms=["HS256"],
                    audience="authenticated",
                    options={"verify_aud": True}
                )
                print(f"[AUTH] Successfully verified token with HS256")
                return payload
            except JWTError:
                pass
        
        print(f"[AUTH] Failed to verify token with any supported algorithm")
        return None
        
    except Exception as e:
        print(f"[AUTH] Error verifying Supabase token: {str(e)}")
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
