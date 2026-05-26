from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from agents.data_retrieval_portfolio.pipeline import PortfolioDomainAgent


router = APIRouter(tags=["portfolio-chat"])
portfolio_chat_agent = PortfolioDomainAgent()


@router.get("/chat/stream")
async def portfolio_chat_stream(question: str, session_id: str | None = None):
    return StreamingResponse(
        portfolio_chat_agent.execute_stream(question, session_id=session_id),
        media_type="text/event-stream",
    )
