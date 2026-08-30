from fastapi import APIRouter

from app.api.routes import (
    accounts,
    auto,
    credentials,
    exports,
    health,
    providers,
    runs,
    settings,
    team,
)

router = APIRouter()
router.include_router(health.router)
router.include_router(accounts.router)
router.include_router(runs.router)
router.include_router(team.router)
router.include_router(settings.router)
router.include_router(credentials.router)
router.include_router(exports.router)
router.include_router(providers.router)
router.include_router(auto.router)
