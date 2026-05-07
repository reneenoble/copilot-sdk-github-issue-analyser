"""
stream_api.py — GitHub Issue Complexity Analyser

Built step-by-step during the livestream. Frontend is pre-built in src/static/.

Usage:
  python stream_api.py hello                          # Phase 2a: Simplest SDK call
  python stream_api.py hello-stream                   # Phase 2b: Streaming events
  python stream_api.py <github_issue_url>             # Phase 4: CLI analysis
  python stream_api.py <owner> <repo> <issue_number>  # Phase 4: CLI analysis
  python stream_api.py serve                          # Phase 5: Start web UI
  python stream_api.py post <github_issue_url>        # Phase 6a: Analyse + post comment & labels
"""

# =================================================================
# PHASE 1 — Imports
# =================================================================

import asyncio
import base64
import json
import os
import sys
from pathlib import Path

from pydantic import BaseModel, Field
from copilot import CopilotClient, define_tool
from copilot.session import PermissionHandler


# =================================================================
# PHASE 2a — Hello World with send_and_wait (simplest possible)
#
# Three concepts: Client → Session → Response
# send_and_wait() blocks until the full response is ready.
# =================================================================

async def hello_world():
    """Simplest example: send a prompt, get the full response back."""
    client = CopilotClient()
    await client.start()

    session = await client.create_session(
        model="gpt-4.1",
        on_permission_request=PermissionHandler.approve_all,
    )
    response = await session.send_and_wait("What is the GitHub Copilot SDK in 2 sentences?")
    print(response.data.content)

    await session.disconnect()
    await client.stop()


# =================================================================
# PHASE 2b — Streaming with Events
#
# Same thing, but now we see tokens arrive in real-time.
# This is the pattern we'll use for the rest of the stream.
# Event types: assistant.message, tool.call, session.idle
# =================================================================

async def hello_world_streaming():
    """Stream the response token by token using events."""
    client = CopilotClient()
    await client.start()

    session = await client.create_session(
        model="gpt-4.1",
        on_permission_request=PermissionHandler.approve_all,
    )

    done = asyncio.Event()

    def on_event(event):
        if event.type.value == "assistant.message":
            print(event.data.content, end="", flush=True)
        elif event.type.value == "session.idle":
            done.set()

    session.on(on_event)
    await session.send("What is the GitHub Copilot SDK in 2 sentences?")
    await done.wait()

    print()
    await session.disconnect()
    await client.stop()


# =================================================================
# PHASE 3 — Custom Tools with @define_tool
#
# Tools let the agent interact with the outside world.
# You define a function + Pydantic params, and the agent decides
# when and how to call it. That's the "agentic" part.
# =================================================================

def github_api(endpoint: str) -> dict:
    """Call the GitHub REST API (shared helper for all tools)."""
    import httpx

    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "copilot-livestream",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    with httpx.Client() as http:
        resp = http.get(f"https://api.github.com{endpoint}", headers=headers)
        resp.raise_for_status()
        return resp.json()


# --- Tool 1: Fetch issue details ---

class GetIssueParams(BaseModel):
    owner: str = Field(description="Repository owner (e.g. 'microsoft')")
    repo: str = Field(description="Repository name (e.g. 'vscode')")
    issue_number: int = Field(description="Issue number")


@define_tool(description="Fetch a GitHub issue including title, body, labels, and comments")
async def get_github_issue(params: GetIssueParams) -> str:
    try:
        issue = github_api(
            f"/repos/{params.owner}/{params.repo}/issues/{params.issue_number}"
        )
        comments = github_api(
            f"/repos/{params.owner}/{params.repo}/issues/{params.issue_number}/comments"
        )
        return str({
            "title": issue["title"],
            "body": issue.get("body", "No description"),
            "labels": [l["name"] for l in issue.get("labels", [])],
            "user": issue["user"]["login"],
            "comments": [
                {"user": c["user"]["login"], "body": c["body"][:500]}
                for c in comments[:5]
            ],
        })
    except Exception as e:
        return f"Error fetching issue: {e}"


# --- Tool 2: Explore repo structure ---

class RepoStructureParams(BaseModel):
    owner: str = Field(description="Repository owner")
    repo: str = Field(description="Repository name")
    path: str = Field(default="", description="Directory path (empty for root)")


@define_tool(description="List the directory contents of a GitHub repository")
async def get_repo_structure(params: RepoStructureParams) -> str:
    try:
        items = github_api(
            f"/repos/{params.owner}/{params.repo}/contents/{params.path}"
        )
        if isinstance(items, list):
            return "\n".join(
                f"{'📁' if i['type'] == 'dir' else '📄'} {i['path']}"
                for i in items[:50]
            )
        return f"File: {items['path']}"
    except Exception as e:
        return f"Error: {e}"


# --- Tool 3: Search code ---

class SearchCodeParams(BaseModel):
    owner: str = Field(description="Repository owner")
    repo: str = Field(description="Repository name")
    query: str = Field(description="Search keywords")


@define_tool(description="Search for code in a GitHub repository")
async def search_code_in_repo(params: SearchCodeParams) -> str:
    try:
        results = github_api(
            f"/search/code?q={params.query}+repo:{params.owner}/{params.repo}&per_page=10"
        )
        files = [
            {"path": i["path"], "name": i["name"]}
            for i in results.get("items", [])[:10]
        ]
        return str(files) if files else "No matching code found"
    except Exception as e:
        return f"Error: {e}"


# --- Tool 4: Read file contents ---

class FileContentParams(BaseModel):
    owner: str = Field(description="Repository owner")
    repo: str = Field(description="Repository name")
    path: str = Field(description="File path within the repository")


@define_tool(description="Fetch and read a specific file from a GitHub repository")
async def get_file_content(params: FileContentParams) -> str:
    try:
        data = github_api(
            f"/repos/{params.owner}/{params.repo}/contents/{params.path}"
        )
        if data.get("encoding") == "base64":
            text = base64.b64decode(data["content"]).decode("utf-8")
            if len(text) > 5000:
                return text[:5000] + "\n...[truncated]"
            return text
        return data.get("content", "Unable to decode")
    except Exception as e:
        return f"Error: {e}"


# =================================================================
# PHASE 4 — System Prompt + CLI Analyser
#
# The system prompt shapes the agent's behaviour. The tools list
# tells the SDK what the agent CAN do. The agent autonomously
# decides WHICH tools to call and in what order.
# =================================================================

TOOLS = [get_github_issue, get_repo_structure, search_code_in_repo, get_file_content]

SYSTEM_PROMPT = """You are a senior engineering manager triaging GitHub issues.

When given an issue to analyse, you will:
1. Fetch the issue details using the get_github_issue tool
2. Explore the repository structure to understand the codebase
3. Search for and read relevant source files
4. Provide a structured complexity assessment

Format your response as:
## Issue Summary
## Complexity Assessment
- **Recommended Skill Level**: Junior / Mid-level / Senior / Senior+
- **Confidence**: High / Medium / Low
## Reasoning
## Files Likely Involved
## Suggested Approach
## Mentorship Notes
Include what a less experienced developer would need to learn to tackle this issue."""


async def analyse_cli(owner: str, repo: str, issue_number: int):
    """Run analysis in the terminal with streaming output."""
    print(f"\n🔍 Analysing issue #{issue_number} in {owner}/{repo}...\n")

    client = CopilotClient()
    await client.start()

    session = await client.create_session(
        model="gpt-4.1",
        tools=TOOLS,
        system_message={"mode": "append", "content": SYSTEM_PROMPT},
        on_permission_request=PermissionHandler.approve_all,
    )

    done = asyncio.Event()

    def on_event(event):
        name = event.type.value if hasattr(event.type, "value") else str(event.type)
        if name == "assistant.message":
            print(event.data.content, end="", flush=True)
        elif name in ("tool.call", "tool.execution_start"):
            tool = getattr(event.data, "name", None) or getattr(event.data, "tool_name", "")
            print(f"\n🔧 {tool}...", flush=True)
        elif name == "session.idle":
            done.set()

    session.on(on_event)
    await session.send(
        f"Please analyse GitHub issue #{issue_number} in {owner}/{repo}."
    )
    await done.wait()

    print("\n")
    await session.disconnect()
    await client.stop()


# =================================================================
# PHASE 5 — FastAPI + Server-Sent Events
#
# SSE lets us stream the agent's thinking to the browser in
# real-time. Each tool call and message chunk becomes an event
# the pre-built frontend renders as chat bubbles.
# =================================================================

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse

app = FastAPI(title="GitHub Issue Complexity Analyser")

# Serve the pre-built frontend
static_dir = Path(__file__).parent / "src" / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def root():
    return FileResponse(static_dir / "index.html")


@app.get("/health")
async def health():
    return {"status": "healthy"}


def _parse_args(raw):
    """Parse tool arguments from the various formats the SDK may return."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    if hasattr(raw, "model_dump"):
        return raw.model_dump()
    return {}


async def stream_analysis(owner: str, repo: str, issue_number: int):
    """Async generator that yields Server-Sent Events for the frontend."""
    client = CopilotClient()
    await client.start()

    session = await client.create_session(
        model="gpt-4.1",
        tools=TOOLS,
        system_message={"mode": "append", "content": SYSTEM_PROMPT},
        on_permission_request=PermissionHandler.approve_all,
    )

    queue = asyncio.Queue()
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "premium_requests": 0,
        "tool_calls": 0,
        "model": None,
    }

    def on_event(event):
        name = event.type.value if hasattr(event.type, "value") else str(event.type)

        if name == "assistant.message":
            content = getattr(event.data, "content", "")
            if content and content.strip():
                queue.put_nowait(("message", content))

        elif name == "tool.execution_start":
            tool_name = getattr(event.data, "tool_name", None) or getattr(event.data, "name", None)
            args = _parse_args(getattr(event.data, "arguments", None))
            if tool_name:
                usage["tool_calls"] += 1
                queue.put_nowait(("tool_call", {"name": tool_name, "args": args}))

        elif name == "assistant.usage":
            data = event.data
            input_tokens = getattr(data, "input_tokens", 0) or 0
            output_tokens = getattr(data, "output_tokens", 0) or 0
            model = getattr(data, "model", None)

            usage["input_tokens"] += input_tokens
            usage["output_tokens"] += output_tokens
            usage["premium_requests"] += 1
            if model:
                usage["model"] = model

            queue.put_nowait((
                "usage",
                {
                    "input_tokens": usage["input_tokens"],
                    "output_tokens": usage["output_tokens"],
                    "model": usage["model"],
                },
            ))
            queue.put_nowait((
                "premium_request",
                {"premium_requests": usage["premium_requests"]},
            ))

        elif name == "session.idle":
            queue.put_nowait(("done", None))

    session.on(on_event)
    await session.send(
        f"Please analyse GitHub issue #{issue_number} in {owner}/{repo}."
    )

    while True:
        event_type, data = await queue.get()
        if event_type == "message":
            yield f"event: message\ndata: {json.dumps({'content': data})}\n\n"
        elif event_type == "tool_call":
            yield f"event: tool_call\ndata: {json.dumps(data)}\n\n"
        elif event_type == "usage":
            yield f"event: usage\ndata: {json.dumps(data)}\n\n"
        elif event_type == "premium_request":
            yield f"event: premium_request\ndata: {json.dumps(data)}\n\n"
        elif event_type == "done":
            yield (
                f"event: done\ndata: "
                f"{json.dumps({'status': 'complete', **usage})}\n\n"
            )
            break

    await session.disconnect()
    await client.stop()


@app.get("/analyse/stream")
async def analyse_stream(owner: str, repo: str, issue_number: int):
    """Stream analysis results to the frontend via SSE."""
    return StreamingResponse(
        stream_analysis(owner, repo, issue_number),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


# =================================================================
# PHASE 6a — Write Back to GitHub (human-triggered via UI button)
#
# After the analysis streams in, the user reviews it and clicks
# "Post to GitHub". This endpoint posts the comment and adds
# a difficulty label. Human-in-the-loop = safer.
#
# Requires GITHUB_TOKEN with write permissions:
#   - Classic tokens: repo scope
#   - Fine-grained tokens: Issues → Read and Write
# =================================================================

SKILL_LABELS = {
    "junior": ["good first issue", "difficulty: junior"],
    "mid-level": ["difficulty: mid-level"],
    "senior": ["difficulty: senior"],
    "senior+": ["difficulty: senior+"],
}


async def post_comment(owner: str, repo: str, issue_number: int, body: str):
    """Post a comment on a GitHub issue."""
    import httpx

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    async with httpx.AsyncClient() as http:
        resp = await http.post(
            f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/comments",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            json={"body": body},
        )
        resp.raise_for_status()
    print(f"💬 Comment posted to {owner}/{repo}#{issue_number}")


async def add_labels(owner: str, repo: str, issue_number: int, labels: list[str]):
    """Add labels to a GitHub issue."""
    import httpx

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    async with httpx.AsyncClient() as http:
        resp = await http.post(
            f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/labels",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            json={"labels": labels},
        )
        resp.raise_for_status()
    print(f"🏷️  Labels added: {', '.join(labels)}")


class PostAnalysisRequest(BaseModel):
    owner: str
    repo: str
    issue_number: int
    body: str


@app.post("/post-analysis")
async def post_analysis(req: PostAnalysisRequest):
    """Human-triggered: post the analysis as a comment and add a difficulty label."""
    await post_comment(req.owner, req.repo, req.issue_number, req.body)

    # Try to detect skill level and add a label
    analysis_lower = req.body.lower()
    for level, labels in SKILL_LABELS.items():
        if level in analysis_lower:
            await add_labels(req.owner, req.repo, req.issue_number, labels)
            break

    return {"status": "posted"}


async def analyse_and_post(owner: str, repo: str, issue_number: int):
    """Run analysis, post the result as a comment, and add a difficulty label."""
    client = CopilotClient()
    await client.start()

    session = await client.create_session(
        model="gpt-4.1",
        tools=TOOLS,
        system_message={"mode": "append", "content": SYSTEM_PROMPT},
        on_permission_request=PermissionHandler.approve_all,
    )

    done = asyncio.Event()
    response_parts = []

    def on_event(event):
        name = event.type.value if hasattr(event.type, "value") else str(event.type)
        if name == "assistant.message":
            content = event.data.content
            print(content, end="", flush=True)
            response_parts.append(content)
        elif name in ("tool.call", "tool.execution_start"):
            tool = getattr(event.data, "name", None) or getattr(event.data, "tool_name", "")
            print(f"\n🔧 {tool}...", flush=True)
        elif name == "session.idle":
            done.set()

    session.on(on_event)
    await session.send(
        f"Please analyse GitHub issue #{issue_number} in {owner}/{repo}."
    )
    await done.wait()
    await session.disconnect()
    await client.stop()

    analysis = "".join(response_parts)
    print("\n")

    # Post comment
    await post_comment(owner, repo, issue_number, analysis)

    # Try to detect skill level and add a label
    analysis_lower = analysis.lower()
    for level, labels in SKILL_LABELS.items():
        if level in analysis_lower:
            await add_labels(owner, repo, issue_number, labels)
            break


# =================================================================
# PHASE 6b — Safety: Pre-tool Validation Hook (talk through only)
#
# The SDK lets you intercept tool calls BEFORE they execute.
# This is your last line of defense against prompt injection
# or unexpected agent behaviour.
#
# To enable: add "hooks": {"on_pre_tool_use": validate_tool_args}
# to the create_session() calls above.
# =================================================================

# async def validate_tool_args(event):
#     """Block dangerous tool arguments before execution."""
#     if event.data.tool_name == "get_file_content":
#         path = event.data.arguments.get("path", "")
#         if ".." in path or path.startswith("/") or path.startswith("~"):
#             print(f"  🛑 BLOCKED: unsafe path — {path}")
#             return {"decision": "reject", "message": "Blocked: unsafe path"}
#         sensitive = [".env", ".git/", "secrets", "credentials", "token"]
#         if any(s in path.lower() for s in sensitive):
#             print(f"  🛑 BLOCKED: sensitive file — {path}")
#             return {"decision": "reject", "message": "Blocked: sensitive file"}
#     return {"decision": "allow"}


# =================================================================
# CLI Entry Point
# =================================================================

def parse_github_url(url: str) -> tuple[str, str, int]:
    """Parse 'https://github.com/owner/repo/issues/123' into parts."""
    parts = url.rstrip("/").replace("https://github.com/", "").split("/")
    if len(parts) >= 4 and parts[2] == "issues":
        return parts[0], parts[1], int(parts[3])
    raise ValueError(f"Invalid GitHub issue URL: {url}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("🐛 GitHub Issue Complexity Analyser — Livestream Build\n")
        print("  python stream_api.py hello                          # Test the SDK (send_and_wait)")
        print("  python stream_api.py hello-stream                   # Test with streaming events")
        print("  python stream_api.py <github_issue_url>             # CLI analysis")
        print("  python stream_api.py <owner> <repo> <issue_number>  # CLI analysis")
        print("  python stream_api.py serve                          # Web UI")
        print("  python stream_api.py post <github_issue_url>        # Analyse + post to issue")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "hello":
        asyncio.run(hello_world())
    elif cmd == "hello-stream":
        asyncio.run(hello_world_streaming())
    elif cmd == "serve":
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=8000)
    elif cmd == "post" and len(sys.argv) == 3:
        # Analyse and post back to the issue
        owner, repo, num = parse_github_url(sys.argv[2])
        asyncio.run(analyse_and_post(owner, repo, num))
    elif cmd.startswith("https://"):
        owner, repo, num = parse_github_url(cmd)
        asyncio.run(analyse_cli(owner, repo, num))
    elif len(sys.argv) == 4:
        asyncio.run(analyse_cli(sys.argv[1], sys.argv[2], int(sys.argv[3])))
    else:
        print("Error: Invalid arguments. Run without args for usage.")
        sys.exit(1)
