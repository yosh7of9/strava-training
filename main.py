from fastapi import FastAPI, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from core.config import settings
from routers import auth, settings as settings_router, sync as sync_router, webhook, processor, api as api_router
from core.database import get_db

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
        
    return templates.TemplateResponse(
        request=request, name="dashboard.html", context={
            "title": "My Dashboard", 
            "user": user_data
        }
    )
