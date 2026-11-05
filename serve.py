"""Optional minimal project-session API. Set R2D2_API_KEY locally; does not start providers itself."""
import os, sys, hmac
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'telegram-bots'))
from fastapi import FastAPI
from agent_project_routes import build_agent_project_router
app=FastAPI()
def auth(request):
    key=os.environ.get('R2D2_API_KEY','')
    return bool(key) and hmac.compare_digest(request.headers.get('x-api-key',''),key)
app.include_router(build_agent_project_router(auth))
