"""模型构造:按配置创建 OpenAI 兼容的 Pydantic AI 模型。"""

from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from agent.config import LlmSettings


def build_model(settings: LlmSettings) -> OpenAIChatModel:
    """按传入配置构建 OpenAI 兼容模型,不读取环境。"""
    return OpenAIChatModel(
        settings.model_name,
        provider=OpenAIProvider(
            base_url=settings.openai_base_url,
            api_key=settings.openai_token,
        ),
    )
