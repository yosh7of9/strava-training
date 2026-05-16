import os
import sys
import httpx
import numpy as np
from google.cloud import firestore

sys.path.append(os.getcwd())

def backfill():
    db = firestore.Client()
    users = db.collection("users").get()
    
    for user_doc in users:
        user_id = user_doc.id
        user_data = user_doc.to_dict()
        print(f"User: {user_id}")
        
        access_token = user_data.get("access_token")
        pmc_history = user_data.get("pmc_history", [])
        if not pmc_history:
            continue
            
        update_count = 0
        new_pmc_history = []
        
        headers = {"Authorization": f"Bearer {access_token}"}
        
        for entry in pmc_history:
            # We must create a new dict or modify carefully
            date_str = entry.get("date")
            if not date_str:
                new_pmc_history.append(entry)
                continue
            
            # Already has it?
            if "p5" in entry:
                new_pmc_history.append(entry)
                continue
                
            print(f"  Date: {date_str}")
            
            # Get activities
            activities_ref = db.collection("users").document(user_id).collection("activities")
            acts_today = activities_ref.where("date", "==", date_str).get()
            
            combined_watts = []
            for act_doc in acts_today:
                act_data = act_doc.to_dict()
                if "watts_data" in act_data:
                    combined_watts.extend(act_data["watts_data"])
                else:
                    act_id = act_doc.id
                    print(f"    Fetching {act_id}...")
                    url = f"https://www.strava.com/api/v3/activities/{act_id}/streams?keys=watts&key_by_type=true"
                    with httpx.Client() as client:
                        resp = client.get(url, headers=headers)
                        if resp.status_code == 200:
                            streams = resp.json()
                            if "watts" in streams:
                                w_data = streams["watts"]["data"]
                                combined_watts.extend(w_data)
                                activities_ref.document(act_id).update({"watts_data": w_data})
            
            if combined_watts:
                filtered = [w for w in combined_watts if w > 20]
                if filtered:
                    entry["p5"] = round(float(np.percentile(filtered, 5)), 1)
                    entry["p50"] = round(float(np.percentile(filtered, 50)), 1)
                    entry["p95"] = round(float(np.percentile(filtered, 95)), 1)
                    update_count += 1
                    print(f"    Updated: {entry['p5']}, {entry['p50']}, {entry['p95']}")
            
            new_pmc_history.append(entry)
            
        if update_count > 0:
            db.collection("users").document(user_id).update({"pmc_history": new_pmc_history})
            print(f"Successfully updated {update_count} entries for {user_id}.")

if __name__ == "__main__":
    backfill()
