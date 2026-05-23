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
    wbal_trend_vals = []
    
    for act in trend_activities:
        m = act.get("metrics", {})
        np_val = m.get("normalized_power")
        hr_val = m.get("average_heartrate_active") or act.get("average_heartrate")
        if np_val and hr_val and hr_val > 0:
            ef_trend_vals.append(np_val / hr_val)
        
        dec = m.get("aerobic_decoupling_pct")
        if dec is not None:
            decoupling_trend_vals.append(dec)
            
        cad = m.get("cadence_dropoff_rpm")
        if cad is not None:
            cadence_trend_vals.append(cad)

        wbal = m.get("wbal_drop_kj")
        if wbal is not None:
            wbal_trend_vals.append(wbal)
            
    ef_slope = calculate_slope(ef_trend_vals)
    decoupling_slope = calculate_slope(decoupling_trend_vals)
    cadence_slope = calculate_slope(cadence_trend_vals)
    wbal_slope = calculate_slope(wbal_trend_vals)
    
    # Calculate historical averages
    avg_np = round(float(np.mean([m["normalized_power"] for m in past_metrics if m.get("normalized_power") is not None])), 1) if any(m.get("normalized_power") is not None for m in past_metrics) else None
    avg_vi = round(float(np.mean([m["variability_index"] for m in past_metrics if m.get("variability_index") is not None])), 2) if any(m.get("variability_index") is not None for m in past_metrics) else None
    avg_decoupling = round(float(np.mean([m["aerobic_decoupling_pct"] for m in past_metrics if m.get("aerobic_decoupling_pct") is not None])), 2) if any(m.get("aerobic_decoupling_pct") is not None for m in past_metrics) else None
    avg_cadence_dropoff = round(float(np.mean([m["cadence_dropoff_rpm"] for m in past_metrics if m.get("cadence_dropoff_rpm") is not None])), 1) if any(m.get("cadence_dropoff_rpm") is not None for m in past_metrics) else None
    avg_wbal_drop = round(float(np.mean([m["wbal_drop_kj"] for m in past_metrics if m.get("wbal_drop_kj") is not None])), 1) if any(m.get("wbal_drop_kj") is not None for m in past_metrics) else None
    avg_cadence_active = round(float(np.mean([m["average_cadence_pedaling"] for m in past_metrics if m.get("average_cadence_pedaling") is not None])), 1) if any(m.get("average_cadence_pedaling") is not None for m in past_metrics) else None
    
    # Calculate historical EF averages
    past_efs = []
    for x in past_activities:
        m = x.get("metrics", {})
        np_val = m.get("normalized_power")
        hr_val = m.get("average_heartrate_active") or x.get("average_heartrate")
        if np_val and hr_val and hr_val > 0:
            past_efs.append(np_val / hr_val)
    avg_ef = round(float(np.mean(past_efs)), 2) if past_efs else None
    
    baseline = {
        "avg_np": avg_np,
        "avg_vi": avg_vi,
        "avg_decoupling": avg_decoupling,
        "avg_cadence_dropoff": avg_cadence_dropoff,
        "avg_ef": avg_ef,
        "avg_cadence_active": avg_cadence_active,
        "ef_slope": ef_slope,
        "decoupling_slope": decoupling_slope,
        "cadence_slope": cadence_slope,
        "avg_wbal_drop": avg_wbal_drop,
        "wbal_slope": wbal_slope,
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
        
    metrics = act_data.get("metrics", {})
    current_np = metrics.get("normalized_power")
    current_hr = metrics.get("average_heartrate_active") or act_data.get("average_heartrate")

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
    
    # 2. Get PRE-training TSB/CTL context
    pmc = user_data.get("pmc_history", [])
    pre_training_tsb = 0.0
    pre_training_ctl = 0.0
    post_training_tsb = 0.0
    if pmc:
        act_date = act_data.get("date")
        target_idx = -1
        for i, day in enumerate(pmc):
            if day["date"] == act_date:
                target_idx = i
                break
                
        if target_idx > 0:
            # Pre-training metrics come from the day before
            pre_day = pmc[target_idx - 1]
            pre_training_tsb = pre_day.get("tsb", 0.0)
            pre_training_ctl = pre_day.get("ctl", 0.0)
            post_training_tsb = pmc[target_idx].get("tsb", 0.0)
        elif target_idx == 0:
            pre_training_tsb = pmc[0].get("tsb", 0.0)
            pre_training_ctl = pmc[0].get("ctl", 0.0)
            post_training_tsb = pmc[0].get("tsb", 0.0)
        else:
            pre_training_tsb = pmc[-1].get("tsb", 0.0)
            pre_training_ctl = pmc[-1].get("ctl", 0.0)
            post_training_tsb = pmc[-1].get("tsb", 0.0)
        
    # 3. Build Prompt for AI personal coach
    metrics = act_data.get("metrics", {})
    tiz_str = ", ".join([f"{k}: {v}%" for k, v in metrics.get("time_in_zones", {}).items() if v > 0])
    
    # Calculate EF and IF for current activity
    current_hr = metrics.get("average_heartrate_active") or act_data.get("average_heartrate")
    current_np = metrics.get("normalized_power")
    current_ef = round(current_np / current_hr, 2) if current_np and current_hr and current_hr > 0 else None
    if_val = round(current_np / ftp, 2) if current_np and ftp and ftp > 0 else None
    
    comp_performance = ""
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

        wbal_trend_str = "データ不足"
        if baseline.get("wbal_slope") is not None:
            slope = baseline["wbal_slope"]
            if slope < -0.5:
                wbal_trend_str = f"向上傾向（無駄脚の減少） 📈 ({round(slope, 1)} kJ/回)"
            elif slope > 0.5:
                wbal_trend_str = f"悪化傾向（パワーの荒れ） 📉 (+{round(slope, 1)} kJ/回)"
            else:
                wbal_trend_str = "安定 ➡️"

        comp_performance = f"""
【パフォーマンス比較: 今回 vs 過去の類似ライド平均 ({baseline['count']}件)】

- 有酸素効率 (EF = NP / ActiveHR)
  今回: {current_ef if current_ef is not None else 'データなし'}
  過去平均: {baseline['avg_ef'] if baseline['avg_ef'] is not None else 'データなし'}
  トレンド: {ef_trend_str}

- 有酸素デカップリング (Drift)
  今回: {metrics.get('aerobic_decoupling_pct', 'データなし')}%
  過去平均: {baseline['avg_decoupling'] if baseline['avg_decoupling'] is not None else 'データなし'}%
  トレンド: {dec_trend_str}

- ケイデンス低下 (後半のタレ)
  今回: {metrics.get('cadence_dropoff_rpm', 'データなし')} rpm
  過去平均: {baseline['avg_cadence_dropoff'] if baseline['avg_cadence_dropoff'] is not None else 'データなし'} rpm
  トレンド: {cad_trend_str}

- 平均ケイデンス (実走時)
  今回: {metrics.get('average_cadence_pedaling', 'データなし')} rpm
  過去平均: {baseline['avg_cadence_active'] if baseline['avg_cadence_active'] is not None else 'データなし'} rpm

- 無酸素バッテリー消費 (W'bal Drop)
  今回: {metrics.get('wbal_drop_kj', 'データなし')} kJ
  過去平均: {baseline['avg_wbal_drop'] if baseline['avg_wbal_drop'] is not None else 'データなし'} kJ
  トレンド: {wbal_trend_str}

- 参考情報:
  平均NP {baseline['avg_np']} W
  平均VI {baseline['avg_vi']}
"""
    else:
        comp_performance = f"""
【今回のライド】\n比較可能な同条件の過去ライドはありません（今回が初回です）。

- 有酸素効率 (EF = NP / ActiveHR)
  今回: {current_ef if current_ef is not None else 'データなし'}

- 有酸素デカップリング (Drift)
  今回: {metrics.get('aerobic_decoupling_pct', 'データなし')}%

- ケイデンス低下 (後半のタレ)
  今回: {metrics.get('cadence_dropoff_rpm', 'データなし')} rpm

- 平均ケイデンス (実走時)
  今回: {metrics.get('average_cadence_pedaling', 'データなし')} rpm

- 無酸素バッテリー消費 (W'bal Drop)
  今回: {metrics.get('wbal_drop_kj', 'データなし')} kJ
""" 
        
    rpe_context = f"ユーザーの自己申告キツさ (RPE, 1-10段階): {rpe_val if rpe_val is not None else 'スキップ（未申告）'}"

    metrics_mean = """
【各メトリクスの実戦的意味】

■ EF (有酸素効率)
- 心拍あたりどれだけ出力できているか
- 高いほど「少ない心拍で大きな出力を維持できている」
- EF上昇傾向は、有酸素適応やベース向上の可能性
- ただし単独では判断しない

■ デカップリング (Drift)
- ライド後半で心拍効率がどれだけ崩れたか
- <3%: 非常に良好〜正常範囲
- 3〜5%: 通常範囲
- >5%: 持久疲労・補給不足・暑熱・回復不足の可能性
- 1.5%未満の差はノイズの可能性が高い
- 単独で疲労判定しない

■ ケイデンス
- 高疲労時は高回転維持が崩れ、トルク依存になりやすい
- 85〜95rpm維持は神経系が安定している良好兆候
- 後半に大きく落ちる場合、筋持久疲労の可能性
- 2rpm未満の差はノイズの可能性

■ W'bal Drop
- FTP超過領域で「どれだけ脚を削ったか」
- < 1 kJ
  - ほぼ純有酸素
  - steady endurance
  - recovery/tempo寄り
  - 「脚を削った感」かなり少ない
- 1〜3 kJ
  - 軽いsurgeあり
  - 疲労コストまだ低い
- 3〜6 kJ
  - 明確に高強度寄与あり
  - 神経筋疲労増える
  - 翌日に少し残りやすい
- 6〜10 kJ 
  - race-like
  - 無酸素寄与かなり大
  - 「脚を使った感」が強い
- >10 kJ
  - 高強度連発
  - 回復コスト大
  - TSS以上に疲れる可能性
- トレーニング内容を表し、単なる「疲労量」ではなく「疲労の種類」を見る指標

■ VI
- フリーライド:
  - 1.00〜1.05: 極めて均一
  - 1.05〜1.10: 実戦的で良好
  - >1.15: 踏み直し過多
- ERGワークアウト時のVIは評価対象外

【超重要】
- 単一メトリクスから断定しない
- 数値を読み上げるのではなく「身体で何が起きたか」を推論すること
- 微小差を過大評価しない
- 「問題なし」という結論を積極的に許可する
- 異常探しAIにならない
"""

    prompt = f"""
あなたは、耐久スポーツの実戦経験を持つ高レベルコーチです。

役割は「数値説明」ではありません。
複数メトリクスの関係性から、

- 今日どんな刺激が入ったか
- 身体がどう反応したか
- どんな能力が伸び始めているか
- どんな疲労が発生したか
- 今の状態で積めているか

を推論してください。

【最重要ルール】
- 各メトリクスを単独評価してはいけない
- 必ず複数メトリクスを関連付けて解釈する
- 「異常探し」をしない
- 正常範囲なら「良好」「問題なし」と言い切ってよい
- 数値そのものではなく、生理学的意味を語ること
- 良い適応が見えている場合は積極的に評価すること

{metrics_mean}

【複合メトリクス解釈ルール】

■ IF × RPE
- IF高めなのにRPE低:
  有酸素ベース拡張・tempo/SST耐性向上の可能性
- IF低いのにRPE高:
  疲労・暑熱・回復不足の可能性

■ TSB × 心拍 × デカップリング × ケイデンス
- TSBマイナスでも:
  - 心拍安定
  - デカップリング低
  - 高回転維持
  が揃う場合:
  「疲労を吸収しながら処理できている」
  と解釈する

- ケイデンス低下 + RPE上昇:
  トルク依存・筋疲労優位の可能性

■ W'bal Drop × VI × マッチ消費
- W'bal Drop大:
  レース的・神経筋的疲労
- W'bal Drop小:
  steady aerobic寄り

- VI高 + W'bal Drop大:
  踏み直し・加減速負荷が大きい

- VI適度 + マッチ消費適量:
  実戦的負荷への適応の可能性

■ EF × デカップリング
- EF良好 + デカップリング低:
  有酸素効率安定
- EF悪化 + デカップリング増:
  持久疲労・回復不足・補給不足の可能性

{comp_performance}

【その他の今回のライドデータ】

- 物理的仕事量 (Total Work):{metrics.get('total_work_kj')} kJ
- 強度係数 (IF): {if_val}
- 平均心拍 (走行中のみ):{metrics.get('average_heartrate_active')} bpm
- マッチ消費数(120% FTPを15秒以上連続で超えた回数): {metrics.get('matches_burned')} 回
- ゾーン滞在時間 (Time in Zones): {tiz_str}

【コンディション】

- ライド前CTL: {pre_training_ctl}
- ライド前TSB: {pre_training_tsb}
- ライド後TSB予測: {post_training_tsb}
- {rpe_context}

【ライド種別別ルール】

■ ERGワークアウト
- VIは無視
- 注視点:
  - ケイデンス維持
  - IF達成度
  - RPEとのギャップ
  - デカップリング
  - W'bal Drop

- 高IFなのにW'bal Drop小:
  FTP設定が低い可能性も考慮

■ フリーライド / 実走
- VI・W'bal Drop・マッチ消費を重視
- 適度なVIは実戦適応として肯定的に解釈
- 「ペースが荒い」だけで減点しない

【出力ルール】

- 300〜500文字
- 結論から書く
- 挨拶禁止
- 無駄な一般論禁止
- 数値の音読禁止
- 「身体で何が起きたか」を説明する
- 良い点と注意点を両方書く
- 問題なければ「特になし」と明言する

【出力構成（マークダウン形式）】
1. **ライド総括**: （今日のライドが身体に与えた刺激を1〜2行で）
2. **アクティビティ内容**: (アクティビティ内容から期待されるトレーニング効果)
3. **特に良かった点**: （複合メトリクスから分かる身体適応）
4. **注意点・懸念**: （問題なければ「特になし」でよい）
5. **次回へのヒント**: （次回このタイプをやる際の具体的改善を1行）
"""

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
