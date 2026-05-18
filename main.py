from fastapi import FastAPI, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from core.config import settings
from routers import auth, settings as settings_router, sync as sync_router, webhook, processor, api as api_router, recommend as recommend_router, activity as activity_router
from core.database import get_db
from google.cloud import firestore

app = FastAPI(title="Strava Training Dashboard")

# Add session middleware
app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY)

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Jinja2 templates
templates = Jinja2Templates(directory="templates")

# Include routers
app.include_router(auth.router)
app.include_router(settings_router.router)
app.include_router(sync_router.router)
app.include_router(webhook.router)
app.include_router(processor.router)
app.include_router(api_router.router)
app.include_router(recommend_router.router)
app.include_router(activity_router.router)

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    # If already logged in, redirect to dashboard
    if request.session.get("user_id"):
        return RedirectResponse(url="/dashboard", status_code=303)
        
    return templates.TemplateResponse(
        request=request, name="index.html", context={"title": "Strava Dashboard"}
    )

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse(url="/", status_code=303)
        
    # Fetch user info
    db = get_db()
    user_doc = db.collection("users").document(user_id).get()
    
    if not user_doc.exists:
        request.session.clear()
        return RedirectResponse(url="/", status_code=303)
        
    user_data = user_doc.to_dict()
    
    # Fetch Today's or Yesterday's activities (JST)
    from datetime import datetime, timedelta, timezone
    jst = timezone(timedelta(hours=9))
    now_jst = datetime.now(jst)
    today_str = now_jst.strftime("%Y-%m-%d")
    yesterday_str = (now_jst - timedelta(days=1)).strftime("%Y-%m-%d")
    
    last_activity = None
    activities_ref = db.collection("users").document(user_id).collection("activities")
    
    # Try Today first
    today_acts = activities_ref.where("date", "==", today_str).get()
    target_acts = today_acts
    target_date_label = "Today"
    
    # If no activity today, try Yesterday
    if not today_acts:
        yesterday_acts = activities_ref.where("date", "==", yesterday_str).get()
        target_acts = yesterday_acts
        target_date_label = "Yesterday"
    
    if target_acts:
        total_tss = 0
        names = []
        for act in target_acts:
            d = act.to_dict()
            total_tss += d.get("tss", 0)
            names.append(d.get("name", "Activity"))
        
        last_activity = {
            "tss": total_tss,
            "name": f"[{target_date_label}] " + ", ".join(names)
        }
        
    # Fetch the absolute latest activity to check for is_new_activity and AI Feedback
    latest_act_query = activities_ref.order_by("start_date", direction=firestore.Query.DESCENDING).limit(1).get()
    latest_activity_data = None
    is_new_activity = False
    new_activity_id = None
    
    if latest_act_query:
        latest_doc = latest_act_query[0]
        latest_activity_data = latest_doc.to_dict()
        latest_activity_data["id"] = latest_doc.id
        
        # Check if it needs RPE popup (only show if is_new_activity is True AND rpe has not been set yet)
        if latest_activity_data.get("is_new_activity") is True and latest_activity_data.get("rpe") is None:
            is_new_activity = True
            new_activity_id = latest_doc.id
            
    return templates.TemplateResponse(
        request=request, name="dashboard.html", context={
            "title": "My Dashboard", 
            "user": user_data,
            "last_activity": last_activity,
            "latest_activity": latest_activity_data,
            "is_new_activity": is_new_activity,
            "new_activity_id": new_activity_id
        }
    )
