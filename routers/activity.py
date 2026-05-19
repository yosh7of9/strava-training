import os
import httpx
import numpy as np
from datetime import datetime, timezone
from fastapi import APIRouter, Request, HTTPException, Form, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from google.cloud import firestore
from core.database import get_db
from core.config import settings

router = APIRouter(prefix="/activity", tags=["activity"])
templates = Jinja2Templates(directory="templates")

# Advanced helper to fetch recent same-type activities, calculate EF (Efficiency Factor) and trend slopes (linear regression)
async def get_historical_baseline(activities_ref, profile_key, current_act_id, limit=5):
    if not profile_key:
        return None
        
    # Query past activities with the same profile_key (No composite index required!)
    docs = activities_ref.where("profile_key", "==", profile_key).get()
    
    activities = []
    for doc in docs:
        d = doc.to_dict()
        d["id"] = doc.id
        if "metrics" in d and d["metrics"]:
            activities.append(d)
            
    # Sort chronologically by date ascending for trend analysis
    activities.sort(key=lambda x: x.get("date", ""))
    
    # Locate current activity index
    current_index = -1
    for i, act in enumerate(activities):
        if act["id"] == current_act_id:
            current_index = i
            break
            
    # Chronological sliding window of up to 5 sessions leading up to and including current
    if current_index != -1:
        trend_activities = activities[max(0, current_index - 4) : current_index + 1]
    else:
        trend_activities = activities[-5:]
        
    # Historical baseline excludes the current activity
    past_activities = [act for act in activities if act["id"] != current_act_id]
    # Sort descending to get the most recent ones first for the baseline average
    past_activities.sort(key=lambda x: x.get("date", ""), reverse=True)
    past_activities = past_activities[:limit]
    
    past_metrics = [x["metrics"] for x in past_activities]
    if not past_metrics:
        return None
        
    # Slope (linear regression) helper using numpy
    def calculate_slope(y_values):
        if len(y_values) < 3:
            return None
        try:
            x = np.arange(len(y_values))
            slope, _ = np.polyfit(x, y_values, 1)
            return round(float(slope), 4)
        except Exception:
            return None

    # Calculate trends over sliding window
    ef_trend_vals = []
    decoupling_trend_vals = []
    cadence_trend_vals = []
    
    for act in trend_activities:
        m = act.get("metrics", {})
        np_val = m.get("normalized_power")
        hr_val = act.get("average_heartrate")
        if np_val and hr_val and hr_val > 0:
            ef_trend_vals.append(np_val / hr_val)
        
        dec = m.get("aerobic_decoupling_pct")
        if dec is not None:
            decoupling_trend_vals.append(dec)
            
        cad = m.get("cadence_dropoff_rpm")
        if cad is not None:
            cadence_trend_vals.append(cad)
            
    ef_slope = calculate_slope(ef_trend_vals)
    decoupling_slope = calculate_slope(decoupling_trend_vals)
    cadence_slope = calculate_slope(cadence_trend_vals)
    
    # Calculate historical averages
    avg_np = round(float(np.mean([m["normalized_power"] for m in past_metrics])), 1)
    avg_vi = round(float(np.mean([m["variability_index"] for m in past_metrics])), 2)
    avg_decoupling = round(float(np.mean([m["aerobic_decoupling_pct"] for m in past_metrics if m.get("aerobic_decoupling_pct") is not None])), 2) if any(m.get("aerobic_decoupling_pct") is not None for m in past_metrics) else None
    avg_cadence_dropoff = round(float(np.mean([m["cadence_dropoff_rpm"] for m in past_metrics if m.get("cadence_dropoff_rpm") is not None])), 1) if any(m.get("cadence_dropoff_rpm") is not None for m in past_metrics) else None
    
    # Calculate historical EF averages
    past_efs = []
    for x in past_activities:
        m = x.get("metrics", {})
        np_val = m.get("normalized_power")
        hr_val = x.get("average_heartrate")
        if np_val and hr_val and hr_val > 0:
            past_efs.append(np_val / hr_val)
    avg_ef = round(float(np.mean(past_efs)), 2) if past_efs else None
    
    baseline = {
        "avg_np": avg_np,
        "avg_vi": avg_vi,
        "avg_decoupling": avg_decoupling,
        "avg_cadence_dropoff": avg_cadence_dropoff,
        "avg_ef": avg_ef,
        "ef_slope": ef_slope,
        "decoupling_slope": decoupling_slope,
        "cadence_slope": cadence_slope,
        "count": len(past_activities)
    }
    return baseline

async def call_gemini_api(prompt: str) -> str:
    api_key = settings.GEMINI_API_KEY or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return "Error: GEMINI_API_KEY settings or environment variable is not set."
        
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    payload = {
        "contents": [{
            "parts": [{"text": prompt}]
        }],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 8192
        }
    }
    
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(url, json=payload, timeout=30.0)
            if resp.status_code != 200:
                return f"Gemini API returned error code {resp.status_code}: {resp.text}"
            
            result = resp.json()
            feedback = result["candidates"][0]["content"]["parts"][0]["text"]
            return feedback
        except Exception as e:
            return f"Failed to generate coaching feedback due to API connection error: {str(e)}"

@router.get("/history", response_class=HTMLResponse)
async def activity_history(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse(url="/", status_code=303)
        
    db = get_db()
    user_doc = db.collection("users").document(user_id).get()
    if not user_doc.exists:
        return RedirectResponse(url="/auth/logout", status_code=303)
        
    # Fetch recent activities (limit to past 90 days / approx 3 months in JST)
    from datetime import datetime, timedelta, timezone
    jst = timezone(timedelta(hours=9))
    three_months_ago = (datetime.now(jst) - timedelta(days=90)).strftime("%Y-%m-%d")
    
    acts_ref = db.collection("users").document(user_id).collection("activities")
    acts = acts_ref.where("date", ">=", three_months_ago).order_by("date", direction=firestore.Query.DESCENDING).get()
    
    activity_list = []
    for doc in acts:
        d = doc.to_dict()
        d["id"] = doc.id
        activity_list.append(d)
        
    return templates.TemplateResponse(
        request=request, name="activity_history.html", context={
            "title": "Activity History",
            "activities": activity_list,
            "user": user_doc.to_dict()
        }
    )

@router.get("/{act_id}", response_class=HTMLResponse)
async def activity_detail(request: Request, act_id: str):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse(url="/", status_code=303)
        
    db = get_db()
    user_doc = db.collection("users").document(user_id).get()
    if not user_doc.exists:
        return RedirectResponse(url="/auth/logout", status_code=303)
        
    act_doc = db.collection("users").document(user_id).collection("activities").document(act_id).get()
    if not act_doc.exists:
        raise HTTPException(status_code=404, detail="Activity not found")
        
    act_data = act_doc.to_dict()
    act_data["id"] = act_doc.id
    if "metrics" not in act_data or not act_data["metrics"]:
        act_data["metrics"] = {}
        
    # Calculate Efficiency Factor (EF) and Intensity Factor (IF) for the current activity
    current_hr = act_data.get("average_heartrate")
    current_np = act_data.get("metrics", {}).get("normalized_power")
    if current_np and current_hr and current_hr > 0:
        act_data["metrics"]["efficiency_factor"] = round(current_np / current_hr, 2)
    else:
        act_data["metrics"]["efficiency_factor"] = None
        
    user_data = user_doc.to_dict()
    ftp = user_data.get("ftp", 200)
    if current_np and ftp and ftp > 0:
        act_data["metrics"]["intensity_factor"] = round(current_np / ftp, 2)
    else:
        act_data["metrics"]["intensity_factor"] = None
    
    # Calculate baseline for display compare if possible
    baseline = None
    if "profile_key" in act_data:
        acts_ref = db.collection("users").document(user_id).collection("activities")
        baseline = await get_historical_baseline(acts_ref, act_data["profile_key"], act_id)
        
    is_workout = "workout" in act_data.get("name", "").lower() or "ワークアウト" in act_data.get("name", "").lower() or act_data.get("workout_type") == 3
        
    return templates.TemplateResponse(
        request=request, name="activity_detail.html", context={
            "title": act_data.get("name", "Activity Detail"),
            "activity": act_data,
            "baseline": baseline,
            "user": user_doc.to_dict(),
            "is_workout": is_workout
        }
    )

@router.post("/{act_id}/evaluate")
async def evaluate_activity(request: Request, act_id: str):
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"error": "Unauthorized"})
        
    # Get JSON payload
    try:
        payload = await request.json()
        rpe = payload.get("rpe")
    except Exception:
        # Fallback to Form Data if needed
        rpe = None
        
    db = get_db()
    user_ref = db.collection("users").document(user_id)
    user_doc = user_ref.get()
    user_data = user_doc.to_dict()
    
    activities_ref = user_ref.collection("activities")
    act_doc = activities_ref.document(act_id).get()
    if not act_doc.exists:
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"error": "Activity not found"})
        
    act_data = act_doc.to_dict()
    ftp = user_data.get("ftp", 200)
    is_workout = "workout" in act_data.get("name", "").lower() or "ワークアウト" in act_data.get("name", "").lower() or act_data.get("workout_type") == 3
    workout_type_str = "ワークアウト (ERGモード自動制御)" if is_workout else "通常のライド (フリーライド / レース / グループライド等、自主的ペーシング)"
    
    # RPE conversion (ensure it's int or None)
    rpe_val = int(rpe) if rpe is not None and str(rpe).isdigit() else None
    
    # 1. Fetch same-type historical baseline
    profile_key = act_data.get("profile_key")
    baseline = await get_historical_baseline(activities_ref, profile_key, act_id)
    
    # 2. Get current TSB/CTL/ATL context
    pmc = user_data.get("pmc_history", [])
    current_tsb = 0.0
    current_ctl = 0.0
    if pmc:
        # Match the date of the activity to find TSB on that day
        act_date = act_data.get("date")
        matching_day = next((day for day in pmc if day["date"] == act_date), pmc[-1])
        current_tsb = matching_day.get("tsb", 0.0)
        current_ctl = matching_day.get("ctl", 0.0)
        
    # 3. Build Prompt for AI personal coach
    metrics = act_data.get("metrics", {})
    tiz_str = ", ".join([f"{k}: {v}%" for k, v in metrics.get("time_in_zones", {}).items() if v > 0])
    
    # Calculate EF and IF for current activity
    current_hr = act_data.get("average_heartrate")
    current_np = metrics.get("normalized_power")
    current_ef = round(current_np / current_hr, 2) if current_np and current_hr and current_hr > 0 else None
    if_val = round(current_np / ftp, 2) if current_np and ftp and ftp > 0 else None
    
    history_context = ""
    if baseline:
        ef_trend_str = "データ不足"
        if baseline.get("ef_slope") is not None:
            slope = baseline["ef_slope"]
            if slope > 0.005:
                ef_trend_str = f"向上傾向 📈 (+{round(slope * 100, 1)}%/回)"
            elif slope < -0.005:
                ef_trend_str = f"低下傾向 📉 ({round(slope * 100, 1)}%/回)"
            else:
                ef_trend_str = "安定・横ばい ➡️"
                
        dec_trend_str = "データ不足"
        if baseline.get("decoupling_slope") is not None:
            slope = baseline["decoupling_slope"]
            if slope < -0.1:
                dec_trend_str = f"向上傾向（減少） 📈 ({round(slope, 1)}%/回)"
            elif slope > 0.1:
                dec_trend_str = f"悪化傾向（増加） 📉 (+{round(slope, 1)}%/回)"
            else:
                dec_trend_str = "安定・変化なし ➡️"

        cad_trend_str = "データ不足"
        if baseline.get("cadence_slope") is not None:
            slope = baseline["cadence_slope"]
            if slope > 0.1:
                cad_trend_str = f"向上傾向（筋肉疲労の軽減） 📈 (+{round(slope, 1)} rpm/回)"
            elif slope < -0.1:
                cad_trend_str = f"悪化傾向（後半タレやすくなっている） 📉 ({round(slope, 1)} rpm/回)"
            else:
                cad_trend_str = "安定・変化なし ➡️"

        history_context = f"""
【過去の類似ライド({baseline['count']}件の平均ベースラインおよび直近5回以内の一次回帰トレンド)】
- 有酸素効率 (EF = NP/HR): 今回 {current_ef if current_ef is not None else 'データなし'} (過去平均: {baseline['avg_ef'] if baseline['avg_ef'] is not None else 'データなし'}) -> 直近トレンド: {ef_trend_str}
- 有酸素デカップリング (Drift): 今回 {metrics.get('aerobic_decoupling_pct', 'データなし')}% (過去平均: {baseline['avg_decoupling'] if baseline['avg_decoupling'] is not None else 'データなし'}%) -> 直近トレンド: {dec_trend_str}
- ケイデンス低下 (Drop-off): 今回 {metrics.get('cadence_dropoff_rpm', 'データなし')} rpm (過去平均: {baseline['avg_cadence_dropoff'] if baseline['avg_cadence_dropoff'] is not None else 'データなし'} rpm) -> 直近トレンド: {cad_trend_str}
- 参考情報 (過去平均の絶対出力): 平均NP {baseline['avg_np']} W, 平均VI {baseline['avg_vi']}

※注意※: パワーやNPの絶対値の増減のみでユーザーの体調や能力を判断してはいけません。今回の強度の意図（軽めのリカバリー走なのか、高強度のトレーニングなのか）を踏まえ、心拍あたりの出力効率を表す「有酸素効率 (EF)」や「デカップリング（Drift）」の数値と、それらの「直近の傾き（トレンド）」を見て、身体が効率的に適応しているか、または慢性疲労に陥っているかを総合的に判断・アドバイスしてください。
"""
    else:
        history_context = "\n【過去の類似ライド】\n比較可能な同条件の過去ライドはありません（今回が初回です）。\n"
        
    rpe_context = f"ユーザーの自己申告キツさ (RPE, 1-10段階): {rpe_val if rpe_val is not None else 'スキップ（未申告）'}"
    
    prompt = f"""
ユーザーから提供されたトレーニングデータを分析【制約事項（超重要・絶対遵守）】
- 丁寧な挨拶、プロのコーチとしての形式的な前置き、退屈な一般論の長文解説は一切排除し、データが示す核心の結論から端的に書き始めてください。
- **データ同士の「掛け算」による複合メトリクス分析（最優先命令）**:
  - 各メトリクスを独立した単体データとして評価することを禁止します。必ず以下の「組み合わせ」から生理学的・神経学的なリアルな状態を推論してください。
    - **【IF (強度) × RPE (主観キツさ)】**: 例えば、IF値が高い（例: 0.80以上＝テンポ〜SST領域）にもかかわらずRPEが比較的低い（例: 4〜5程度）場合、それは「有酸素ベース能力が拡張され、中強度を楽に処理できている（ベースが伸び始めている）」という極めて良好な適応と判断すること。
    - **【TSB (蓄積疲労) × 平均心拍・デカップリング (循環器) × 平均ケイデンス (神経系)】**: 例えば、TSBが大幅にマイナス（深い疲労下）であるにもかかわらず、心拍が安定し、かつ高ケイデンス（90rpm以上など）が崩れずに維持できている場合、「筋肉や心肺に疲労はあるが、ペダリング神経系が破綻せず、トルク頼みの踏み込みに逃げずに処理できている（疲労を良好に吸収できている、積める身体に近づいている）」と解釈すること。逆に、ケイデンスが落ちてトルク寄りになり、RPEが跳ね上がっている場合はオーバーロードと判定すること。
    - **【VI の実戦的解釈】**: ワークアウト（ERG制御）の時はVIが高くて当然（評価対象外）ですが、実走やフリーライドにおける適度なVI（1.08〜1.12など）は、単に「ペースが荒れている」と減点するのではなく「実戦的な集団走での加減速や踏み直しに身体が適応できている良好な兆候」と肯定的に解釈すること。
- **測定誤差の意識（データサイエンス的評価）**: ケイデンス低下の微小な差（例: 2 rpm 未満の差）や、有酸素デカップリングの微小な差（例: 1.5% 未満の差）は、現実的には実質的な差がない「測定誤差・ノイズ」とみなすこと。「劇的な改善」などと過剰評価せず、「最初から最後までペダリングが極めて均一に安定していた」と冷静に評価してください。

【出力構成（マークダウン形式）】
1. **ライドの総括とペーシング評価**: （今回のターゲット強度とペーシングコントロールの適切さを、データに基づき1〜2行でズバッと総括）
2. **今回のライドで特に優れていた点（具体化リスト）**: 
   - 複合メトリクス（IF×RPE、TSB×ケイデンス×心拍等の組み合わせ）から導き出される、「今回のペダリング生理や適応において具体的に何がどう優れていたか」を、数値の説得力を持って箇条書きで具体的にリストアップ（2〜3項目）。
3. **注意すべき点・懸念される兆候（具体化リスト）**: 
   - 蓄積疲労、デカップリングの悪化、ケイデンスの低下などから推測される、懸念点や注意すべきポイントを具体的にリストアップ。特になく完璧な適応を示している場合は、「なし（疲労下でも完全に身体が適応・吸収できています）」と明言すること（1〜2項目）。
4. **今伸びている能力と明日へのアドバイス**: （今回のライド傾向から「今最も成長ポテンシャルが高い領域（例: テンポ域の処理能力）」を特定し、それを伸ばすための明日への具体的アドバイスを提示）
- ケイデンス低下（後半に脚がタレているため悪化。例えば、-5.0 rpm は -0.1 rpm よりも大幅に悪い状態であることに注意してください): {f"{metrics.get('cadence_dropoff_rpm')} rpm" if metrics.get('cadence_dropoff_rpm') is not None else "データなし"}
- マッチ消費数 (120% FTPを15秒以上連続で超えた回数): {metrics.get('matches_burned')} 回
- ゾーン滞在時間 (Time in Zones): {tiz_str}

【ユーザーの体調・コンディション】
- 現在のフィットネス (CTL): {current_ctl}
- 現在の疲労・TSB (トレーニングストレスバランス): {current_tsb}
- {rpe_context}
{history_context}

【制約事項（超重要・絶対遵守）】
- 全体の文字数は **300文字〜500文字程度** に極めてコンパクトにまとめること。PCやスマホの1画面でスクロールせずに一目で要点が把握できるようにしてください。
- 丁寧な挨拶、プロのコーチとしての前置き、無駄な長文解説は一切排除し、結論から端的に書き始めてください。
- 各パート（1〜4）は **短い箇条書きを主体（それぞれ2〜3行以内）** とし、データが示す核心のみをズバッと提示すること。
- **測定誤差の意識（データサイエンス的評価）**: ケイデンス低下の微小な差（例: 2 rpm 未満の差。例えば -0.9 rpm と -0.1 rpm の比較など）や、有酸素デカップリングの微小な差（例: 1.5% 未満の差）は、現実的には実質的な差がない「測定誤差・ノイズ」とみなすこと。「劇的な改善」などと過剰評価せず、「最初から最後までペダリングが極めて均一に安定していた」と冷静に評価してください。
- **ライド種別（ワークアウト vs 通常ライド）に応じた分析指針**:
  - **ワークアウト (ERGモード自動制御) の場合**:
    - **VI (変動指数) の評価は完全に無視・除外すること**: ERGモードでは設定されたメニューインターバルにより機械的にパワーが激しく上下するため、VIが高くなる（例: 1.12〜1.20以上など）のは物理的・システム的に必然です。これを「ペース配分が悪い」などと絶対に誤解・誤判定しないでください（「ワークアウトとして設定通り忠実に出力がこなされた証拠」と解釈すること）。
    - **注視すべき核心**: インターバル（Hardブロック）中のケイデンス維持力（後半タレずに安定して高回転を維持できているか）、有酸素デカップリング（有酸素能力の限界）、および目標強度（IF）の達成感と主観RPEのギャップ。
  - **通常のライド (フリーライド / レース / グループライド) の場合**:
    - **VI (変動指数) と自主的ペース配分の技術を極めて重視すること**: 平坦なエンデュランス走なら 1.00〜1.05 の均一なペーシングができているかを評価し、アップダウンや加減速がある場合は「無駄な踏み込み（マッチの消費）」やパワーの過度なスパイクを抑え、自制心を持った効率的なペースコントロールができたかを評価してください。

【出力構成（マークダウン形式）】
1. **ライドの総括とペーシング評価**: （VIやパワーから今日の狙い通りかを1〜2行で評価）
2. **客観メトリクス解説**: （過去平均トレンドと比較した今日の成長や疲労状況。2〜3行の短い箇条書き）
3. **主観RPEとTSBのギャップ分析**: （TSBと自己申告RPEのギャップから、身体の適応状況や隠れた疲労を1〜2行で鋭く分析）
4. **明日への具体的アドバイス**: （明日の具体的なリカバリーまたはトレーニングメニュー案を1〜2行で提案）
"""

    import os
    ai_feedback = await call_gemini_api(prompt)
    
    # 4. Save to Firestore
    update_payload = {
        "ai_feedback": ai_feedback,
        "is_new_activity": True
    }
    if rpe_val is not None:
        update_payload["rpe"] = rpe_val
        
    activities_ref.document(act_id).update(update_payload)
    
    return JSONResponse(content={
        "success": True,
        "rpe": rpe_val,
        "ai_feedback": ai_feedback
    })


@router.post("/{act_id}/read")
async def mark_activity_as_read(act_id: str, request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse(content={"success": False, "error": "Unauthorized"}, status_code=401)
        
    db = get_db()
    activity_ref = db.collection("users").document(user_id).collection("activities").document(act_id)
    activity_doc = activity_ref.get()
    if not activity_doc.exists:
        return JSONResponse(content={"success": False, "error": "Activity not found"}, status_code=404)
        
    activity_ref.update({"is_new_activity": False})
    return JSONResponse(content={"success": True})

