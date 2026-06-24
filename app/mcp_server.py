from mcp.server.fastmcp import FastMCP

mcp = FastMCP("it-support-triage")

@mcp.tool()
def check_system_status(service_name: str) -> str:
    """Check the status of a specific IT service.
    
    Args:
        service_name: The name of the service to check (e.g., 'database', 'auth_service', 'api_gateway').
        
    Returns:
        A string describing the current status.
    """
    service = service_name.lower().strip()
    if service in ["database", "db"]:
        return "Service 'database' is degraded. Response latency is high (1500ms)."
    elif service in ["auth_service", "auth"]:
        return "Service 'auth_service' is running normally (active, 100% healthy)."
    elif service in ["api_gateway", "gateway"]:
        return "Service 'api_gateway' is running normally."
    else:
        return f"Service '{service_name}' status is unknown."

@mcp.tool()
def reset_user_password(username: str, email: str) -> str:
    """Resets the password for a user.
    
    Args:
        username: The employee's username.
        email: The employee's company email.
        
    Returns:
        A confirmation message and temporary password.
    """
    if "@" not in email:
        return "Error: Invalid email format."
    
    import random
    import string
    temp_pass = "".join(random.choices(string.ascii_letters + string.digits, k=10))
    return f"Successfully reset password for {username} ({email}). Temporary password: {temp_pass}. User will be prompted to change it on next login."

@mcp.tool()
def restart_service(service_name: str) -> str:
    """Restarts a specified service.
    
    Args:
        service_name: The service to restart (e.g., 'database', 'auth_service', 'api_gateway').
        
    Returns:
        A message indicating result of the restart operation.
    """
    service = service_name.lower().strip()
    if service in ["database", "db", "auth_service", "auth", "api_gateway", "gateway"]:
        return f"Successfully restarted service '{service}'. Current status: running normally, latency 12ms."
    else:
        return f"Error: Cannot restart unknown service '{service_name}'."

if __name__ == "__main__":
    mcp.run(transport="stdio")
