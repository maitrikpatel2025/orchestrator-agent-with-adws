# Install Worktree

This command sets up an isolated worktree environment with dependency installation.

## Parameters
- Worktree path: {0}
- Backend port: {1} (optional)
- Frontend port: {2} (optional)

## Steps

1. **Navigate to worktree directory**
   ```bash
   cd {0}
   ```

2. **Create port configuration file** (if ports provided)
   Create `.ports.env` with:
   ```
   BACKEND_PORT={1}
   FRONTEND_PORT={2}
   ```

3. **Copy environment files**
   - Copy `.env` from parent repo if it exists
   - If ports are provided, append `.ports.env` contents to `.env`

4. **Install dependencies**
   - Detect the project's package manager and install dependencies
   - For Python projects: `uv sync --all-extras` or `pip install -r requirements.txt`
   - For Node projects: `npm install` or `yarn install`
   - For mixed projects: install both

5. **Copy configuration files**
   - Copy any MCP configuration files (`.mcp.json`) from parent repo if they exist
   - Update paths to use absolute worktree paths where needed

## Error Handling
- If parent .env files don't exist, create minimal versions from .env.example files if available
- Ensure all paths are absolute to avoid confusion

## Report
- List all files created/modified
- Show port assignments (if applicable)
- Confirm dependencies installed
- Note any missing configuration files that need user attention
