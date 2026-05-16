from datetime import datetime, timezone
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from core.database import get_db

router = APIRouter(prefix="/recommend", tags=["recommend"])

# Training type definitions for FTP improvement
TRAINING_TYPES = {
    "Rest": {
        "label": "Rest Day",
        "emoji": "😴",
        "color": "gray",
        "description": "Today is a rest day. Focus on recovery, sleep, and nutrition.",
        "details": "No training needed. Light walking or stretching is fine.",
        "duration": "—",
        "intensity": "—",
    },
    "Recovery": {
        "label": "Active Recovery",
        "emoji": "🚶",
        "color": "green",
        "description": "Easy spin to promote blood flow and recovery.",
        "details": "Keep heart rate very low. Ride at 50-60% FTP. Do NOT push harder even if it feels easy.",
        "duration": "30–45 min",
        "intensity": "< 60% FTP",
    },
    "Endurance": {
        "label": "Endurance",
        "emoji": "🚴",
        "color": "blue",
        "description": "Aerobic base building at a comfortable, conversational pace.",
        "details": "Maintain 60–75% FTP. You should be able to hold a conversation. Great for fat metabolism and aerobic engine.",
        "duration": "60–90 min",
        "intensity": "60–75% FTP",
    },
    "Tempo": {
        "label": "Tempo",
        "emoji": "⚡",
        "color": "yellow",
        "description": "Sustained effort just below lactate threshold.",
        "details": "Hold 76–87% FTP for 20–40 minutes continuously. Challenging but sustainable. Builds lactate clearance.",
        "duration": "60 min (20–40 min @ Tempo)",
        "intensity": "76–87% FTP",
    },
    "SST": {
        "label": "Sweet Spot Training",
        "emoji": "🎯",
        "color": "orange",
        "description": "The most efficient zone for FTP improvement.",
        "details": "Target 88–93% FTP. Do 2×20 min or 3×15 min with 5 min rest between. This is your primary FTP builder.",
        "duration": "60–75 min (2×20 min blocks)",
        "intensity": "88–93% FTP",
    },
    "Threshold": {
        "label": "Threshold",
        "emoji": "🔥",
        "color": "red",
        "description": "Training at or near your FTP. Directly raises your threshold.",
        "details": "Hold 95–105% FTP. Do 2×15 min or 1×30 min. Very demanding — only do this when well-rested.",
        "duration": "60 min (2×15 min blocks)",
        "intensity": "95–105% FTP",
    },
    "VO2max": {
        "label": "VO2max Intervals",
        "emoji": "💥",
        "color": "purple",
        "description": "Short, very hard intervals to raise your aerobic ceiling.",
        "details": "Do 5×4 min @ 106–120% FTP with 4 min easy recovery. Or 8×3 min with 3 min recovery. Painful but powerful.",
        "duration": "45–60 min (5×4 min intervals)",
        "intensity": "106–120% FTP",
    },
    "Long Endurance": {
        "label": "Long Endurance Ride",
        "emoji": "🗺️",
        "color": "teal",
        "description": "Long aerobic ride to build your aerobic base and fat metabolism.",
        "details": "Ride at 60–75% FTP for an extended period. No need to push hard. Focus on consistency and time in the saddle.",
        "duration": "2–4 hours",
        "intensity": "60–75% FTP",
    },
}

# Intensity ranking for TSB adjustment
INTENSITY_RANK = ["Rest", "Recovery", "Endurance", "Long Endurance", "Tempo", "SST", "Threshold", "VO2max"]

def adjust_for_tsb(training_type: str, tsb: float) -> tuple[str, str | None]:
    """
    Adjust training type based on TSB (form).
    Returns (adjusted_type, warning_message).
    """
    warning = None
    
    if tsb < -20:
        # Very fatigued — force recovery regardless of schedule
        if training_type not in ["Rest", "Recovery"]:
            warning = f"⚠️ Your TSB is {tsb:.1f} (very fatigued). Scheduled workout downgraded to Recovery."
            return "Recovery", warning
    elif tsb < -10:
        # Somewhat fatigued — step down one level
        rank = INTENSITY_RANK.index(training_type) if training_type in INTENSITY_RANK else -1
        if rank > 2:  # Only step down if above Endurance
            downgraded = INTENSITY_RANK[rank - 1]
            warning = f"📉 TSB is {tsb:.1f} (some fatigue). Intensity reduced from {training_type} → {downgraded}."
            return downgraded, warning
    elif tsb >= 5:
        # Fresh legs — optionally step up one level
        rank = INTENSITY_RANK.index(training_type) if training_type in INTENSITY_RANK else -1
        if 2 <= rank < len(INTENSITY_RANK) - 1:
            upgraded = INTENSITY_RANK[rank + 1]
            warning = f"✅ TSB is {tsb:.1f} (fresh legs!). You could push to {upgraded} if feeling good."
            return training_type, warning  # Suggest but don't force upgrade
    
    return training_type, warning


def generate_default_schedule(rest_days: list[str]) -> dict[str, str]:
    """
    Auto-generate a weekly schedule based on selected rest days.
    Rules:
    - Mon-Fri = short sessions (Zwift evening, 45-90 min)
    - Sat-Sun = longer sessions possible (2-4 hours)
    - Day after rest -> Threshold (fresh legs)
    - Day before rest -> Tempo (taper)
    - Weekends (non-rest) -> Long Endurance
    - Other weekdays -> SST or VO2max (one per week)
    """
    days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    weekends = {"sat", "sun"}
    schedule = {}
    
    rest_set = set(rest_days)
    
    # First pass: mark rest days
    for d in days:
        schedule[d] = "Rest" if d in rest_set else None
    
    # Second pass: day after rest → Threshold (only weekdays)
    for i, d in enumerate(days):
        prev = days[(i - 1) % 7]
        if prev in rest_set and d not in rest_set and d not in weekends:
            schedule[d] = "Threshold"
    
    # Third pass: day before rest → Tempo (only weekdays)
    for i, d in enumerate(days):
        nxt = days[(i + 1) % 7]
        if nxt in rest_set and d not in rest_set and d not in weekends:
            # Don't override already-set Threshold
            if schedule[d] is None:
                schedule[d] = "Tempo"
    
    # Fourth pass: weekends (non-rest) → Long Endurance
    for d in weekends:
        if d not in rest_set:
            schedule[d] = "Long Endurance"
    
    # Fifth pass: remaining weekdays — add one VO2max, rest SST
    vo2max_assigned = False
    for d in days:
        if schedule[d] is None and d not in weekends:
            if not vo2max_assigned:
                schedule[d] = "VO2max"
                vo2max_assigned = True
            else:
                schedule[d] = "SST"
    
    return schedule


def format_training_with_ftp(training_info: dict, ftp: int) -> dict:
    """
    Replaces percentage strings like '60–75% FTP' with actual Watt values like '120–150W'.
    """
    import re
    info = training_info.copy()
    
    def replace_func(match):
        p_str = match.group(1)
        # Handle en-dash (–) and hyphen (-)
        sep = "–" if "–" in p_str else "-"
        if sep in p_str:
            try:
                low, high = map(float, p_str.split(sep))
                return f"{int(ftp * low / 100)}–{int(ftp * high / 100)}W"
            except: return match.group(0)
        else:
            try:
                val = float(p_str)
                return f"{int(ftp * val / 100)}W"
            except: return match.group(0)

    # Matches "60–75% FTP", "90% FTP", etc.
    pattern = r"(\d+(?:[–-]\d+)?)\s*%\s*FTP"
    
    for key in ["details", "intensity"]:
        if key in info:
            info[key] = re.sub(pattern, replace_func, info[key])
    return info


@router.get("/today")
async def get_today_recommendation(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    
    db = get_db()
    user_doc = db.collection("users").document(user_id).get()
    if not user_doc.exists:
        return JSONResponse({"error": "User not found"}, status_code=404)
    
    user_data = user_doc.to_dict()
    ftp = user_data.get("ftp", 200)
    weekly_schedule = user_data.get("weekly_schedule", {})
    current_ctl = user_data.get("initial_ctl", 0.0)
    current_atl = user_data.get("initial_atl", 0.0)
    tsb = round(current_ctl - current_atl, 1)
    
    # Get today's day of week
    today = datetime.now(timezone.utc).strftime("%a").lower()  # mon, tue, ...
    
    scheduled_type = weekly_schedule.get(today, "Endurance")
    if not scheduled_type:
        scheduled_type = "Endurance"
    
    adjusted_type, warning = adjust_for_tsb(scheduled_type, tsb)
    
    # Calculate TSS allowance if TSB is low (<= -10)
    tss_allowance = None
    if tsb <= -10:
        # Target TSB formula provided by user
        # TSB_allow = -10 - 0.2 * Current_CTL + 0.3 * Current_TSB
        target_tsb = -10 - (0.2 * current_ctl) + (0.3 * tsb)
        
        # Reverse calculation for TSS:
        # TSB_after = TSB_now - 5/42 * TSS - CTL/42 + ATL/7 >= TSB_allow
        # TSS <= (TSB_now - TSB_allow - CTL/42 + ATL/7) * 42/5
        tss_limit = (tsb - target_tsb - (current_ctl / 42.0) + (current_atl / 7.0)) * 8.4
        tss_allowance = max(0, round(tss_limit))
        
        allowance_msg = f"💡 今日のTSS許容上限は {tss_allowance} です。これを超える強度のトレーニングは控えましょう。"
        if warning:
            warning += f" {allowance_msg}"
        else:
            warning = allowance_msg

    raw_training_info = TRAINING_TYPES.get(adjusted_type, TRAINING_TYPES["Endurance"])
    # Convert % FTP to Watts for better UX
    training_info = format_training_with_ftp(raw_training_info, ftp)
    
    # Adjust Duration/Details for any training if TSB is low
    if tss_allowance is not None:
        def calc_m(sets, intensity_factor):
            # Base TSS for WU/CD/Rest (approx 30-35 min) is around 12-15
            base_tss = 10 + (sets * 2) # Heuristic
            available = tss_allowance - base_tss
            if available <= 0: return 0
            # M = (available_tss * 60) / (sets * intensity^2 * 100)
            return round((available * 60) / (sets * (intensity_factor**2) * 100))

        if adjusted_type == "Endurance":
            min_dur = round((tss_allowance * 60) / 56.25)
            max_dur = round((tss_allowance * 60) / 36.0)
            training_info["duration"] = f"{min_dur}–{max_dur} min (Max)"
            
        elif adjusted_type in ["Tempo", "SST", "Threshold", "VO2max"]:
            # Map types to target intensity factors
            ifs = {"Tempo": 0.82, "SST": 0.90, "Threshold": 1.0, "VO2max": 1.13}
            target_if = ifs.get(adjusted_type, 0.9)
            
            # Update duration label
            m2 = calc_m(2, target_if)
            if m2 > 0:
                training_info["duration"] = f"{30 + 2*m2} min (2×{m2} min blocks)"
            
            # Update details text dynamically with a cap at the original value
            import re
            def replacer(match):
                sets = int(match.group(1))
                original_m = int(match.group(2))
                m = calc_m(sets, target_if)
                # Never exceed the original planned duration
                final_m = min(m, original_m)
                return f"{sets}×{final_m} min"
            
            pattern = r"(\d+)×(\d+) min"
            training_info["details"] = re.sub(pattern, replacer, training_info["details"])
            
            # Re-update duration label based on the (potentially capped) m2
            m2_capped = min(calc_m(2, target_if), 20) # Default SST/Threshold usually starts around 20
            # For simplicity, let's just use the updated details as the source of truth
            if m2 <= 0:
                training_info["details"] = "⚠️ TSS limit is too low for intervals. Consider active recovery or rest."

    return {
        "day": today,
        "scheduled_type": scheduled_type,
        "adjusted_type": adjusted_type,
        "tsb": tsb,
        "warning": warning,
        "tss_allowance": tss_allowance,
        "training": training_info,
        "ftp": ftp
    }


@router.post("/generate-schedule")
async def generate_schedule(request: Request):
    """Generate a default weekly schedule based on rest days."""
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    
    body = await request.json()
    rest_days = body.get("rest_days", [])
    
    schedule = generate_default_schedule(rest_days)
    return {"schedule": schedule}
