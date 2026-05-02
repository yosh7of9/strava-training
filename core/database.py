from google.cloud import firestore
from core.config import settings

def get_db():
    """
    Initialize and return a Firestore client.
    Uses GCP_PROJECT_ID from settings.
    """
    db = firestore.Client(project=settings.GCP_PROJECT_ID)
    return db
