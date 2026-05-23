import httpx
import numpy as np
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Request, HTTPException, status
from fastapi.responses import RedirectResponse
from core.database import get_db
from core.analyzer import ActivityAnalyzer

JST = timezone(timedelta(hours=9))

router = APIRouter(prefix="/sync", tags=["sync"])

def calculate_tss(activity, ftp, max_hr, override_np=None):
    """
    Estimate TSS based on power, suffer score, or heart rate.
    """
    # Try power-based TSS
    # Use override_np if provided (from detailed stream analysis), otherwise fallback to Strava summary
    power = override_np or activity.get("weighted_average_power") or activity.get("average_watts")
    moving_time = activity.get("moving_time", 0) # in seconds
    
    if power and ftp and ftp > 0:
        # TSS Formula: (sec * NP * (NP/FTP)) / (FTP * 3600) * 100
        tss = (moving_time * (power ** 2)) / ((ftp ** 2) * 3600) * 100
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
    start_date = datetime.now(JST) - timedelta(days=365)
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
    
    # Group activities by day (YYYY-MM-DD)
    daily_tss = {}
    for act in activities:
        date_str = act.get("start_date_local", act.get("start_date"))[:10]
        tss = calculate_tss(act, ftp, max_hr)
        daily_tss[date_str] = daily_tss.get(date_str, 0) + tss

    # Build a lookup of existing pmc_history entries to preserve p5/p50/p95
    existing_pmc = user_data.get("pmc_history", [])
    existing_power_stats = {
        e["date"]: {k: e[k] for k in ("p5", "p50", "p95") if k in e}
        for e in existing_pmc
        if e.get("p5") is not None
    }

    # Calculate CTL and ATL from start_date to today
    ctl = 0.0
    atl = 0.0
    
    today = datetime.now(JST).date()
    current_date = start_date.date()
    pmc_history = []
    
    while current_date <= today:
        date_str = current_date.strftime("%Y-%m-%d")
        tss_today = daily_tss.get(date_str, 0)
        
        if current_date == today and tss_today == 0:
            break
            
        ctl = ctl + (tss_today - ctl) / 42.0
        atl = atl + (tss_today - atl) / 7.0

        entry = {
            "date": date_str,
            "tss": round(tss_today, 1),
            "ctl": round(ctl, 1),
            "atl": round(atl, 1),
            "tsb": round(ctl - atl, 1)
        }
        if date_str in existing_power_stats:
            entry.update(existing_power_stats[date_str])

        pmc_history.append(entry)
        current_date += timedelta(days=1)
        
    # Batch save individual activities
    batch = db.batch()
    for act in activities:
        act_id = str(act["id"])
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
            "moving_time": act.get("moving_time", 0),
            "average_heartrate": act.get("average_heartrate"),
            "average_cadence": act.get("average_cadence")
        })
    
    batch.commit()
        
    user_ref.update({
        "initial_ctl": round(ctl, 1),
        "initial_atl": round(atl, 1),
        "last_sync_date": today.strftime("%Y-%m-%d"),
        "pmc_history": pmc_history[-1095:]
    })
    
    return RedirectResponse(url="/dashboard", status_code=303)


async def sync_pmc_data(user_ref, user_data, target_date):
    """
    Robust PMC recalculation from the last entry up to target_date.
    - Aggregates multi-activity TSS and power distribution for each day.
    - Fills gaps (rest days) with TSS=0.
    - Stops at target_date - 1 if today (target_date) has TSS=0.
    - Recalculates from target_date if it's older than the current history end.
    """
    pmc_history = user_data.get("pmc_history", [])
    if not pmc_history:
        return None

    last_entry = pmc_history[-1]
    last_date = datetime.strptime(last_entry["date"], "%Y-%m-%d").date()
    
    # 1. Determine starting point
    if last_date >= target_date:
        # We need to re-calculate from target_date onwards
        # Find the last 'safe' day before target_date
        safe_history = [e for e in pmc_history if datetime.strptime(e["date"], "%Y-%m-%d").date() < target_date]
        if safe_history:
            last_safe = safe_history[-1]
            new_pmc_history = safe_history
            current_date = target_date
        else:
            # If target_date is older than our entire history, we fallback or return None
            return None 
    else:
        last_safe = last_entry
        new_pmc_history = pmc_history[:]
        current_date = last_date + timedelta(days=1)

    # 2. Preparation for aggregation and preservation
    existing_power_stats = {
        e["date"]: {k: e[k] for k in ("p5", "p50", "p95") if k in e}
        for e in pmc_history
        if e.get("p5") is not None
    }

    # Recalculate up to today (or target_date if that's later)
    today = datetime.now(JST).date()
    final_date = max(today, target_date)

    # Fetch all activities for the gap/update period from Firestore
    activities_ref = user_ref.collection("activities")
    acts = activities_ref.where("date", ">=", current_date.strftime("%Y-%m-%d")).where("date", "<=", final_date.strftime("%Y-%m-%d")).get()
    
    daily_data = {}
    for a in acts:
        d = a.to_dict()
        dt = d.get("date")
        if dt not in daily_data:
            daily_data[dt] = {"tss": 0, "watts": []}
        daily_data[dt]["tss"] += d.get("tss", 0)
        if d.get("watts_data"):
            daily_data[dt]["watts"].extend(d.get("watts_data"))

    ctl = last_safe["ctl"]
    atl = last_safe["atl"]
    
    updated = False
    while current_date <= final_date:
        date_str = current_date.strftime("%Y-%m-%d")
        day_data = daily_data.get(date_str, {"tss": 0, "watts": []})
        tss_today = day_data["tss"]
        
        # UI Policy: Don't show today if no training yet
        if current_date == today and tss_today == 0:
            break
            
        ctl = ctl + (tss_today - ctl) / 42.0
        atl = atl + (tss_today - atl) / 7.0
        
        # Calculate power distribution (percentiles) from aggregated watts
        p5, p50, p95 = None, None, None
        filtered_watts = [w for w in day_data["watts"] if w > 20]
        if filtered_watts:
            p5 = round(float(np.percentile(filtered_watts, 5)), 1)
            p50 = round(float(np.percentile(filtered_watts, 50)), 1)
            p95 = round(float(np.percentile(filtered_watts, 95)), 1)
        
        entry = {
            "date": date_str,
            "tss": round(tss_today, 1),
            "ctl": round(ctl, 1),
            "atl": round(atl, 1),
            "tsb": round(ctl - atl, 1),
            "p5": p5,
            "p50": p50,
            "p95": p95
        }
        
        # Fallback to existing power stats if new ones couldn't be calculated
        if p5 is None and date_str in existing_power_stats:
            entry.update(existing_power_stats[date_str])

        new_pmc_history.append(entry)
        current_date += timedelta(days=1)
        updated = True

    if updated:
        update_doc = {
            "pmc_history": new_pmc_history[-1095:],
            "last_sync_date": final_date.strftime("%Y-%m-%d")
        }
        if new_pmc_history:
            update_doc["initial_ctl"] = new_pmc_history[-1]["ctl"]
            update_doc["initial_atl"] = new_pmc_history[-1]["atl"]
            
        user_ref.update(update_doc)
        return new_pmc_history
        
    return None


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
    
    # Preemptively clean up any previous is_new_activity flags
    activities_ref = user_ref.collection("activities")
    outstanding_new = activities_ref.where("is_new_activity", "==", True).get()
    for doc in outstanding_new:
        doc.reference.update({"is_new_activity": False})

    
    # Step-wise search: 10 days -> 20 days -> 30 days -> 45 days
    headers = {"Authorization": f"Bearer {access_token}"}
    activities = []
    start_date_used = None
    
    async with httpx.AsyncClient() as client:
        for days in [10, 20, 30, 45]:
            start_date_used = datetime.now(JST) - timedelta(days=days)
            start_ts = int(start_date_used.timestamp())
            url = f"https://www.strava.com/api/v3/athlete/activities?after={start_ts}&per_page=100"
            resp = await client.get(url, headers=headers)
            
            if resp.status_code == 401:
                request.session.clear()
                return RedirectResponse(url="/auth/login", status_code=303)
                
            activities = resp.json()
            if activities:
                break
            
            # If our history already covers this period, no need to search further back
            pmc_history = user_data.get("pmc_history", [])
            if pmc_history:
                last_entry_date = datetime.strptime(pmc_history[-1]["date"], "%Y-%m-%d").date()
                if last_entry_date >= start_date_used.date():
                    break
    
    # Identify which activities are new or the absolute latest
    # Get IDs for the search period to avoid re-fetching unchanged activities
    existing_acts = user_ref.collection("activities").where("date", ">=", start_date_used.strftime("%Y-%m-%d")).get()
    existing_ids = {doc.id for doc in existing_acts}
    latest_act_id = str(activities[0]["id"]) if activities else None

    async with httpx.AsyncClient() as client:
        for act in activities:
            act_id = str(act["id"])
            # Always update the latest activity, or any activity we don't have yet
            if act_id in existing_ids and act_id != latest_act_id:
                continue
                
            # Fetch streams for new or latest activity for box plot and analysis
            watts_data = []
            hr_data = []
            cad_data = []
            url_streams = f"https://www.strava.com/api/v3/activities/{act_id}/streams?keys=watts,heartrate,cadence&key_by_type=true"
            resp_streams = await client.get(url_streams, headers=headers)
            if resp_streams.status_code == 200:
                streams = resp_streams.json()
                if "watts" in streams:
                    watts_data = streams["watts"]["data"]
                if "heartrate" in streams:
                    hr_data = streams["heartrate"]["data"]
                if "cadence" in streams:
                    cad_data = streams["cadence"]["data"]

            start_iso = act.get("start_date_local", act.get("start_date"))
            date_only = start_iso[:10]
            
            # Run ActivityAnalyzer
            analyzer = ActivityAnalyzer(watts_data, hr_data, cad_data, ftp=ftp)
            metrics = analyzer.analyze_all(workout_type_id=act.get("workout_type"))
            
            # Calculate precise TSS using the NP from the analyzer
            precise_np = metrics.get("normalized_power", 0)
            tss = calculate_tss(act, ftp, max_hr, override_np=precise_np) if precise_np > 0 else calculate_tss(act, ftp, max_hr)

            user_ref.collection("activities").document(act_id).set({
                "name": act.get("name"),
                "date": date_only,
                "start_date": start_iso,
                "tss": round(tss, 1),
                "type": act.get("type"),
                "moving_time": act.get("moving_time", 0),
                "average_heartrate": act.get("average_heartrate"),
                "average_cadence": act.get("average_cadence"),
                "watts_data": watts_data,
                "metrics": metrics,
                "profile_key": metrics.get("profile_key"),
                "is_new_activity": True,
                "synced_at": datetime.now(timezone.utc).isoformat()
            })
    
    # Recalculate PMC everything up to today
    today = datetime.now(JST).date()
    await sync_pmc_data(user_ref, user_data, today)
    
    return RedirectResponse(url="/dashboard", status_code=303)
