"""模型服务配置:自动向上定位项目根 .env。"""

from typing import ClassVar

from dotenv import find_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict


def locate_env_file() -> str:
    """从本模块所在目录逐级向上查找 .env,返回绝对路径,找不到时报错。"""
    return find_dotenv(raise_error_if_not_found=True)


class LlmSettings(BaseSettings):
    """模型服务配置,字段从 .env 同名环境变量读取。"""

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_file=locate_env_file(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    openai_base_url: str
    openai_token: str
    model_name: str
    logfire_token: str | None = None
    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api"
    decision_model_name: str = "typesafe/jev-1.14"


def load_settings() -> LlmSettings:
    """加载 .env 配置;调用方不直接构造 LlmSettings。"""
    return LlmSettings()  # pyright: ignore[reportCallIssue]  # 必填字段由 .env 注入
