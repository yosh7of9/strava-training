from fastapi import APIRouter, Request, HTTPException
from core.database import get_db

router = APIRouter(prefix="/api", tags=["api"])

@router.get("/pmc-data")
async def get_pmc_data(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401)
        
    db = get_db()
    user_doc = db.collection("users").document(user_id).get()
    
    if not user_doc.exists:
        raise HTTPException(status_code=404)
        
    user_data = user_doc.to_dict()
    history = user_data.get("pmc_history", [])
    ftp = user_data.get("ftp", 200)
    ftp_history = user_data.get("ftp_history", [])
    
    dates = [item["date"] for item in history]
    daily_tss = [item.get("tss", 0) for item in history]
    ctl = [item["ctl"] for item in history]
    atl = [item["atl"] for item in history]
    tsb = [item["tsb"] for item in history]
    p5 = [item.get("p5") for item in history]
    p50 = [item.get("p50") for item in history]
    p95 = [item.get("p95") for item in history]
    
    return {
        "dates": dates,
        "daily_tss": daily_tss,
        "ctl": ctl,
        "atl": atl,
        "tsb": tsb,
        "p5": p5,
        "p50": p50,
        "p95": p95,
        "ftp": ftp,
        "ftp_history": ftp_history
    }
