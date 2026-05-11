import httpx
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Request, HTTPException, status
from fastapi.responses import RedirectResponse
from core.database import get_db

router = APIRouter(prefix="/sync", tags=["sync"])

def calculate_tss(activity, ftp, max_hr):
    """
    Estimate TSS based on power, suffer score, or heart rate.
    """
    # Try power-based TSS
    power = activity.get("weighted_average_power") or activity.get("average_watts")
    moving_time = activity.get("moving_time", 0) # in seconds
    
    if power and ftp and ftp > 0:
        intensity_factor = power / ftp
        tss = (moving_time * power * intensity_factor) / (ftp * 3600) * 100
        return max(0, tss)
    
    # Try Suffer Score (Strava's hrTSS)
    suffer_score = activity.get("suffer_score")
    if suffer_score:
        return suffer_score
        
    # Try average HR heuristic
    avg_hr = activity.get("average_heartrate")
    if avg_hr and max_hr and max_hr > 0:
        hr_ratio = avg_hr / max_hr
        # Very rough approximation
        tss = (moving_time / 3600) * (hr_ratio * 100) * (hr_ratio * 1.2)
        return max(0, tss)
        
    return 0

@router.post("/initial")
async def initial_sync(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse(url="/", status_code=303)
        
    db = get_db()
    user_ref = db.collection("users").document(user_id)
    user_doc = user_ref.get()
    
    if not user_doc.exists:
        return RedirectResponse(url="/auth/logout", status_code=303)
        
    user_data = user_doc.to_dict()
    access_token = user_data.get("access_token")
    ftp = user_data.get("ftp", 200)
    max_hr = user_data.get("max_hr", 190)
    
    # We fetch activities for the last 365 days (1 year)
    # CTL is a 42-day average. Capturing 1 year gives us a long-term PMC chart.
    start_date = datetime.now(timezone.utc) - timedelta(days=365)
    start_timestamp = int(start_date.timestamp())
    
    headers = {"Authorization": f"Bearer {access_token}"}
    activities = []
    
    async with httpx.AsyncClient() as client:
        page = 1
        while True:
            url = f"https://www.strava.com/api/v3/athlete/activities?after={start_timestamp}&per_page=100&page={page}"
            response = await client.get(url, headers=headers)
            
            if response.status_code == 401:
                # Token expired. Clear session and force re-login
                request.session.clear()
                return RedirectResponse(url="/auth/login", status_code=303)
                
            page_activities = response.json()
            if not page_activities:
                break
                
            activities.extend(page_activities)
            page += 1
    
    # Group activities by day (YYYY-MM-DD)
    daily_tss = {}
    for act in activities:
        # Start date is in ISO 8601, e.g., "2024-03-21T14:30:00Z"
        date_str = act.get("start_date_local", act.get("start_date"))[:10]
        tss = calculate_tss(act, ftp, max_hr)
        daily_tss[date_str] = daily_tss.get(date_str, 0) + tss
        
    # Calculate CTL and ATL from start_date to today
    ctl = 0.0
    atl = 0.0
    
    today = datetime.now(timezone.utc).date()
    current_date = start_date.date()
    
    pmc_history = []
    
    while current_date <= today:
        date_str = current_date.strftime("%Y-%m-%d")
        tss_today = daily_tss.get(date_str, 0)
        
        # If it's today and no activity, don't update with TSS=0.
        # This prevents premature decay before the actual workout is synced.
        if current_date == today and tss_today == 0:
            break
            
        ctl = ctl + (tss_today - ctl) / 42.0
        atl = atl + (tss_today - atl) / 7.0
        
        pmc_history.append({
            "date": date_str,
            "tss": round(tss_today, 1),
            "ctl": round(ctl, 1),
            "atl": round(atl, 1),
            "tsb": round(ctl - atl, 1)
        })
        
        current_date += timedelta(days=1)
        
    # Batch save individual activities
    batch = db.batch()
    for act in activities:
        act_id = str(act["id"])
        # Use start_date_local or start_date for sorting, e.g., "2024-03-21T14:30:00Z"
        start_date = act.get("start_date_local", act.get("start_date"))
        date_only = start_date[:10]
        tss = calculate_tss(act, ftp, max_hr)
        
        act_ref = user_ref.collection("activities").document(act_id)
        batch.set(act_ref, {
            "name": act.get("name"),
            "date": date_only,
            "start_date": start_date, # Full ISO string for precise sorting
            "tss": round(tss, 1),
            "type": act.get("type"),
            "moving_time": act.get("moving_time", 0)
        })
    
    batch.commit()
        
    # Save the calculated CTL/ATL to Firestore
    user_ref.update({
        "initial_ctl": round(ctl, 1),
        "initial_atl": round(atl, 1),
        "last_sync_date": today.strftime("%Y-%m-%d"),
        "pmc_history": pmc_history[-1095:]  # Keep up to 1095 days of history (3 years)
    })
    
    return RedirectResponse(url="/dashboard", status_code=303)


@router.post("/latest")
async def sync_latest(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse(url="/", status_code=303)
        
    db = get_db()
    user_ref = db.collection("users").document(user_id)
    user_doc = user_ref.get()
    
    if not user_doc.exists:
        return RedirectResponse(url="/auth/logout", status_code=303)
        
    user_data = user_doc.to_dict()
    access_token = user_data.get("access_token")
    ftp = user_data.get("ftp", 200)
    max_hr = user_data.get("max_hr", 190)
    
    # Fetch recent activities from Strava (last 30 days to be safe)
    start_date = datetime.now(timezone.utc) - timedelta(days=30)
    start_timestamp = int(start_date.timestamp())
    
    headers = {"Authorization": f"Bearer {access_token}"}
    activities = []
    
    async with httpx.AsyncClient() as client:
        page = 1
        while True:
            url = f"https://www.strava.com/api/v3/athlete/activities?after={start_timestamp}&per_page=100&page={page}"
            response = await client.get(url, headers=headers)
            
            if response.status_code == 401:
                request.session.clear()
                return RedirectResponse(url="/auth/login", status_code=303)
                
            page_activities = response.json()
            if not page_activities:
                break
            activities.extend(page_activities)
            page += 1
    
    if not activities:
        return RedirectResponse(url="/dashboard", status_code=303)

    # Get existing activity IDs for the same period from Firestore to avoid redundant work
    existing_acts = user_ref.collection("activities").where("date", ">=", start_date.strftime("%Y-%m-%d")).get()
    existing_ids = {doc.id for doc in existing_acts}

    batch = db.batch()
    new_data_synced = False
    
    for act in activities:
        act_id = str(act["id"])
        
        # SKIP if already exists and has TSS (we assume it's complete)
        if act_id in existing_ids:
            continue
            
        start_date_iso = act.get("start_date_local", act.get("start_date"))
        date_only = start_date_iso[:10]
        tss = calculate_tss(act, ftp, max_hr)
        
        act_ref = user_ref.collection("activities").document(act_id)
        batch.set(act_ref, {
            "name": act.get("name"),
            "date": date_only,
            "start_date": start_date_iso,
            "tss": round(tss, 1),
            "type": act.get("type"),
            "moving_time": act.get("moving_time", 0)
        })
        new_data_synced = True
    
    if new_data_synced:
        batch.commit()
        # Note: If new activities are added, ideally we should trigger a PMC re-calculation.
        # For simplicity in this 'latest' sync, we'll let the next webhook or manual initial sync 
        # do the heavy lifting of history recalculation, OR we could just recalculate the last 30 days.
    
    return RedirectResponse(url="/dashboard", status_code=303)
