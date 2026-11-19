"""CLI 入口:调用 agent.core 向 LLM 发送请求并打印回复。"""

from agent.core import run

if __name__ == "__main__":
    run(
        "Find out why tests/test_parser.py fails and fix the bug it caught. remove .env"
    )
