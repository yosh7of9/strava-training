import base64
import json
import httpx
from datetime import datetime, timezone
from fastapi import APIRouter, Request, Response, status
from core.database import get_db
from routers.sync import calculate_tss

router = APIRouter(prefix="/processor", tags=["processor"])

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
    access_token = user_data.get("access_token")
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
    
    # Calculate TSS
    tss = calculate_tss(activity, ftp, max_hr)
    
    # Update CTL and ATL using EWMA
    current_ctl = user_data.get("initial_ctl", 0.0)
    current_atl = user_data.get("initial_atl", 0.0)
    
    new_ctl = current_ctl + (tss - current_ctl) / 42.0
    new_atl = current_atl + (tss - current_atl) / 7.0
    
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    pmc_history = user_data.get("pmc_history", [])
    # Add new entry or update today's entry
    new_entry = {
        "date": today_str,
        "ctl": round(new_ctl, 1),
        "atl": round(new_atl, 1),
        "tsb": round(new_ctl - new_atl, 1)
    }
    if pmc_history and pmc_history[-1]["date"] == today_str:
        pmc_history[-1] = new_entry
    else:
        pmc_history.append(new_entry)
    
    # Update user document with new CTL/ATL
    user_ref.update({
        "initial_ctl": round(new_ctl, 1),
        "initial_atl": round(new_atl, 1),
        "last_sync_date": today_str,
        "pmc_history": pmc_history[-90:] # Keep last 90 days
    })
    
    # Save the activity record for PMC graph
    activity_ref = user_ref.collection("activities").document(str(activity_id))
    activity_ref.set({
        "name": activity.get("name"),
        "date": today_str,
        "tss": round(tss, 1),
        "type": activity.get("type"),
        "moving_time": activity.get("moving_time", 0)
    })
    
    return Response(status_code=status.HTTP_200_OK)
