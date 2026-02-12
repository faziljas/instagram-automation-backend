from fastapi import Header, HTTPException, status, Depends
from sqlalchemy.orm import Session
import jwt  # PyJWT
import os
import requests
from typing import Optional
from app.db.session import get_db

# Cache for JWKS (Public Keys)
JWKS_CACHE = None


def get_jwks(supabase_url: str):
    """
    Fetch JWKS from Supabase with caching.
    """
    global JWKS_CACHE
    if JWKS_CACHE:
        return JWKS_CACHE
    try:
        jwks_url = f"{supabase_url}/auth/v1/.well-known/jwks.json"
        print(f"[AUTH] Fetching JWKS from: {jwks_url}")
        r = requests.get(jwks_url, timeout=5)
        r.raise_for_status()
        JWKS_CACHE = r.json()
        print(f"[AUTH] Successfully fetched JWKS with {len(JWKS_CACHE.get('keys', []))} keys")
        return JWKS_CACHE
    except Exception as e:
        print(f"[AUTH] Failed to fetch JWKS: {e}")
        return None


def verify_supabase_token(authorization: Optional[str] = Header(None)):
    """
    Verifies the Supabase JWT token.
    Supports both HS256 (Shared Secret) and ES256/RS256 (Asymmetric Key).
    Returns the payload dict if valid.
    
    CRITICAL: We decode AND get the payload in one step.
    We do NOT call decode() again later.
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
    
    # Reject common invalid token values
    if token.lower() in ["null", "undefined", "none", ""]:
        print(f"[AUTH] Rejected invalid token value: '{token}'")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token: token value is null or undefined"
        )
    
    # Validate JWT token format (should have 3 parts: header.payload.signature)
    token_parts = token.split(".")
    if len(token_parts) != 3:
        print(f"[AUTH] Invalid token format: expected 3 parts, got {len(token_parts)}. Token length: {len(token)}")
        print(f"[AUTH] Token preview (first 50 chars): {token[:50]}...")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token format. Token must have header.payload.signature structure."
        )
    
    # 1. Check Algorithm from header
    try:
        unverified_header = jwt.get_unverified_header(token)
        algo = unverified_header.get("alg")
        kid = unverified_header.get("kid")
        print(f"[AUTH] Token algorithm: {algo}, Key ID: {kid}")
    except jwt.DecodeError as e:
        print(f"[AUTH] Failed to decode token header: {str(e)}")
        print(f"[AUTH] Token preview (first 100 chars): {token[:100]}...")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token header: {str(e)}"
        )
    except Exception as e:
        print(f"[AUTH] Unexpected error decoding token header: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token header"
        )
    
    payload = None
    
    # 2. Case A: ES256 (Supabase Default) - Uses Public Keys (JWKS)
    if algo == "ES256":
        supabase_url = os.getenv("SUPABASE_URL")
        if not supabase_url:
            print("[AUTH] Error: SUPABASE_URL is missing for ES256 verification")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Server misconfiguration: SUPABASE_URL not set"
            )
        
        jwks = get_jwks(supabase_url)
        if not jwks:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not fetch authentication keys from Supabase"
            )
        
        try:
            # PyJWT automatically finds the right key from the JWKS
            jwks_client = jwt.PyJWKClient(f"{supabase_url}/auth/v1/.well-known/jwks.json")
            signing_key = jwks_client.get_signing_key_from_jwt(token)
            
            # CRITICAL: We decode AND get the payload in one step.
            # We do NOT call decode() again later.
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["ES256"],
                audience="authenticated",
                options={"verify_aud": True}
            )
            email = payload.get("email", "unknown")
            print(f"[AUTH] Successfully verified ES256 token for user: {email}")
        except Exception as e:
            print(f"[AUTH] ES256 Verification failed: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token signature"
            )
    
    # 3. Case B: HS256 (Legacy) - Uses Secret
    elif algo == "HS256":
        secret = os.getenv("SUPABASE_JWT_SECRET")
        if not secret:
            print("[AUTH] Error: SUPABASE_JWT_SECRET is missing in environment variables")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Server misconfiguration: SUPABASE_JWT_SECRET not set"
            )
        
        try:
            # CRITICAL: We decode AND get the payload in one step.
            payload = jwt.decode(
                token,
                secret,
                algorithms=["HS256"],
                audience="authenticated",
                options={"verify_aud": True}
            )
            email = payload.get("email", "unknown")
            print(f"[AUTH] Successfully verified HS256 token for user: {email}")
        except Exception as e:
            print(f"[AUTH] HS256 Verification failed: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token signature"
            )
    
    # 4. Case C: RS256 (RSA) - Uses Public Keys (JWKS)
    elif algo == "RS256":
        supabase_url = os.getenv("SUPABASE_URL")
        if not supabase_url:
            print("[AUTH] Error: SUPABASE_URL is missing for RS256 verification")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Server misconfiguration: SUPABASE_URL not set"
            )
        
        jwks = get_jwks(supabase_url)
        if not jwks:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not fetch authentication keys from Supabase"
            )
        
        try:
            # PyJWT automatically finds the right key from the JWKS
            jwks_client = jwt.PyJWKClient(f"{supabase_url}/auth/v1/.well-known/jwks.json")
            signing_key = jwks_client.get_signing_key_from_jwt(token)
            
            # CRITICAL: We decode AND get the payload in one step.
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience="authenticated",
                options={"verify_aud": True}
            )
            email = payload.get("email", "unknown")
            print(f"[AUTH] Successfully verified RS256 token for user: {email}")
        except Exception as e:
            print(f"[AUTH] RS256 Verification failed: {str(e)}")
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
    
    # 5. Return the payload (contains 'sub', 'email', etc.)
    if payload:
        return payload
    
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not verify token"
    )


def get_current_user_id(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db)
) -> int:
    """
    FastAPI dependency that verifies Supabase token and returns backend user ID.
    This is the main dependency to use in route handlers.
    
    CRITICAL: We use the payload from verify_supabase_token directly.
    We do NOT call decode() again.
    
    Auto-creates user if missing to prevent 404 errors for new users.
    """
    # Verify token and get payload (already verified, contains all claims)
    payload = verify_supabase_token(authorization)
    
    # Extract email and user ID from verified payload (no decode() call here!)
    email = payload.get("email")
    supabase_user_id = payload.get("sub")
    
    if not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing email claim"
        )
    
    # Look up user by email in backend database
    from app.models.user import User
    user = db.query(User).filter(User.email.ilike(email)).first()
    
    if not user:
        # Auto-create user if missing (lazy sync) to prevent 404 errors for new users
        # This handles race conditions where user signs up and immediately navigates to a protected page
        if not supabase_user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token missing user ID claim"
            )
        
        # Check if user exists by supabase_id (shouldn't happen, but safety check)
        existing_supabase_user = db.query(User).filter(
            User.supabase_id == supabase_user_id
        ).first()
        
        if existing_supabase_user:
            # User exists with different email case - return it
            return existing_supabase_user.id
        
        # Create new user automatically
        from app.utils.auth import hash_password
        placeholder_password = hash_password(f"supabase_user_{supabase_user_id}")
        
        new_user = User(
            email=email.lower(),
            hashed_password=placeholder_password,
            supabase_id=supabase_user_id,
            is_verified=True,  # Supabase handles email verification
            plan_tier="free",  # Explicitly set plan_tier for new users
        )
        
        try:
            db.add(new_user)
            db.commit()
            db.refresh(new_user)
            print(f"[AUTH] Auto-created user {new_user.id} for email {email} (lazy sync)")
            return new_user.id
        except Exception as e:
            db.rollback()
            print(f"[AUTH] Failed to auto-create user: {str(e)}")
            import traceback
            traceback.print_exc()
            
            # Check if user was created by another request (race condition)
            # Retry lookup after rollback
            user = db.query(User).filter(User.email.ilike(email)).first()
            if user:
                print(f"[AUTH] User found after retry (race condition resolved): {user.id}")
                return user.id
            
            # Also check by supabase_id in case email lookup failed
            user_by_supabase = db.query(User).filter(
                User.supabase_id == supabase_user_id
            ).first()
            if user_by_supabase:
                print(f"[AUTH] User found by supabase_id after retry: {user_by_supabase.id}")
                return user_by_supabase.id
            
            # If auto-create fails and user still doesn't exist, raise 404
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found in database. Please sync your account first."
            )
    
    return user.id
