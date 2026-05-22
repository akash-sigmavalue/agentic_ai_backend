from langchain_openai import ChatOpenAI
from langchain_google_genai import ChatGoogleGenerativeAI
from core.config import settings


def get_llm(temperature: float = 0.0):
    """
    Returns the LLM configured for the application.
    To switch between OpenAI and Gemini, comment/uncomment the respective blocks.
    """

    # ==========================================
    # USING OPENAI
    # ==========================================
    return ChatOpenAI(
        model=settings.OPENAI_MODEL,
        temperature=temperature,
        api_key=settings.OPENAI_API_KEY,
    )

    # ==========================================
    # USING GEMINI (DEFAULT)
    # ==========================================
    # return ChatGoogleGenerativeAI(
    #     model="gemini-2.5-flash",
    #     temperature=temperature,
    #     google_api_key=settings.GEMINI_API_KEY,
    #     convert_system_message_to_human=True
    # )
