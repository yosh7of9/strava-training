import json
from fastapi import APIRouter, Request, Response, HTTPException, status
from google.cloud import pubsub_v1
from core.config import settings

router = APIRouter(prefix="/webhook", tags=["webhook"])

publisher = pubsub_v1.PublisherClient()
# Create topic path safely (assumes GCP_PROJECT_ID is set correctly)
if settings.GCP_PROJECT_ID:
    topic_path = publisher.topic_path(settings.GCP_PROJECT_ID, settings.PUBSUB_TOPIC_ID)
else:
    topic_path = None

@router.get("/strava")
async def verify_webhook(request: Request):
    """
    Endpoint for Strava webhook subscription validation.
    """
    hub_mode = request.query_params.get("hub.mode")
    hub_challenge = request.query_params.get("hub.challenge")
    hub_verify_token = request.query_params.get("hub.verify_token")

    if hub_mode == "subscribe" and hub_verify_token == settings.STRAVA_VERIFY_TOKEN:
        return {"hub.challenge": hub_challenge}
    
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid verify token")

@router.post("/strava")
async def receive_webhook(request: Request):
    """
    Receives activity updates from Strava and pushes to Pub/Sub.
    Must return 200 OK within 2 seconds.
    """
    payload = await request.json()
    
    # We only care about activity events (not athlete updates)
    if payload.get("object_type") == "activity" and topic_path:
        # Convert dict to bytes
        data_str = json.dumps(payload)
        data_bytes = data_str.encode("utf-8")
        
        # Publish to Pub/Sub
        try:
            future = publisher.publish(topic_path, data=data_bytes)
        except Exception as e:
            print(f"Error publishing to Pub/Sub: {e}")
            
    # Always return 200 OK immediately to satisfy Strava requirement
    return Response(status_code=status.HTTP_200_OK)
