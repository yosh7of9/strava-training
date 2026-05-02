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
    initial_atl: float = Form(0.0)
):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse(url="/", status_code=303)
        
    db = get_db()
    user_ref = db.collection("users").document(user_id)
    
    update_data = {
        "ftp": ftp,
        "max_hr": max_hr,
        "initial_ctl": initial_ctl,
        "initial_atl": initial_atl
    }
    
    user_ref.update(update_data)
    
    return RedirectResponse(url="/dashboard", status_code=303)
