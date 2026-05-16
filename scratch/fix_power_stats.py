"""
pmc_history の p5/p50/p95 を全件埋め直すスクリプト。
Strava API からパワーデータを直接取得し、watts_data を activities に保存した上で
pmc_history を更新する。
"""
import sys
import os
import time
import httpx
import numpy as np
from datetime import datetime, timezone
from google.cloud import firestore

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def refresh_token_if_needed(user_data: dict) -> str:
    """トークンが期限切れなら更新して新しいアクセストークンを返す"""
    from core.config import settings
    expires_at = user_data.get("token_expires_at", 0)
    now = datetime.now(timezone.utc).timestamp()
    
    if now >= expires_at - 300:  # 5分前に更新
        print("  Refreshing access token...")
        resp = httpx.post("https://www.strava.com/oauth/token", data={
            "client_id": settings.STRAVA_CLIENT_ID,
            "client_secret": settings.STRAVA_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": user_data["refresh_token"],
        })
        if resp.status_code == 200:
            data = resp.json()
            # Firestoreにも更新トークンを保存
            db = firestore.Client()
            db.collection("users").document(user_data["strava_athlete_id"]).update({
                "access_token": data["access_token"],
                "refresh_token": data["refresh_token"],
                "token_expires_at": data["expires_at"],
            })
            print(f"  Token refreshed, expires at {data['expires_at']}")
            return data["access_token"]
        else:
            print(f"  Token refresh failed: {resp.status_code} {resp.text}")
            return user_data.get("access_token")
    
    return user_data.get("access_token")


def fetch_power_for_activity(activity_id: str, access_token: str) -> list:
    """Strava APIからパワーストリームを取得"""
    url = f"https://www.strava.com/api/v3/activities/{activity_id}/streams?keys=watts&key_by_type=true"
    headers = {"Authorization": f"Bearer {access_token}"}
    with httpx.Client() as client:
        resp = client.get(url, headers=headers)
    if resp.status_code == 200:
        streams = resp.json()
        if "watts" in streams:
            return streams["watts"]["data"]
    elif resp.status_code == 429:
        print("  Rate limited! Waiting 60s...")
        time.sleep(60)
        # 再試行
        with httpx.Client() as client:
            resp = client.get(url, headers=headers)
        if resp.status_code == 200:
            streams = resp.json()
            if "watts" in streams:
                return streams["watts"]["data"]
    return []


def run():
    db = firestore.Client()
    users = db.collection("users").get()
    
    for user_doc in users:
        user_id = user_doc.id
        user_data = user_doc.to_dict()
        user_data["strava_athlete_id"] = user_id
        print(f"\n=== User: {user_id} ===")
        
        # トークン更新
        access_token = refresh_token_if_needed(user_data)
        
        pmc_history = user_data.get("pmc_history", [])
        if not pmc_history:
            print("  No pmc_history. Skip.")
            continue
        
        updated_count = 0
        new_pmc_history = []
        
        for entry in pmc_history:
            date_str = entry.get("date")
            
            # p5が既にあればスキップ
            if entry.get("p5") is not None:
                new_pmc_history.append(entry)
                continue
            
            # TSS=0の日はパワーデータなし（休息日）なのでスキップ
            if entry.get("tss", 0) == 0:
                new_pmc_history.append(entry)
                continue
            
            print(f"  Processing {date_str} (tss={entry.get('tss')})...")
            
            # その日のアクティビティをFirestoreから取得
            acts_ref = db.collection("users").document(user_id).collection("activities")
            acts_today = acts_ref.where("date", "==", date_str).get()
            
            combined_watts = []
            for act_doc in acts_today:
                act_data = act_doc.to_dict()
                act_id = act_doc.id
                
                if act_data.get("watts_data"):
                    combined_watts.extend(act_data["watts_data"])
                    print(f"    {act_id}: Using cached watts_data ({len(act_data['watts_data'])} points)")
                else:
                    # Strava APIから取得
                    watts = fetch_power_for_activity(act_id, access_token)
                    if watts:
                        combined_watts.extend(watts)
                        # Firestoreにキャッシュとして保存
                        acts_ref.document(act_id).update({"watts_data": watts})
                        print(f"    {act_id}: Fetched from Strava ({len(watts)} points)")
                    else:
                        print(f"    {act_id}: No power data (non-power sport?)")
                
                time.sleep(0.3)  # API rate limit対策
            
            # パワー分布を計算
            filtered = [w for w in combined_watts if w > 20]
            if filtered:
                entry = dict(entry)  # コピー
                entry["p5"] = round(float(np.percentile(filtered, 5)), 1)
                entry["p50"] = round(float(np.percentile(filtered, 50)), 1)
                entry["p95"] = round(float(np.percentile(filtered, 95)), 1)
                updated_count += 1
                print(f"    -> p5={entry['p5']}, p50={entry['p50']}, p95={entry['p95']}")
            else:
                print(f"    -> No power data found for {date_str}")
            
            new_pmc_history.append(entry)
        
        # Firestoreを更新
        if updated_count > 0:
            db.collection("users").document(user_id).update({
                "pmc_history": new_pmc_history
            })
            print(f"\n  Updated {updated_count} entries for user {user_id}.")
        else:
            print(f"\n  No updates needed for user {user_id}.")


if __name__ == "__main__":
    run()
