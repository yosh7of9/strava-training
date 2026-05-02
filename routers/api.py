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
        
    history = user_doc.to_dict().get("pmc_history", [])
    
    dates = [item["date"] for item in history]
    ctl = [item["ctl"] for item in history]
    atl = [item["atl"] for item in history]
    tsb = [item["tsb"] for item in history]
    
    return {
        "dates": dates,
        "ctl": ctl,
        "atl": atl,
        "tsb": tsb
    }
