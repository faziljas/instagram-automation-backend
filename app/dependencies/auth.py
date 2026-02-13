from fastapi import Header, HTTPException, status, Depends
from sqlalchemy.orm import Session
import jwt  # PyJWT
import os
import requests
import time
from typing import Optional
from app.db.session import get_db

# Cache for JWKS (Public Keys)
JWKS_CACHE = None
JWKS_CACHE_TIMESTAMP = None
JWKS_CACHE_TTL = 3600  # Cache for 1 hour


def get_jwks(supabase_url: str, force_refresh: bool = False):
    """
    Fetch JWKS from Supabase with caching and retry logic.
    Only caches successful fetches - failures are not cached to allow retries.
    """
    global JWKS_CACHE, JWKS_CACHE_TIMESTAMP
    
    # Return cached value if it exists and is still valid (not forcing refresh)
    if JWKS_CACHE and not force_refresh:
        if JWKS_CACHE_TIMESTAMP:
            age = time.time() - JWKS_CACHE_TIMESTAMP
            if age < JWKS_CACHE_TTL:
                return JWKS_CACHE
    
    # Try fetching with retries
    max_retries = 3
    last_error = None
    
    for attempt in range(max_retries):
        try:
            jwks_url = f"{supabase_url}/auth/v1/.well-known/jwks.json"
            print(f"[AUTH] Fetching JWKS from: {jwks_url} (attempt {attempt + 1}/{max_retries})")
            r = requests.get(jwks_url, timeout=10)  # Increased timeout
            r.raise_for_status()
            jwks_data = r.json()
            
            # Only cache successful fetches
            JWKS_CACHE = jwks_data
            JWKS_CACHE_TIMESTAMP = time.time()
            print(f"[AUTH] Successfully fetched JWKS with {len(JWKS_CACHE.get('keys', []))} keys")
            return JWKS_CACHE
        except requests.exceptions.Timeout as e:
            last_error = f"Timeout: {str(e)}"
            print(f"[AUTH] JWKS fetch timeout (attempt {attempt + 1}/{max_retries}): {last_error}")
            if attempt < max_retries - 1:
                time.sleep(1)  # Wait 1 second before retry
        except requests.exceptions.RequestException as e:
            last_error = f"Request error: {str(e)}"
            print(f"[AUTH] JWKS fetch request error (attempt {attempt + 1}/{max_retries}): {last_error}")
            if attempt < max_retries - 1:
                time.sleep(1)  # Wait 1 second before retry
        except Exception as e:
            last_error = f"Unexpected error: {str(e)}"
            print(f"[AUTH] JWKS fetch unexpected error (attempt {attempt + 1}/{max_retries}): {last_error}")
            if attempt < max_retries - 1:
                time.sleep(1)  # Wait 1 second before retry
    
    # All retries failed - return None but don't cache it
    print(f"[AUTH] Failed to fetch JWKS after {max_retries} attempts: {last_error}")
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
        
        # Try to get JWKS (with retries and caching)
        jwks = get_jwks(supabase_url)
        
        # If fresh fetch failed, try using stale cache as fallback
        if not jwks and JWKS_CACHE:
            cache_age = time.time() - (JWKS_CACHE_TIMESTAMP or 0) if JWKS_CACHE_TIMESTAMP else float('inf')
            # Use stale cache if it's less than 24 hours old
            if cache_age < 86400:  # 24 hours
                print(f"[AUTH] Using stale JWKS cache (age: {cache_age:.0f}s) as fallback")
                jwks = JWKS_CACHE
            else:
                print(f"[AUTH] JWKS cache is too stale (age: {cache_age:.0f}s), cannot use as fallback")
        
        if not jwks:
            print("[AUTH] CRITICAL: Could not fetch JWKS and no valid cache available")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication service temporarily unavailable. Please try again in a moment."
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
        
        # Try to get JWKS (with retries and caching)
        jwks = get_jwks(supabase_url)
        
        # If fresh fetch failed, try using stale cache as fallback
        if not jwks and JWKS_CACHE:
            cache_age = time.time() - (JWKS_CACHE_TIMESTAMP or 0) if JWKS_CACHE_TIMESTAMP else float('inf')
            # Use stale cache if it's less than 24 hours old
            if cache_age < 86400:  # 24 hours
                print(f"[AUTH] Using stale JWKS cache (age: {cache_age:.0f}s) as fallback")
                jwks = JWKS_CACHE
            else:
                print(f"[AUTH] JWKS cache is too stale (age: {cache_age:.0f}s), cannot use as fallback")
        
        if not jwks:
            print("[AUTH] CRITICAL: Could not fetch JWKS and no valid cache available")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication service temporarily unavailable. Please try again in a moment."
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
    try:
        # Verify token and get payload (already verified, contains all claims)
        payload = verify_supabase_token(authorization)
    except HTTPException:
        # Re-raise HTTP exceptions (401, 503, etc.) as-is
        raise
    except Exception as e:
        # Catch any unexpected errors in token verification
        print(f"[AUTH] Unexpected error in token verification: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication error. Please try again."
        )
    
    # Extract email and user ID from verified payload (no decode() call here!)
    email = payload.get("email")
    supabase_user_id = payload.get("sub")
    
    if not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing email claim"
        )
    
    try:
        # Look up user: prefer supabase_id (sub) so same token always maps to same user.
        # Email-first lookup can return different users if duplicates exist or casing differs.
        from app.models.user import User
        user = None
        if supabase_user_id:
            user = db.query(User).filter(User.supabase_id == supabase_user_id).first()
        if not user:
            user = db.query(User).filter(User.email.ilike(email)).first()
            # Heal: if we found by email but this row has no supabase_id, set it so future requests find by sub (fixes "two behaviours").
            if user and supabase_user_id and not user.supabase_id:
                try:
                    user.supabase_id = supabase_user_id
                    db.commit()
                    db.refresh(user)
                    print(f"[AUTH] Set supabase_id on user {user.id} (email lookup) so token resolves consistently")
                except Exception as heal_e:
                    db.rollback()
                    print(f"[AUTH] Could not set supabase_id on user: {heal_e}")
    except Exception as e:
        # Catch database connection errors or other unexpected DB errors
        print(f"[AUTH] Database error while querying user: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable. Please try again in a moment."
        )
    
    if not user:
        # Auto-create user if missing (lazy sync) to prevent 404 errors for new users
        # This handles race conditions where user signs up and immediately navigates to a protected page
        if not supabase_user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token missing user ID claim"
            )
        
        # Check if user exists by supabase_id (shouldn't happen, but safety check)
        try:
            existing_supabase_user = db.query(User).filter(
                User.supabase_id == supabase_user_id
            ).first()
            
            if existing_supabase_user:
                # User exists with different email case - return it
                return existing_supabase_user.id
        except Exception as e:
            print(f"[AUTH] Database error checking supabase_id: {str(e)}")
            import traceback
            traceback.print_exc()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Database temporarily unavailable. Please try again in a moment."
            )
        
        # Create new user automatically
        # Use a more robust approach: double-check user doesn't exist before creating
        # This handles race conditions where another request created the user between checks
        from app.utils.auth import hash_password
        import time
        
        # Double-check user doesn't exist (race condition protection)
        user = db.query(User).filter(User.email.ilike(email)).first()
        if user:
            print(f"[AUTH] User found on second check (race condition): {user.id}")
            return user.id
        
        user_by_supabase = db.query(User).filter(User.supabase_id == supabase_user_id).first()
        if user_by_supabase:
            print(f"[AUTH] User found by supabase_id on second check: {user_by_supabase.id}")
            return user_by_supabase.id
        
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
            error_str = str(e).lower()
            print(f"[AUTH] Failed to auto-create user: {str(e)}")
            import traceback
            traceback.print_exc()
            
            # Check if this is a database integrity error (duplicate key, constraint violation)
            # This usually means the user was created by another concurrent request
            is_integrity_error = any(keyword in error_str for keyword in [
                'unique constraint', 'duplicate key', 'integrity', 
                'already exists', 'violates unique constraint'
            ])
            
            if is_integrity_error:
                print(f"[AUTH] Database integrity error detected - user likely created by concurrent request")
                # Refresh session to see committed changes from other transactions
                db.expire_all()
                
                # Wait a tiny bit to allow the other transaction to commit
                time.sleep(0.1)
                
                # Try finding the user - it should exist now
                user = db.query(User).filter(User.email.ilike(email)).first()
                if user:
                    print(f"[AUTH] User found after integrity error (race condition resolved): {user.id}")
                    return user.id
                
                # Also check by supabase_id
                user_by_supabase = db.query(User).filter(
                    User.supabase_id == supabase_user_id
                ).first()
                if user_by_supabase:
                    print(f"[AUTH] User found by supabase_id after integrity error: {user_by_supabase.id}")
                    return user_by_supabase.id
                
                # If integrity error but user still not found, retry a few more times with delays
                for retry_attempt in range(2):
                    time.sleep(0.2 * (retry_attempt + 1))  # Increasing delay
                    db.expire_all()
                    user = db.query(User).filter(User.email.ilike(email)).first()
                    if user:
                        print(f"[AUTH] User found after retry {retry_attempt + 1}: {user.id}")
                        return user.id
                    user_by_supabase = db.query(User).filter(
                        User.supabase_id == supabase_user_id
                    ).first()
                    if user_by_supabase:
                        print(f"[AUTH] User found by supabase_id after retry {retry_attempt + 1}: {user_by_supabase.id}")
                        return user_by_supabase.id
                
                # If we still can't find the user after integrity error, something is wrong
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="User account exists but could not be retrieved. Please try refreshing the page."
                )
            
            # For non-integrity errors, retry lookup a few times
            for retry_attempt in range(2):
                db.expire_all()
                user = db.query(User).filter(User.email.ilike(email)).first()
                if user:
                    print(f"[AUTH] User found after retry {retry_attempt + 1}: {user.id}")
                    return user.id
                user_by_supabase = db.query(User).filter(
                    User.supabase_id == supabase_user_id
                ).first()
                if user_by_supabase:
                    print(f"[AUTH] User found by supabase_id after retry {retry_attempt + 1}: {user_by_supabase.id}")
                    return user_by_supabase.id
                if retry_attempt < 1:
                    time.sleep(0.1)
            
            # If we still can't find the user after retries, log it but don't fail
            # Instead, try to create a minimal user record or return a temporary ID
            # This prevents 404 errors that break the frontend
            print(f"[AUTH] WARNING: User not found after retries, but token is valid. This should not happen.")
            print(f"[AUTH] Email: {email}, Supabase ID: {supabase_user_id}")
            
            try:
                # As a last resort, try one final lookup with a completely fresh query
                db.expire_all()
                final_user = db.query(User).filter(User.email.ilike(email)).first()
                if final_user:
                    return final_user.id
                
                final_user_by_supabase = db.query(User).filter(User.supabase_id == supabase_user_id).first()
                if final_user_by_supabase:
                    return final_user_by_supabase.id
            except Exception as lookup_e:
                print(f"[AUTH] Database error during final lookup: {str(lookup_e)}")
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Database temporarily unavailable. Please try again in a moment."
                )
            
            # If all retries fail, try one more time to create the user with minimal data
            # This is a last resort to prevent breaking the frontend
            try:
                print(f"[AUTH] Last resort: Attempting to create user one more time...")
                # Create a new user object
                final_new_user = User(
                    email=email.lower(),
                    hashed_password=placeholder_password,
                    supabase_id=supabase_user_id,
                    is_verified=True,
                    plan_tier="free",
                )
                db.add(final_new_user)
                db.commit()
                db.refresh(final_new_user)
                print(f"[AUTH] Successfully created user {final_new_user.id} on last resort attempt")
                return final_new_user.id
            except Exception as final_e:
                db.rollback()
                print(f"[AUTH] CRITICAL: Final user creation attempt also failed: {str(final_e)}")
                # Even if creation fails, try one more lookup - maybe it was created by another process
                try:
                    db.expire_all()
                    last_check = db.query(User).filter(User.email.ilike(email)).first()
                    if last_check:
                        print(f"[AUTH] User found on absolute final check: {last_check.id}")
                        return last_check.id
                except Exception:
                    pass
                
                # If we absolutely cannot find or create the user, raise 503 instead of 404
                # This indicates a database issue rather than user not existing
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Unable to access user account. Please try again in a moment or contact support."
                )
    
    # Return user ID - wrap in try-except for safety
    try:
        return user.id
    except Exception as e:
        print(f"[AUTH] Unexpected error returning user ID: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication error. Please try again."
        )
