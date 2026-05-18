from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    STRAVA_CLIENT_ID: str = ""
    STRAVA_CLIENT_SECRET: str = ""
    STRAVA_REDIRECT_URI: str = "http://localhost:8000/auth/strava/callback"
    STRAVA_VERIFY_TOKEN: str = "strava_dashboard_webhook_token"
    GCP_PROJECT_ID: str = ""
    PUBSUB_TOPIC_ID: str = "strava-activity-events"
    SECRET_KEY: str = "supersecretkey"
    GEMINI_API_KEY: str = ""

    class Config:
        env_file = ".env"

settings = Settings()
