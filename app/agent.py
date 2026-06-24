import datetime
import json
import os
import re
import sys
from zoneinfo import ZoneInfo
from pydantic import BaseModel, Field

from google.adk.agents import LlmAgent
from google.adk.apps import App, ResumabilityConfig
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.agents.context import Context
from google.adk.workflow import Workflow, START
from google.adk.tools.mcp_tool import MCPToolset, StdioConnectionParams
from mcp import StdioServerParameters
from google.genai import types

from app.config import config

import asyncio
import copy
import logging
from google.adk.models.google_llm import Gemini

logger = logging.getLogger("RetryingGemini")

class RetryingGemini(Gemini):
    async def generate_content_async(self, llm_request, stream: bool = False):
        max_attempts = 5
        for attempt in range(max_attempts):
            try:
                req_copy = copy.deepcopy(llm_request)
                async for response in super().generate_content_async(req_copy, stream=stream):
                    yield response
                return
            except Exception as e:
                err_str = str(e).upper()
                if "503" in err_str or "UNAVAILABLE" in err_str or "HIGH DEMAND" in err_str or "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    import re
                    match = re.search(r"'RETRYDELAY':\s*'(\d+)S'", err_str)
                    if match:
                        wait_time = int(match.group(1)) + 1
                    else:
                        match2 = re.search(r"PLEASE RETRY IN (\d+(?:\.\d+)?)S", err_str)
                        if match2:
                            wait_time = int(float(match2.group(1))) + 1
                        else:
                            wait_time = 15
                    logger.warning(
                        f"Gemini API returned rate limit/unavailability. Retrying in {wait_time}s (attempt {attempt + 1}/{max_attempts}). Error: {e}"
                    )
                    await asyncio.sleep(wait_time)
                else:
                    raise
        # Final attempt
        req_copy = copy.deepcopy(llm_request)
        async for response in super().generate_content_async(req_copy, stream=stream):
            yield response

llm_model = RetryingGemini(model=config.model)


# Define output schemas for structured agent outputs
class TriageOutput(BaseModel):
    category: str = Field(description="Must be one of: 'diagnostics', 'resolution', or 'human'")
    reasoning: str = Field(description="Explanation of why this category was chosen")
    ticket_summary: str = Field(description="A concise summary of the issue")
    affected_service: str = Field(description="Name of the service if any (e.g., 'database', 'auth_service', 'api_gateway', or 'unknown')")

class DiagnosticsOutput(BaseModel):
    diagnostic_report: str = Field(description="A summary of the diagnostics run and findings")
    needs_resolution: bool = Field(description="True if an automated resolution/fix is possible and required")
    suggested_service: str = Field(description="The service name to act upon")

class ResolutionOutput(BaseModel):
    resolution_report: str = Field(description="A summary of the resolution attempts and results")
    status: str = Field(description="Must be 'success', 'needs_approval', or 'failed'")


# Initialize the Model Context Protocol (MCP) toolset
mcp_server_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "mcp_server.py"))

mcp_toolset = MCPToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command=sys.executable,
            args=[mcp_server_path]
        )
    )
)


# Helper function to extract text content from node inputs
def extract_text(node_input) -> str:
    if hasattr(node_input, "parts") and node_input.parts:
        text_parts = [part.text for part in node_input.parts if part.text]
        return " ".join(text_parts)
    elif isinstance(node_input, str):
        return node_input
    elif isinstance(node_input, dict) and "text" in node_input:
        return node_input["text"]
    return str(node_input)


# Security Checkpoint Node (Phase 4)
def security_checkpoint(ctx: Context, node_input) -> Event:
    text = extract_text(node_input)
    scrubbed = text

    # PII Scrubbing
    if config.pii_redaction_enabled:
        # Scrub Emails
        email_pattern = r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+'
        scrubbed = re.sub(email_pattern, "[EMAIL_REDACTED]", scrubbed)
        # Scrub passwords/keys/tokens
        pass_pattern = r'(?i)(password|secret|key|token|passwd)\s*[:=]\s*[^\s]+'
        scrubbed = re.sub(pass_pattern, r'\1=[REDACTED]', scrubbed)
        # Scrub IP addresses
        ip_pattern = r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b'
        scrubbed = re.sub(ip_pattern, "[IP_REDACTED]", scrubbed)

    # Prompt Injection & SQL Injection Detection
    is_injection = False
    if config.injection_detection_enabled:
        injection_keywords = [
            "ignore previous instructions",
            "system prompt",
            "forget all rules",
            "you are now a",
            "override safety",
            "drop database",
            "sql injection",
            "delete files"
        ]
        lower_text = text.lower()
        for kw in injection_keywords:
            if kw in lower_text:
                is_injection = True
                break

    # Structured Audit Log
    audit_data = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "session_id": ctx.session.id if ctx.session else "unknown",
        "raw_input_length": len(text),
        "scrubbed_input_length": len(scrubbed),
        "pii_detected": scrubbed != text,
        "injection_detected": is_injection,
    }

    if is_injection:
        audit_data["severity"] = "CRITICAL"
        audit_data["action"] = "BLOCKED"
        print(json.dumps(audit_data))
        return Event(
            output="Access Denied: Potential security threat detected in your request.",
            route="SECURITY_EVENT"
        )

    audit_data["severity"] = "INFO"
    audit_data["action"] = "ALLOWED"
    print(json.dumps(audit_data))

    return Event(
        output=scrubbed,
        route="SAFE",
        state={"user_query": scrubbed}
    )


# 1. Triage Orchestrator (Phase 2)
triage_orchestrator = LlmAgent(
    name="triage_orchestrator",
    model=llm_model,
    instruction="""You are an IT Support Triage Orchestrator. 
Your task is to analyze the user's IT support ticket and classify it.
Classify the ticket into one of the following categories:
- 'diagnostics': The user is asking for system status, error diagnostics, or checking if services are running.
- 'resolution': The user is requesting a concrete fix/action such as restarting a service or resetting a password.
- 'human': The request requires direct human support, is highly complex, or cannot be handled automatically.

Identify the affected service name if possible (e.g., 'database', 'auth_service', 'api_gateway').

Provide your response strictly conforming to the output schema.""",
    output_schema=TriageOutput,
)

def route_triage(node_input: TriageOutput) -> Event:
    category = node_input.category.lower().strip()
    if category not in ["diagnostics", "resolution", "human"]:
        category = "human"
    return Event(
        output=node_input,
        route=category,
        state={"category": category, "triage_info": node_input.model_dump()}
    )


# 2. Diagnostics Sub-agent (Phase 2 & 3)
diagnostics_agent = LlmAgent(
    name="diagnostics_agent",
    model=llm_model,
    instruction="""You are an IT Diagnostics Agent. 
You have access to MCP tools to inspect the status of IT systems and services.
Run the check_system_status tool to diagnose any service problems mentioned in the ticket.
Report your findings. Determine if the service needs a resolution (e.g. restart) based on the tool status.
If a service is degraded or offline, suggest a resolution and set needs_resolution=True.

Provide your response strictly conforming to the output schema.""",
    tools=[mcp_toolset],
    output_schema=DiagnosticsOutput,
)

def route_diagnostics(node_input: DiagnosticsOutput) -> Event:
    if node_input.needs_resolution:
        return Event(
            output=node_input,
            route="yes",
            state={"diagnostics_report": node_input.diagnostic_report, "suggested_service": node_input.suggested_service}
        )
    else:
        return Event(
            output=node_input,
            route="no",
            state={"diagnostics_report": node_input.diagnostic_report}
        )


# 3. Resolution Sub-agent (Phase 2 & 3)
resolution_agent = LlmAgent(
    name="resolution_agent",
    model=llm_model,
    instruction="""You are an IT Support Resolution Agent.
Your job is to resolve system issues and fulfill IT requests.
Use the MCP tools at your disposal, such as reset_user_password or restart_service.
Check the conversation state and previous diagnostic reports to find the service to restart or the user to reset.
IMPORTANT SECURITY RULE: 
- Any request to reset a password MUST require administrator approval. For password resets, do NOT call the reset tool yet; instead, set status='needs_approval'.
- For other service restarts, you can call the restart_service tool directly, and if successful, set status='success'.

Provide your response strictly conforming to the output schema.""",
    tools=[mcp_toolset],
    output_schema=ResolutionOutput,
)

def route_resolution(node_input: ResolutionOutput) -> Event:
    status = node_input.status.lower().strip()
    if status == "needs_approval":
        return Event(
            output=node_input,
            route="needs_approval",
            state={"resolution_report": node_input.resolution_report}
        )
    else:
        return Event(
            output=node_input,
            route="__DEFAULT__",
            state={"resolution_report": node_input.resolution_report}
        )


# Human-In-The-Loop Approval Node (Phase 2 & 4)
async def approval_node(ctx: Context, node_input: ResolutionOutput):
    if not ctx.resume_inputs or "admin_approval" not in ctx.resume_inputs:
        yield RequestInput(
            interrupt_id="admin_approval",
            message="✋ IT Administrator: A password reset request requires your approval. Do you approve? (Reply 'yes' or 'no')"
        )
        return
    
    approval = ctx.resume_inputs["admin_approval"].lower().strip()
    if approval == "yes":
        import random
        import string
        temp_pass = "".join(random.choices(string.ascii_letters + string.digits, k=10))
        msg = f"✅ Administrator approved the password reset. Temporary password generated: {temp_pass}."
        yield Event(
            output=msg,
            state={"approval_result": "approved", "temp_password": temp_pass}
        )
    else:
        msg = "❌ Administrator rejected the password reset request."
        yield Event(
            output=msg,
            state={"approval_result": "rejected"}
        )


# Final Presentation Node (Outputs beautiful Markdown for Web UI)
def final_output(ctx: Context, node_input):
    text = extract_text(node_input)
    
    category = ctx.state.get("category", "unknown")
    triage_info = ctx.state.get("triage_info", {})
    affected_service = triage_info.get("affected_service", "unknown")
    
    md_output = f"### 🛠️ IT Support Triage Report\n\n"
    md_output += f"**Category**: {category.capitalize()}\n"
    md_output += f"**Service**: {affected_service}\n\n"
    
    if "user_query" in ctx.state:
        md_output += f"**Original Request (Scrubbed)**: {ctx.state['user_query']}\n\n"
        
    md_output += f"#### Resolution Steps:\n"
    
    if "diagnostics_report" in ctx.state:
        md_output += f"- **Diagnostics**: {ctx.state['diagnostics_report']}\n"
    
    if "resolution_report" in ctx.state:
        md_output += f"- **Resolution**: {ctx.state['resolution_report']}\n"
        
    if "approval_result" in ctx.state:
        result = ctx.state["approval_result"]
        md_output += f"- **Admin Approval**: {result.capitalize()}\n"
        if result == "approved" and "temp_password" in ctx.state:
            md_output += f"  - *Generated Temp Password*: `{ctx.state['temp_password']}`\n"
            
    if category == "human":
        md_output += f"- **Action**: Ticket routed to Tier 2 Human Support.\n"
        
    if text and text not in md_output:
        md_output += f"\n**Status Update**: {text}\n"
        
    yield Event(content=types.Content(role='model', parts=[types.Part.from_text(text=md_output)]))
    yield Event(output=md_output)


# Connect Workflow Graph (Phase 2 & 4)
edges = [
    (START, security_checkpoint),
    (security_checkpoint, {"SAFE": triage_orchestrator, "SECURITY_EVENT": final_output}),
    (triage_orchestrator, route_triage),
    (route_triage, {"diagnostics": diagnostics_agent, "resolution": resolution_agent, "human": final_output}),
    (diagnostics_agent, route_diagnostics),
    (route_diagnostics, {"yes": resolution_agent, "no": final_output}),
    (resolution_agent, route_resolution),
    (route_resolution, {"needs_approval": approval_node, "__DEFAULT__": final_output}),
    (approval_node, final_output),
]

root_agent = Workflow(
    name="it_support_triage_workflow",
    edges=edges,
)

app = App(
    root_agent=root_agent,
    name="app",
    resumability_config=ResumabilityConfig(is_resumable=True),
)
