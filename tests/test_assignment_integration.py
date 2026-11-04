"""Exercise the gateway against the real memory service with deterministic models."""

import os
import subprocess
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[1]
ASSIGNMENT = Path("/home/neeraj/Code/mynaksh_assignment")
ASSIGNMENT_PYTHON = ASSIGNMENT / ".venv/bin/python"
GATEWAY_TOKEN = "test-gateway-token-" + "x" * 32


def test_gateway_to_assignment_chat_keeps_scoped_unicode_memories() -> None:
    """Prove real API routing, memory persistence/isolation, and Hindi preference."""
    if not ASSIGNMENT_PYTHON.is_file():
        raise AssertionError(f"Reference assignment interpreter missing: {ASSIGNMENT_PYTHON}")

    script = r"""
import asyncio
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import httpx

workspace = Path(os.environ["GATEWAY_REPO"])
assignment = Path(os.environ["ASSIGNMENT_REPO"])

# Load the gateway as an isolated package, then import the assignment's `app`
# package normally. Both services run in this subprocess without module clashes.
gateway_spec = importlib.util.spec_from_file_location(
    "gateway_app",
    workspace / "app" / "__init__.py",
    submodule_search_locations=[str(workspace / "app")],
)
gateway_package = importlib.util.module_from_spec(gateway_spec)
sys.modules["gateway_app"] = gateway_package
gateway_spec.loader.exec_module(gateway_package)
gateway_main_spec = importlib.util.spec_from_file_location(
    "gateway_app.main", workspace / "app" / "main.py"
)
gateway_main = importlib.util.module_from_spec(gateway_main_spec)
sys.modules["gateway_app.main"] = gateway_main
gateway_main_spec.loader.exec_module(gateway_main)

sys.path.insert(0, str(assignment))
from app.config import Settings
from app.main import create_app as create_assignment_app
from pydantic_ai import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel


async def exercise() -> None:
    prompts = []

    def record_reply(messages, _info):
        prompts.append(json.loads(messages[-1].parts[-1].content))
        return ModelResponse(parts=[TextPart("नमस्ते, यह नियंत्रित उत्तर है।")])

    with tempfile.TemporaryDirectory(prefix="astro-assignment-e2e-") as temp_dir:
        db_path = Path(temp_dir) / "memory.sqlite3"
        assignment_app = create_assignment_app(
            settings=Settings(
                _env_file=None,
                database_path=db_path,
                otel_exporter_otlp_endpoint=None,
            ),
            context_model=TestModel(custom_output_args={"language_scope": "unchanged"}),
            reply_model=FunctionModel(record_reply),
        )

        async with assignment_app.router.lifespan_context(assignment_app):
            gateway = gateway_main.create_app(
                "http://assignment.test/chat",
                transport=httpx.ASGITransport(app=assignment_app),
                gateway_token=os.environ["GATEWAY_TOKEN"],
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=gateway),
                base_url="http://gateway.test",
            ) as client:
                async def send(user_id, session_id, message, decision):
                    with assignment_app.state.context_agent.override(
                        model=TestModel(custom_output_args=decision)
                    ):
                        return await client.post(
                            "/api/chat",
                            headers={"Authorization": "Bearer " + os.environ["GATEWAY_TOKEN"]},
                            json={
                                "user_id": user_id,
                                "session_id": session_id,
                                "message": message,
                            },
                        )

                unicode_statement = "Mera goal agle saal शिक्षक बनना है।"
                extracted = await send(
                    "integration-user",
                    "first-session",
                    unicode_statement,
                    {
                        "language_scope": "unchanged",
                        "new_memories": [
                            {
                                "kind": "goal",
                                "key": "career_goal",
                                "value": "become a teacher next year",
                                "evidence": unicode_statement,
                            }
                        ],
                    },
                )
                assert extracted.status_code == 200, extracted.text

                switched = await send(
                    "integration-user",
                    "second-session",
                    "Aaj se Hindi mein jawab dena.",
                    {
                        "language_scope": "durable",
                        "reply_language": "Hindi",
                        "language_evidence": "Aaj se Hindi mein jawab dena",
                        "relevant_memory_ids": [1],
                    },
                )
                assert switched.status_code == 200, switched.text
                assert switched.json()["context_used"] == ["career_goal"]

                recalled = await send(
                    "integration-user",
                    "third-session",
                    "Mera goal kya tha?",
                    {"language_scope": "unchanged", "relevant_memory_ids": [1]},
                )
                assert recalled.status_code == 200, recalled.text
                assert recalled.json()["context_used"] == [
                    "language_preference",
                    "career_goal",
                ]

                other_user_message = "मेरे लक्ष्य क्या हैं?"
                isolated = await send(
                    "different-user",
                    "their-session",
                    other_user_message,
                    {"language_scope": "unchanged", "relevant_memory_ids": [1]},
                )
                assert isolated.status_code == 200, isolated.text
                assert isolated.json()["context_used"] == []

        assert len(prompts) == 4
        assert prompts[0]["current_message"] == unicode_statement
        assert prompts[2]["reply_language"] == "Hindi"
        assert prompts[2]["relevant_memories"] == [
            {
                "id": 2,
                "kind": "preference",
                "key": "language_preference",
                "value": "Hindi",
            },
            {
                "id": 1,
                "kind": "goal",
                "key": "career_goal",
                "value": "become a teacher next year",
            },
        ]
        assert prompts[3]["current_message"] == other_user_message
        assert prompts[3]["relevant_memories"] == []
        assert prompts[3]["reply_language"] is None

        with sqlite3.connect(db_path) as db:
            alice_memory = db.execute(
                "SELECT memory_type,memory_key,value FROM user_memories "
                "WHERE user_id=? ORDER BY id",
                ("integration-user",),
            ).fetchall()
            bob_memory = db.execute(
                "SELECT memory_key FROM user_memories WHERE user_id=?",
                ("different-user",),
            ).fetchall()
            stored_statement = db.execute(
                "SELECT content FROM conversation_messages WHERE user_id=? "
                "AND session_id=? AND role='user'",
                ("integration-user", "first-session"),
            ).fetchone()

        assert alice_memory == [
            ("goal", "career_goal", "become a teacher next year"),
            ("preference", "language_preference", "Hindi"),
        ]
        assert bob_memory == []
        assert stored_statement == (unicode_statement,)


asyncio.run(exercise())
"""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "GATEWAY_REPO": str(WORKSPACE),
        "ASSIGNMENT_REPO": str(ASSIGNMENT),
        "GATEWAY_TOKEN": GATEWAY_TOKEN,
    }
    result = subprocess.run(
        [str(ASSIGNMENT_PYTHON), "-c", script],
        cwd=WORKSPACE,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, (
        f"Controlled gateway-to-assignment E2E failed.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
