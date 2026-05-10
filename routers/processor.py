import base64
import json
import httpx
import numpy as np
from datetime import datetime, timezone
from fastapi import APIRouter, Request, Response, status
from core.database import get_db
from routers.sync import calculate_tss
from core.config import settings

router = APIRouter(prefix="/processor", tags=["processor"])

async def get_valid_access_token(user_ref, user_data):
    """
    Checks if token is expired and refreshes if necessary.
    """
    expires_at = user_data.get("expires_at", 0)
    now = datetime.now(timezone.utc).timestamp()
    
    if now < expires_at - 60: # 1 minute margin
        return user_data.get("access_token")
        
    print(f"Token expired for user {user_ref.id}. Refreshing...")
    
    refresh_token = user_data.get("refresh_token")
    if not refresh_token:
        return None
        
    url = "https://www.strava.com/oauth/token"
    data = {
        "client_id": settings.STRAVA_CLIENT_ID,
        "client_secret": settings.STRAVA_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.post(url, data=data)
        
    if response.status_code != 200:
        print(f"Failed to refresh token: {response.text}")
        return None
        
    new_tokens = response.json()
    user_ref.update({
        "access_token": new_tokens["access_token"],
        "refresh_token": new_tokens["refresh_token"],
        "expires_at": new_tokens["expires_at"]
    })
    
    return new_tokens["access_token"]


@router.post("/process-activity")
async def process_activity(request: Request):
    """
    Invoked by Pub/Sub Push Subscription when a new activity event arrives.
    """
    try:
        envelope = await request.json()
    except Exception:
        return Response(status_code=status.HTTP_400_BAD_REQUEST)
        
    if not envelope or "message" not in envelope:
        return Response(status_code=status.HTTP_400_BAD_REQUEST)
        
    message = envelope["message"]
    if "data" not in message:
        return Response(status_code=status.HTTP_400_BAD_REQUEST)
        
    # Decode pub/sub data
    data_str = base64.b64decode(message["data"]).decode("utf-8")
    event = json.loads(data_str)
    
    # We only process 'create' or 'update' events for now
    if event.get("aspect_type") not in ["create", "update"]:
        return Response(status_code=status.HTTP_200_OK)
        
    athlete_id = str(event.get("owner_id"))
    activity_id = event.get("object_id")
    
    db = get_db()
    user_ref = db.collection("users").document(athlete_id)
    user_doc = user_ref.get()
    
    if not user_doc.exists:
        # User not found in our DB, ignore
        return Response(status_code=status.HTTP_200_OK)
        
    user_data = user_doc.to_dict()
    access_token = await get_valid_access_token(user_ref, user_data)
    if not access_token:
        return Response(status_code=status.HTTP_200_OK)
        
    ftp = user_data.get("ftp", 200)
    max_hr = user_data.get("max_hr", 190)
    
    # Fetch activity details
    url = f"https://www.strava.com/api/v3/activities/{activity_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    
    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers)
        
    if response.status_code != 200:
        print(f"Failed to fetch activity {activity_id} from Strava. Status: {response.status_code}")
        # Return 200 to Pub/Sub to ack the message (don't retry endlessly if unauthorized)
        return Response(status_code=status.HTTP_200_OK)
        
    activity = response.json()
    activity_date = activity.get("start_date_local", activity.get("start_date"))[:10]
    
    # Fetch Power Streams for Distribution
    url_streams = f"https://www.strava.com/api/v3/activities/{activity_id}/streams?keys=watts&key_by_type=true"
    watts_data = []
    async with httpx.AsyncClient() as client:
        resp_streams = await client.get(url_streams, headers=headers)
        if resp_streams.status_code == 200:
            streams = resp_streams.json()
            if "watts" in streams:
                watts_data = streams["watts"]["data"]
    
    # Calculate current activity TSS
    tss = calculate_tss(activity, ftp, max_hr)
    
    # Save/Update this specific activity in its sub-collection
    activity_ref = user_ref.collection("activities").document(str(activity_id))
    activity_ref.set({
        "name": activity.get("name"),
        "date": activity_date,
        "tss": round(tss, 1),
        "watts_data": watts_data, # Store for re-calculation if needed
        "synced_at": datetime.now(timezone.utc).isoformat()
    })
    
    # Aggregate all activities for the same day
    all_acts_today = user_ref.collection("activities").where("date", "==", activity_date).get()
    total_tss_today = 0
    combined_watts = []
    for act_doc in all_acts_today:
        data = act_doc.to_dict()
        total_tss_today += data.get("tss", 0)
        combined_watts.extend(data.get("watts_data", []))
    
    # Filter 0W/Low power for distribution (ignore <= 20W)
    filtered_watts = [w for w in combined_watts if w > 20]
    p5, p50, p95 = None, None, None
    if filtered_watts:
        p5 = float(np.percentile(filtered_watts, 5))
        p50 = float(np.percentile(filtered_watts, 50))
        p95 = float(np.percentile(filtered_watts, 95))
    
    # Update CTL/ATL based on total_tss_today
    # To be mathematically correct for multiple updates on same day, 
    # we need to start from the values at the end of YESTERDAY.
    pmc_history = user_data.get("pmc_history", [])
    
    prev_ctl = user_data.get("initial_ctl", 0.0)
    prev_atl = user_data.get("initial_atl", 0.0)
    
    # If today's entry already exists, 'yesterday' is the one before it
    is_update = False
    if pmc_history and pmc_history[-1]["date"] == activity_date:
        is_update = True
        if len(pmc_history) > 1:
            prev_ctl = pmc_history[-2]["ctl"]
            prev_atl = pmc_history[-2]["atl"]
        else:
            # If today is the first day ever, start from initial user settings
            # We need to backtrack the first update. 
            # new_ctl = current_ctl + (tss - current_ctl) / 42.0
            # current_ctl = (new_ctl * 42 - tss) / 41
            # But let's assume we have initial values in the user doc.
            pass

    new_ctl = prev_ctl + (total_tss_today - prev_ctl) / 42.0
    new_atl = prev_atl + (total_tss_today - prev_atl) / 7.0
    
    new_entry = {
        "date": activity_date,
        "tss": round(total_tss_today, 1),
        "ctl": round(new_ctl, 1),
        "atl": round(new_atl, 1),
        "tsb": round(new_ctl - new_atl, 1),
        "p5": round(p5, 1) if p5 is not None else None,
        "p50": round(p50, 1) if p50 is not None else None,
        "p95": round(p95, 1) if p95 is not None else None
    }
    
    if is_update:
        pmc_history[-1] = new_entry
    else:
        pmc_history.append(new_entry)
    
    # Update user document
    user_ref.update({
        "initial_ctl": round(new_ctl, 1),
        "initial_atl": round(new_atl, 1),
        "last_sync_date": activity_date,
        "pmc_history": pmc_history[-1095:]
    })
    
    # Save the activity record for PMC graph
    start_date = activity.get("start_date_local", activity.get("start_date"))
    activity_ref = user_ref.collection("activities").document(str(activity_id))
    activity_ref.set({
        "name": activity.get("name"),
        "date": today_str,
        "start_date": start_date,
        "tss": round(tss, 1),
        "type": activity.get("type"),
        "moving_time": activity.get("moving_time", 0)
    })
    
    return Response(status_code=status.HTTP_200_OK)
