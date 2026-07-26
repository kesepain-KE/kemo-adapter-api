from api.routes.capabilities import router as capabilities_router
from api.routes.retrieval import router as retrieval_router
from api.routes.responses import router as responses_router
from api.routes.status import router as status_router

__all__ = ["capabilities_router", "responses_router", "retrieval_router", "status_router"]
