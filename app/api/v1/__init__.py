from fastapi import APIRouter

from app.api.v1 import events, flights, kitchen

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(flights.router)
api_router.include_router(events.router)
api_router.include_router(kitchen.router)
