from google.cloud import firestore
db = firestore.Client(project="strava-training-dashboard")
users = db.collection("users").stream()
for user in users:
    data = user.to_dict()
    pmc = data.get("pmc_history", [])
    print(f"User: {user.id}, pmc_history length: {len(pmc)}")
    if pmc:
        print(f"Sample data: {pmc[-1]}")
