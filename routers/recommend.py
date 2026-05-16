from datetime import datetime, timezone
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from core.database import get_db

router = APIRouter(prefix="/recommend", tags=["recommend"])

# Training type definitions for FTP improvement
TRAINING_TYPES = {
    "Rest": {
        "label": "レスト（休息）",
        "emoji": "😴",
        "color": "gray",
        "description": "今日は休息日です。睡眠と栄養をしっかり摂り、回復に専念しましょう。",
        "details": "トレーニングは不要です。軽いウォーキングやストレッチ程度に留めてください。",
        "duration": "—",
        "intensity": "—",
    },
    "Recovery": {
        "label": "リカバリー（回復走）",
        "emoji": "🚶",
        "color": "green",
        "description": "血流を促進し、疲労を抜くための非常に軽いライドです。",
        "details": "心拍数を上げすぎないように。強度は 50-60% FTP を維持してください。物足りなく感じても、決して踏み込みすぎないでください。",
        "duration": "30–45 分",
        "intensity": "< 60% FTP",
    },
    "Endurance": {
        "label": "エンデュランス（有酸素）",
        "emoji": "🚴",
        "color": "blue",
        "description": "有酸素能力の土台を作る、会話ができる程度の強度です。",
        "details": "60–75% FTP を維持します。脂肪燃焼効率を高め、スタミナを強化するのに最適です。",
        "duration": "60–90 分",
        "intensity": "60–75% FTP",
    },
    "Tempo": {
        "label": "テンポ",
        "emoji": "⚡",
        "color": "yellow",
        "description": "乳酸閾値の少し下、持久力とパワーを両立させる強度です。",
        "details": "76–87% FTP を 20–40 分間継続します。ややきついですが、一定時間維持できるペースです。乳酸除去能力を高めます。",
        "duration": "60 分 (20–40 分 @ Tempo)",
        "intensity": "76–87% FTP",
    },
    "SST": {
        "label": "Sweet Spot Training (SST)",
        "emoji": "🎯",
        "color": "orange",
        "description": "FTP向上に最も効率的と言われる、王道のトレーニングです。",
        "details": "88–93% FTP をターゲットにします。2×20 分、または 3×15 分（セット間レスト 5 分）が基本構成です。",
        "duration": "60–75 分 (2×20 分ブロック)",
        "intensity": "88–93% FTP",
    },
    "Threshold": {
        "label": "閾値（Threshold）",
        "emoji": "🔥",
        "color": "red",
        "description": "FTP付近でのトレーニングです。閾値そのものを直接引き上げます。",
        "details": "95–105% FTP で維持します。2×15 分、または 1×30 分を行います。非常に負荷が高いので、十分に回復した状態で行ってください。",
        "duration": "60 分 (2×15 分ブロック)",
        "intensity": "95–105% FTP",
    },
    "VO2max": {
        "label": "VO2max（最大酸素摂取量）",
        "emoji": "💥",
        "color": "purple",
        "description": "有酸素能力の天井を引き上げる、短時間・高強度のインターバルです。",
        "details": "106–120% FTP で 5×4 分（セット間レスト 4 分）を行います。非常に苦しいですが、効果は絶大です。",
        "duration": "45–60 分 (5×4 分インターバル)",
        "intensity": "106–120% FTP",
    },
    "Long Endurance": {
        "label": "ロングライド",
        "emoji": "🗺️",
        "color": "teal",
        "description": "長時間走行により、有酸素ベースと脂質代謝能力を徹底的に鍛えます。",
        "details": "60–75% FTP で長時間走り続けます。強度は上げすぎず、一貫性とサドルの上での時間を重視してください。",
        "duration": "2–4 時間",
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
            warning = f"⚠️ TSBが {tsb:.1f} です（極度の疲労）。今日のメニューを「リカバリー」に下方修正しました。"
            return "Recovery", warning
    elif tsb < -10:
        # Somewhat fatigued — step down one level
        rank = INTENSITY_RANK.index(training_type) if training_type in INTENSITY_RANK else -1
        if rank > 2:  # Only step down if above Endurance
            downgraded = INTENSITY_RANK[rank - 1]
            warning = f"📉 TSBが {tsb:.1f} です（疲労蓄積）。強度を {training_type} → {downgraded} に調整しました。"
            return downgraded, warning
    elif tsb >= 5:
        # Fresh legs — optionally step up one level
        rank = INTENSITY_RANK.index(training_type) if training_type in INTENSITY_RANK else -1
        if 2 <= rank < len(INTENSITY_RANK) - 1:
            upgraded = INTENSITY_RANK[rank + 1]
            warning = f"✅ TSBが {tsb:.1f} です（絶好調！）。余裕があれば {upgraded} に挑戦してみるのも良いでしょう。"
            return training_type, warning  # Suggest but don't force upgrade
    
    return training_type, warning


def generate_default_schedule(rest_days: list[str]) -> dict[str, str]:
    """
    Auto-generate a weekly schedule based on selected rest days.
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
            training_info["duration"] = f"{min_dur}–{max_dur} 分 (上限)"
            
        elif adjusted_type in ["Tempo", "SST", "Threshold", "VO2max"]:
            # Map types to target intensity factors
            ifs = {"Tempo": 0.82, "SST": 0.90, "Threshold": 1.0, "VO2max": 1.13}
            target_if = ifs.get(adjusted_type, 0.9)
            
            # Update duration label
            m2 = calc_m(2, target_if)
            if m2 > 0:
                m2_f = min(m2, 20) # Default cap for duration label display
                training_info["duration"] = f"{30 + 2*m2_f} 分 (2×{m2_f} 分ブロック)"
            
            # Update details text dynamically with a cap at the original value
            import re
            def replacer(match):
                sets = int(match.group(1))
                original_m = int(match.group(2))
                m = calc_m(sets, target_if)
                # Never exceed the original planned duration
                final_m = min(m, original_m)
                return f"{sets}×{final_m} 分"
            
            # Note: We now match "分" since the base strings are Japanese
            pattern = r"(\d+)×(\d+) 分"
            training_info["details"] = re.sub(pattern, replacer, training_info["details"])
            
            if m2 <= 0:
                training_info["details"] = "⚠️ TSS許容上限が低すぎます。完全休息またはリカバリーを推奨します。"

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
