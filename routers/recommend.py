from datetime import datetime, timezone
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from core.database import get_db

router = APIRouter(prefix="/recommend", tags=["recommend"])

# TSSに対する各ゾーンのワークアウトパタン（持続時間・レスト時間）
WORKOUTS = {
    "VO2max": [
        {"tss": 16, "duration": "30 分", "intervals": "(5 x 2分 レスト2分)"},
        {"tss": 24, "duration": "45 分", "intervals": "(5 x 3分 レスト3分)"},
        {"tss": 34, "duration": "55 分", "intervals": "(5 x 4分 レスト4分)"},
    ],
    "Threshold": [
        {"tss": 25, "duration": "35 分", "intervals": "(2 x 8分 レスト4分)"},
        {"tss": 34, "duration": "40 分", "intervals": "(2 x 10分 レスト5分)"},
        {"tss": 53, "duration": "56 分", "intervals": "(3 x 12分 レスト5分)"},
    ],
    "SST": [
        {"tss": 33, "duration": "40 分", "intervals": "(2 x 10分 レスト5分)"},
        {"tss": 47, "duration": "50 分", "intervals": "(2 x 15分 レスト5分)"},
        {"tss": 61, "duration": "60 分", "intervals": "(2 x 20分 レスト5分)"},
        {"tss": 88, "duration": "85 分", "intervals": "(3 x 20分 レスト5分)"},
    ],
    "Tempo": [
        {"tss": 39, "duration": "55 分", "intervals": "(2 x 20分 レスト5分)"},
        {"tss": 59, "duration": "75 分", "intervals": "(2 x 30分 レスト5分)"},
        {"tss": 78, "duration": "75 分", "intervals": "(1 x 60分)"},
    ],
    "Endurance": [
        {"tss": 49, "duration": "60 分", "intervals": "(60分)"},
        {"tss": 74, "duration": "90 分", "intervals": "(90分)"},
        {"tss": 98, "duration": "120 分", "intervals": "(120分)"},
    ],
}

# Training type definitions for FTP improvement
TRAINING_TYPES = {
    "Rest": {
        "label": "レスト（休息）",
        "emoji": "😴",
        "color": "gray",
        "description": "今日は休息日です。睡眠と栄養をしっかり摂り、回復に専念しましょう。",
        "details": "トレーニングは不要です。軽いウォーキングやストレッチ程度に留めてください。",
        "duration": "—",
        "intervals": None,
        "intensity": "—",
    },
    "Recovery": {
        "label": "リカバリー（回復走）",
        "emoji": "🚶",
        "color": "green",
        "description": "血流を促進し、疲労を抜くための非常に軽いライドです。",
        "details": "心拍数を上げすぎないように。強度は 50-60% FTP を維持してください。物足りなく感じても、決して踏み込みすぎないでください。",
        "duration": "30–45 分",
        "intervals": None,
        "intensity": "< 60% FTP",
    },
    "Endurance": {
        "label": "エンデュランス（有酸素）",
        "emoji": "🚴",
        "color": "blue",
        "description": "有酸素能力の土台を作る、会話ができる程度の強度です。",
        "details": "60–75% FTP を維持します。脂肪燃焼効率を高め、スタミナを強化するのに最適です。",
        "duration": "60–90 分",
        "intervals": None,
        "intensity": "60–75% FTP",
    },
    "Tempo": {
        "label": "テンポ",
        "emoji": "⚡",
        "color": "yellow",
        "description": "乳酸閾値の少し下、持久力とパワーを両立させる強度です。",
        "details": "76–87% FTP を継続します。ややきついですが、一定時間維持できるペースです。乳酸除去能力を高めます。",
        "duration": "70 分",
        "intervals": "(2 x 30分レスト5分、または 60分)",
        "intensity": "76–87% FTP",
    },
    "SST": {
        "label": "Sweet Spot Training (SST)",
        "emoji": "🎯",
        "color": "orange",
        "description": "FTP向上に最も効率的と言われる、王道のトレーニングです。",
        "details": "88–93% FTP をターゲットにします。",
        "duration": "60–75 分",
        "intervals": "(2 x 20分 レスト5分)",
        "intensity": "88–93% FTP",
    },
    "Threshold": {
        "label": "閾値（Threshold）",
        "emoji": "🔥",
        "color": "red",
        "description": "FTP付近でのトレーニングです。閾値そのものを直接引き上げます。",
        "details": "95–105% FTP で維持します。非常に負荷が高いので、十分に回復した状態で行ってください。",
        "duration": "60 分",
        "intervals": "(2 x 15分レスト5分 または 30分)",
        "intensity": "95–105% FTP",
    },
    "VO2max": {
        "label": "VO2max（最大酸素摂取量）",
        "emoji": "💥",
        "color": "purple",
        "description": "有酸素能力の天井を引き上げる、短時間・高強度のインターバルです。",
        "details": "106–120% FTP で行います。非常に苦しいですが、効果は絶大です。",
        "duration": "45–60 分",
        "intervals": "(5 x 4分 レスト4分)",
        "intensity": "106–120% FTP",
    },
}

# Intensity ranking for TSB adjustment
INTENSITY_RANK = ["Rest", "Recovery", "Endurance", "Tempo", "SST", "Threshold", "VO2max"]

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

def suggest_workout(target_zone: str, tss_limit: int):
    target_idx = INTENSITY_RANK.index(target_zone)
    candidates = []
    for zone_idx, zone in enumerate(INTENSITY_RANK):
        distance = abs(zone_idx - target_idx)
        for w in WORKOUTS.get(zone, {}):
            if len(w) == 0:
                continue
            if w["tss"] <= tss_limit:
                candidates.append({
                    "zone": zone,
                    "distance": distance,
                    **w
                })
    if not candidates:
        return None
    candidates.sort(
        key=lambda x: (
            x["distance"],      # 希望ゾーン優先
            -x["tss"]           # 同距離ならTSS最大
        )
    )
    best = candidates[0]
    return {
        "zone": best["zone"],
        "intervals": best["intervals"],
        "tss": best["tss"],
        "duration": best["duration"]
    }


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
            schedule[d] = "Endurance"
    
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
    
    # Get today's day of week (JST)
    from datetime import timezone, timedelta
    jst = timezone(timedelta(hours=9))
    today = datetime.now(jst).strftime("%a").lower()  # mon, tue, ...
    
    scheduled_type = weekly_schedule.get(today, "Endurance")
    if not scheduled_type:
        scheduled_type = "Endurance"
    
    # Check if a custom type is requested via query parameter
    custom_type = request.query_params.get("type")
    if custom_type in TRAINING_TYPES:
        adjusted_type = custom_type
    else:
        adjusted_type = scheduled_type
    
    # Calculate TSS allowance if TSB is low (<= -10)
    tss_allowance = None
    # テスト用に条件を緩める場合はここを調整（例: tsb < 0）
    warning = None
    if tsb <= -10:
        # Target TSB formula provided by user
        target_tsb = -10 - (0.2 * current_ctl) + (0.3 * tsb)
        
        # Reverse calculation for TSS
        tss_limit = (tsb - target_tsb - (current_ctl / 42.0) + (current_atl / 7.0)) * 8.4
        tss_allowance = max(0, round(tss_limit))
        
        warning = f"💡 今日のTSS許容上限は {tss_allowance} です。これを超える強度のトレーニングは控えましょう。"
    elif tsb >= 5:
        rank = INTENSITY_RANK.index(adjusted_type) if adjusted_type in INTENSITY_RANK else -1
        if 2 <= rank < len(INTENSITY_RANK) - 1:
            upgraded = INTENSITY_RANK[rank + 1]
            upgraded_label = TRAINING_TYPES[upgraded]["label"]
            warning = f"✅ TSBが {tsb:.1f} です（絶好調！）。余裕があればプルダウンから「{upgraded_label}」に挑戦してみるのも良いでしょう。"

    # 基本となるトレーニング情報を取得（辞書を壊さないようコピー）
    base_info = TRAINING_TYPES.get(adjusted_type, TRAINING_TYPES["Endurance"]).copy()
    
    # Refine based on TSS limit if TSB is low
    if (tss_allowance is not None) and (adjusted_type not in ["Rest", "Recovery"]):
        workout = suggest_workout(adjusted_type, tss_allowance)
        if workout:
            # 提案されたワークアウトのゾーンが予定と異なる場合、そのゾーンの基本情報をベースにする
            if workout["zone"] != adjusted_type:
                base_info = TRAINING_TYPES[workout["zone"]].copy()
            
            base_info["duration"] = workout['duration']
            base_info["details"] += f"<br>【推奨メニュー】{workout['duration']} {workout['intervals']} (推定TSS: {workout['tss']})"
        else:
            # TSS上限に合うメニューがない場合、強制的にリカバリー
            base_info = TRAINING_TYPES["Recovery"].copy()
            base_info["details"] = "⚠️ TSS許容上限が非常に低いため、積極的休養（リカバリー）を強く推奨します。"
            base_info["duration"] = "—"
    else:
        base_info["details"] += f"<br>【メニュー】{base_info['duration']}" 
        base_info["details"] += base_info['intervals'] if base_info['intervals'] else ''

    # 最終的なワット数変換を適用
    training_info = format_training_with_ftp(base_info, ftp)

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
