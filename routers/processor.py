import base64
import json
import httpx
from datetime import datetime, timezone
from fastapi import APIRouter, Request, Response, status
from core.database import get_db
from routers.sync import calculate_tss
from core.config import settings
from core.analyzer import ActivityAnalyzer

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
    
    # Fetch Streams for Distribution and Analysis
    url_streams = f"https://www.strava.com/api/v3/activities/{activity_id}/streams?keys=watts,heartrate,cadence&key_by_type=true"
    watts_data = []
    hr_data = []
    cad_data = []
    async with httpx.AsyncClient() as client:
        resp_streams = await client.get(url_streams, headers=headers)
        if resp_streams.status_code == 200:
            streams = resp_streams.json()
            if "watts" in streams:
                watts_data = streams["watts"]["data"]
            if "heartrate" in streams:
                hr_data = streams["heartrate"]["data"]
            if "cadence" in streams:
                cad_data = streams["cadence"]["data"]
    
    # Run ActivityAnalyzer
    analyzer = ActivityAnalyzer(watts_data, hr_data, cad_data, ftp=ftp)
    metrics = analyzer.analyze_all(workout_type_id=activity.get("workout_type"))
    
    # Calculate precise TSS using the NP from the analyzer
    # calculate_tss logic uses (moving_time * NP * IF) / (FTP * 3600) * 100
    # If NP is calculated from streams, it's more accurate than summary data
    precise_np = metrics.get("normalized_power", 0)
    tss = calculate_tss(activity, ftp, max_hr, override_np=precise_np) if precise_np > 0 else calculate_tss(activity, ftp, max_hr)
    
    # Save/Update this specific activity in its sub-collection
    start_date = activity.get("start_date_local", activity.get("start_date"))
    activity_ref = user_ref.collection("activities").document(str(activity_id))
    activity_ref.set({
        "name": activity.get("name"),
        "date": activity_date,
        "start_date": start_date, # Full ISO string for precise sorting
        "tss": round(tss, 1),
        "type": activity.get("type"),
        "moving_time": activity.get("moving_time", 0),
        "average_heartrate": activity.get("average_heartrate"),
        "average_cadence": activity.get("average_cadence"),
        "watts_data": watts_data, # Store for re-calculation if needed
        "metrics": metrics,
        "profile_key": metrics.get("profile_key"),
        "is_new_activity": True,
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
    
    # We now use the robust sync_pmc_data function to handle gap filling
    # and recalculate up to the date of the new activity.
    from routers.sync import sync_pmc_data
    target_date = datetime.strptime(activity_date, "%Y-%m-%d").date()
    await sync_pmc_data(user_ref, user_data, target_date)
    
    return Response(status_code=status.HTTP_200_OK)
