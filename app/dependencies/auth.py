from fastapi import Header, HTTPException, status, Depends
from jose import jwt, JWTError
from jose.jwt import get_unverified_header
from sqlalchemy.orm import Session
import os
import requests
from typing import Optional
from app.db.session import get_db

# Cache for JWKS to avoid fetching on every request
JWKS_CACHE = None
JWKS_CACHE_TIMESTAMP = None
JWKS_CACHE_TTL = 3600  # Cache for 1 hour


def get_jwks(supabase_url: str):
    """
    Fetch JWKS from Supabase with caching.
    """
    global JWKS_CACHE, JWKS_CACHE_TIMESTAMP
    import time
    
    # Return cached JWKS if still valid
    if JWKS_CACHE and JWKS_CACHE_TIMESTAMP:
        if time.time() - JWKS_CACHE_TIMESTAMP < JWKS_CACHE_TTL:
            return JWKS_CACHE
    
    try:
        # Fetch keys from Supabase JWKS endpoint
        jwks_url = f"{supabase_url}/auth/v1/.well-known/jwks.json"
        print(f"[AUTH] Fetching JWKS from: {jwks_url}")
        r = requests.get(jwks_url, timeout=5)
        r.raise_for_status()
        JWKS_CACHE = r.json()
        JWKS_CACHE_TIMESTAMP = time.time()
        print(f"[AUTH] Successfully fetched JWKS with {len(JWKS_CACHE.get('keys', []))} keys")
        return JWKS_CACHE
    except Exception as e:
        print(f"[AUTH] Failed to fetch JWKS: {e}")
        return None


def get_signing_key_from_jwks(jwks: dict, kid: str):
    """
    Extract signing key from JWKS for a given key ID.
    Supports both RSA and ECC keys.
    """
    if not jwks or not kid:
        return None
    
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            try:
                # Use jose library to handle key conversion
                # For ES256 (ECC P-256)
                if key.get("kty") == "EC" and key.get("crv") == "P-256":
                    from cryptography.hazmat.primitives import serialization
                    from cryptography.hazmat.primitives.asymmetric import ec
                    from cryptography.hazmat.backends import default_backend
                    import base64
                    
                    # Base64URL decode
                    def base64url_decode(data):
                        padding = 4 - len(data) % 4
                        if padding != 4:
                            data += "=" * padding
                        data = data.replace("-", "+").replace("_", "/")
                        return base64.b64decode(data)
                    
                    x_bytes = base64url_decode(key["x"])
                    y_bytes = base64url_decode(key["y"])
                    
                    public_numbers = ec.EllipticCurvePublicNumbers(
                        int.from_bytes(x_bytes, "big"),
                        int.from_bytes(y_bytes, "big"),
                        ec.SECP256R1()
                    )
                    public_key = public_numbers.public_key(default_backend())
                    
                    pem = public_key.public_bytes(
                        encoding=serialization.Encoding.PEM,
                        format=serialization.PublicFormat.SubjectPublicKeyInfo
                    )
                    return pem.decode()
                
                # For RS256 (RSA) - jose library handles this automatically
                elif key.get("kty") == "RSA":
                    # jose library can handle RSA keys directly from JWK
                    return key
                    
            except Exception as e:
                print(f"[AUTH] Error extracting key from JWKS: {str(e)}")
                return None
    
    return None


def verify_supabase_token(authorization: Optional[str] = Header(None)):
    """
    Verifies the Supabase JWT token.
    Supports both HS256 (Shared Secret) and ES256/RS256 (Asymmetric Key).
    Returns the payload dict if valid.
    """
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authorization header"
        )
    
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid header format. Expected 'Bearer <token>'"
        )
    
    token = authorization.replace("Bearer ", "").strip()
    
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing token"
        )
    
    # 1. Decode header to check algorithm
    try:
        unverified_header = get_unverified_header(token)
        algo = unverified_header.get("alg")
        kid = unverified_header.get("kid")  # Key ID for asymmetric keys
        print(f"[AUTH] Token algorithm: {algo}, Key ID: {kid}")
    except Exception as e:
        print(f"[AUTH] Failed to decode token header: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token header"
        )
    
    # 2. Case A: HS256 (Standard/Legacy) - Uses SUPABASE_JWT_SECRET
    if algo == "HS256":
        secret = os.getenv("SUPABASE_JWT_SECRET")
        if not secret:
            print("[AUTH] Error: SUPABASE_JWT_SECRET is missing in environment variables")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Server misconfiguration: SUPABASE_JWT_SECRET not set"
            )
        
        try:
            payload = jwt.decode(
                token,
                secret,
                algorithms=["HS256"],
                audience="authenticated",
                options={"verify_aud": True}
            )
            email = payload.get("email", "unknown")
            print(f"[AUTH] Successfully verified HS256 token for user: {email}")
            return payload  # Return full payload
        except JWTError as e:
            print(f"[AUTH] HS256 Verification failed: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token"
            )
    
    # 3. Case B: ES256/RS256 (New Supabase Projects) - Uses JWKS
    elif algo in ["ES256", "RS256"]:
        supabase_url = os.getenv("SUPABASE_URL")
        if not supabase_url:
            print("[AUTH] Error: SUPABASE_URL is missing for ES256/RS256 verification")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Server misconfiguration: SUPABASE_URL not set"
            )
        
        if not kid:
            print("[AUTH] Error: Token missing 'kid' (key ID) for ES256/RS256 verification")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token: missing key ID"
            )
        
        # Fetch JWKS
        jwks = get_jwks(supabase_url)
        if not jwks:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not fetch authentication keys from Supabase"
            )
        
        # Get signing key from JWKS
        signing_key = get_signing_key_from_jwks(jwks, kid)
        if not signing_key:
            print(f"[AUTH] Error: Could not find signing key for kid: {kid}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token: key not found"
            )
        
        try:
            # Verify token with the signing key
            payload = jwt.decode(
                token,
                signing_key,
                algorithms=[algo],
                audience="authenticated",
                options={"verify_aud": True}
            )
            email = payload.get("email", "unknown")
            print(f"[AUTH] Successfully verified {algo} token for user: {email}")
            return payload  # Return full payload
        except JWTError as e:
            print(f"[AUTH] {algo} Verification failed: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token signature"
            )
    
    else:
        print(f"[AUTH] Unsupported algorithm: {algo}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Unsupported token algorithm: {algo}"
        )


def get_current_user_id(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db)
) -> int:
    """
    FastAPI dependency that verifies Supabase token and returns backend user ID.
    This is the main dependency to use in route handlers.
    """
    # Verify token and get payload (already verified, so we have email)
    payload = verify_supabase_token(authorization)
    
    # Extract email from verified payload
    email = payload.get("email")
    
    if not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing email claim"
        )
    
    # Look up user by email in backend database
    from app.models.user import User
    user = db.query(User).filter(User.email.ilike(email)).first()
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found in database. Please sync your account first."
        )
    
    return user.id
