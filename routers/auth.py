import os
import httpx
from fastapi import APIRouter, Request, HTTPException, status
from fastapi.responses import RedirectResponse
from core.config import settings
from core.database import get_db

router = APIRouter(prefix="/auth", tags=["auth"])

def is_user_allowed(athlete_id: str) -> bool:
    allowlist_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'allowed_users.txt')
    if not os.path.exists(allowlist_path):
        return True
        
    with open(allowlist_path, 'r') as f:
        allowed = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        
    # If file exists but is empty (except comments), we restrict to no one, or maybe allow all?
    # Let's say if it's empty, we allow all for now, until they add their ID.
    if not allowed:
        return True
        
    return athlete_id in allowed

@router.get("/login")
async def strava_login():
    """
    Redirects user to Strava's OAuth approval page.
    """
    strava_auth_url = (
        f"https://www.strava.com/oauth/authorize?"
        f"client_id={settings.STRAVA_CLIENT_ID}&"
        f"response_type=code&"
        f"redirect_uri={settings.STRAVA_REDIRECT_URI}&"
        f"approval_prompt=force&"
        f"scope=read,activity:read_all"
    )
    return RedirectResponse(url=strava_auth_url)

@router.get("/strava/callback")
async def strava_callback(request: Request, code: str = None, error: str = None):
    """
    Handles the callback from Strava after user approval.
    """
    if error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"OAuth Error: {error}")
    
    if not code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No authorization code returned")

    # Exchange code for token
    token_url = "https://www.strava.com/oauth/token"
    payload = {
        "client_id": settings.STRAVA_CLIENT_ID,
        "client_secret": settings.STRAVA_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code"
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(token_url, data=payload)
        
    if response.status_code != 200:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Failed to retrieve token from Strava")

    token_data = response.json()
    
    # Extract data
    athlete = token_data.get("athlete", {})
    athlete_id = str(athlete.get("id"))
    
    # Check Authorization (receipt-agent-app style)
    if not is_user_allowed(athlete_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied: your Strava account is not in the allowed_users.txt list.")

    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    expires_at = token_data.get("expires_at")
    
    # Save to Firestore
    db = get_db()
    user_ref = db.collection("users").document(athlete_id)
    user_doc = user_ref.get()
    
    user_data = {
        "strava_athlete_id": athlete_id,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_expires_at": expires_at,
        "name": f"{athlete.get('firstname', '')} {athlete.get('lastname', '')}".strip(),
        "profile_image": athlete.get("profile"),
    }

    if user_doc.exists:
        user_ref.update(user_data)
    else:
        # Initial default settings for a new user
        user_data["ftp"] = 200  # Default FTP
        user_data["max_hr"] = 190
        user_data["initial_ctl"] = 0
        user_data["initial_atl"] = 0
        user_ref.set(user_data)

    # Set user session
    request.session["user_id"] = athlete_id
    request.session["name"] = user_data["name"]

    # Redirect to dashboard
    return RedirectResponse(url="/dashboard", status_code=303)

@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)
