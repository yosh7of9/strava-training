from fastapi import APIRouter, Request, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from core.database import get_db

router = APIRouter(prefix="/settings", tags=["settings"])
templates = Jinja2Templates(directory="templates")

@router.get("/", response_class=HTMLResponse)
async def view_settings(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse(url="/", status_code=303)
        
    db = get_db()
    user_doc = db.collection("users").document(user_id).get()
    if not user_doc.exists:
        return RedirectResponse(url="/auth/logout", status_code=303)
        
    user_data = user_doc.to_dict()
    
    return templates.TemplateResponse(
        request=request, name="settings.html", context={
            "title": "Settings",
            "user": user_data
        }
    )

@router.post("/")
async def update_settings(
    request: Request,
    ftp: int = Form(...),
    max_hr: int = Form(...),
    initial_ctl: float = Form(0.0),
    initial_atl: float = Form(0.0),
    schedule_mon: str = Form("Endurance"),
    schedule_tue: str = Form("SST"),
    schedule_wed: str = Form("Endurance"),
    schedule_thu: str = Form("Threshold"),
    schedule_fri: str = Form("Endurance"),
    schedule_sat: str = Form("Long Endurance"),
    schedule_sun: str = Form("Rest"),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse(url="/", status_code=303)
        
    db = get_db()
    user_ref = db.collection("users").document(user_id)
    
    from datetime import datetime, timezone
    user_data = user_ref.get().to_dict()
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ftp_history = user_data.get("ftp_history", [])
    if not ftp_history or ftp_history[-1]["ftp"] != ftp:
        ftp_history.append({"date": today_str, "ftp": ftp})

    update_data = {
        "ftp": ftp,
        "max_hr": max_hr,
        "initial_ctl": initial_ctl,
        "initial_atl": initial_atl,
        "ftp_history": ftp_history,
        "weekly_schedule": {
            "mon": schedule_mon,
            "tue": schedule_tue,
            "wed": schedule_wed,
            "thu": schedule_thu,
            "fri": schedule_fri,
            "sat": schedule_sat,
            "sun": schedule_sun,
        }
    }
    
    user_ref.update(update_data)
    
    return RedirectResponse(url="/dashboard", status_code=303)
