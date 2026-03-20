#!/usr/bin/env python3
"""LangChain agent wrapper around sixmo form automation tool."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_JSON = PROJECT_ROOT / "skills" / "sixmo-form-autofill" / "scripts" / "input.example.json"
DEFAULT_RUNNER_PATH = (
    PROJECT_ROOT / "skills" / "sixmo-form-autofill" / "scripts" / "run_sixmo_form.py"
)
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"


SYSTEM_PROMPT = """
Ты агент для автопрохождения формы sixmo.
Если пользователь просит пройти/заполнить форму, обязательно вызови инструмент `run_sixmo_form`.
После выполнения инструмента отвечай в формате:

Форма пройдена, идентификационный номер: <ID>
Вопросы и ответы:
- <вопрос 1>: <ответ 1>
- <вопрос 2>: <ответ 2>

Если инструмент вернул ошибку, ответь:
Форма не пройдена. Ошибка: <текст ошибки>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LangChain GPT agent for sixmo form automation.")
    parser.add_argument(
        "--message",
        default="",
        help="User message for one-shot run. If omitted, starts interactive mode.",
    )
    parser.add_argument(
        "--input-json",
        default=str(DEFAULT_INPUT_JSON),
        help="Default input JSON for sixmo tool.",
    )
    parser.add_argument(
        "--runner-path",
        default=str(DEFAULT_RUNNER_PATH),
        help="Path to sixmo runner script.",
    )
    parser.add_argument(
        "--python-bin",
        default=sys.executable,
        help="Python executable for running sixmo runner.",
    )
    parser.add_argument(
        "--model",
        default="gpt-4.1-mini",
        help="OpenAI model for LangChain ChatOpenAI.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="Model temperature.",
    )
    parser.add_argument(
        "--tool-timeout-seconds",
        type=int,
        default=360,
        help="Timeout for form tool execution.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Verbose mode for agent executor.",
    )
    parser.add_argument(
        "--env-file",
        default=str(DEFAULT_ENV_FILE),
        help="Path to .env file with OPENAI_API_KEY (optional).",
    )
    return parser.parse_args()


def load_env_file(path: str) -> None:
    env_path = Path(path).expanduser().resolve()
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        os.environ[key] = value


def parse_json_blob(text: str) -> Dict[str, Any]:
    text = text.strip()
    if not text:
        raise ValueError("Empty JSON output from tool")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError(f"Unable to parse JSON from output: {text[:300]}")
        parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected dict JSON from tool, got: {type(parsed)}")
    return parsed


class RunSixmoInput(BaseModel):
    input_json_path: Optional[str] = Field(
        default=None,
        description="Absolute path to input JSON for the sixmo runner. If omitted, default path is used.",
    )


def build_tool(
    default_input_json: str,
    runner_path: str,
    python_bin: str,
    tool_timeout_seconds: int,
) -> StructuredTool:
    default_input_path = str(Path(default_input_json).expanduser().resolve())
    runner_abs_path = str(Path(runner_path).expanduser().resolve())

    def run_sixmo_form(input_json_path: Optional[str] = None) -> str:
        selected_input = input_json_path or default_input_path
        input_path = Path(selected_input).expanduser().resolve()
        if not input_path.exists():
            return json.dumps(
                {
                    "ok": False,
                    "error": f"Input JSON not found: {input_path}",
                },
                ensure_ascii=False,
            )

        if not Path(runner_abs_path).exists():
            return json.dumps(
                {
                    "ok": False,
                    "error": f"Runner script not found: {runner_abs_path}",
                },
                ensure_ascii=False,
            )

        cmd = [
            python_bin,
            runner_abs_path,
            "--input",
            str(input_path),
            "--verbose",
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=tool_timeout_seconds,
        )

        stderr_tail = (proc.stderr or "").strip()[-8000:]
        try:
            payload = parse_json_blob(proc.stdout or "")
        except Exception as exc:  # noqa: BLE001
            return json.dumps(
                {
                    "ok": False,
                    "error": f"Failed to parse tool JSON output: {exc}",
                    "stdout": (proc.stdout or "").strip()[-2000:],
                    "stderr": stderr_tail,
                    "returncode": proc.returncode,
                },
                ensure_ascii=False,
            )

        # Keep only the fields needed by agent response.
        result = {
            "ok": bool(payload.get("ok")),
            "error": payload.get("error"),
            "flowId": payload.get("flowId"),
            "finalIdentifier": payload.get("finalIdentifier"),
            "completedAt": payload.get("completedAt"),
            "submittedAnswers": payload.get("submittedAnswers") or [],
            "toolStderr": stderr_tail,
            "returncode": proc.returncode,
        }
        return json.dumps(result, ensure_ascii=False)

    return StructuredTool.from_function(
        func=run_sixmo_form,
        name="run_sixmo_form",
        description=(
            "Запускает автопрохождение формы sixmo и возвращает JSON с finalIdentifier и списком submittedAnswers "
            "(вопросы и отправленные ответы)."
        ),
        args_schema=RunSixmoInput,
    )


def build_executor(args: argparse.Namespace) -> AgentExecutor:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    llm = ChatOpenAI(
        model=args.model,
        temperature=args.temperature,
        api_key=api_key,
    )

    tool = build_tool(
        default_input_json=args.input_json,
        runner_path=args.runner_path,
        python_bin=args.python_bin,
        tool_timeout_seconds=args.tool_timeout_seconds,
    )
    tools = [tool]

    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", SYSTEM_PROMPT),
            MessagesPlaceholder("chat_history", optional=True),
            ("human", "{input}"),
            MessagesPlaceholder("agent_scratchpad"),
        ]
    )
    agent = create_tool_calling_agent(llm, tools, prompt)
    return AgentExecutor(agent=agent, tools=tools, verbose=args.verbose, max_iterations=4)


def run_once(executor: AgentExecutor, message: str) -> str:
    response = executor.invoke({"input": message, "chat_history": []})
    return str(response.get("output", "")).strip()


def run_interactive(executor: AgentExecutor) -> None:
    chat_history: list[Any] = []
    while True:
        user_input = input("you> ").strip()
        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit", "q"}:
            break
        response = executor.invoke({"input": user_input, "chat_history": chat_history})
        output = str(response.get("output", "")).strip()
        print(f"agent> {output}")
        chat_history.append(HumanMessage(content=user_input))
        chat_history.append(AIMessage(content=output))


def main() -> int:
    args = parse_args()
    load_env_file(args.env_file)
    executor = build_executor(args)
    if args.message:
        print(run_once(executor, args.message))
    else:
        run_interactive(executor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
