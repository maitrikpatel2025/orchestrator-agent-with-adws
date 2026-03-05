# E2E Test Runner

Execute end-to-end (E2E) tests for the application using browser automation (Playwright MCP Server or Bash-based HTTP testing). If any errors occur and assertions fail, mark the test as failed and explain exactly what went wrong.

## Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `adw_id` | Unique workflow identifier | `$1` if provided, otherwise generate random 8 character hex string |
| `agent_name` | Agent executing the test | `$2` if provided, otherwise `test_e2e` |
| `e2e_test_file` | Path to the E2E test specification | `$3` (required) |
| `application_url` | Base URL for the frontend application | `$4` if provided, otherwise `http://localhost:3000` |

## Test File Location

E2E test specifications are located in `.claude/commands/e2e/`:
- Each test file describes a user story, test steps, and success criteria
- Test files follow a standard format with sequential verification steps

## Instructions

### Phase 1: Preparation

1. Read the `e2e_test_file` specified in the variables
2. Digest the `User Story` to understand what functionality is being validated
3. Note the `Success Criteria` to understand pass/fail conditions

### Phase 2: Setup

1. Verify the application is running at the expected URLs
2. Check backend health endpoint (e.g., `/api/health` or `/api/test`)
3. Ensure the frontend is accessible at `application_url`
4. If the application is not running, attempt to start it or report the failure

### Phase 3: Test Execution

1. **Initialize browser automation** (Playwright MCP or equivalent) in headed mode for visibility
2. Navigate to the `application_url`
3. **Execute each `Test Step`** from the test file in sequence
4. For each **Verify** step:
   - Check the assertion
   - If it fails, immediately mark the test as failed
   - Format failure as: `(Step N) [Step description] - [Error details]`
5. **Capture screenshots** as specified in the test steps
6. Allow time for async operations and element visibility (React/Vue/Svelte state updates, API calls)

### Phase 4: Screenshot Management

Save screenshots to the designated directory with descriptive names:

**Directory Structure:**
```
<codebase_root>/agents/<adw_id>/<agent_name>/img/<test_directory_name>/
```

**Naming Convention:**
```
01_<descriptive_name>.png
02_<descriptive_name>.png
```

Use `pwd` or equivalent to get the absolute path to the codebase for correct screenshot paths.

### Phase 5: Error Handling

If you encounter an error:
1. Mark the test as **failed** immediately
2. Report the exact step where the failure occurred
3. Include the specific error message
4. Example: `(Step 3) Failed to find element with selector "submit-btn" on page "http://localhost:3000/form"`

## Common Test Patterns

### Frontend Elements to Test

| Element Type | How to Find | Notes |
|-------------|-------------|-------|
| Navigation | nav links, router links | Test routing between pages |
| Forms | input fields, buttons, selects | Test user input workflows |
| Tables/Lists | table rows, list items | Test data display |
| Modals/Dialogs | overlay elements | Test interactive flows |
| API-loaded content | elements that appear after fetch | Wait for loading states |

### API Endpoints to Test

| Pattern | What to Check |
|---------|---------------|
| Health checks | Status 200, expected response body |
| CRUD endpoints | Create, read, update, delete operations |
| Authentication | Token handling, protected routes |
| Error responses | 400/404/500 error handling |

## Output Format

Return results in the following JSON format:

```json
{
  "test_name": "Test Name Here",
  "status": "passed|failed",
  "steps_completed": 5,
  "total_steps": 10,
  "screenshots": [
    "<absolute_path>/agents/<adw_id>/<agent_name>/img/<test_name>/01_<descriptive_name>.png",
    "<absolute_path>/agents/<adw_id>/<agent_name>/img/<test_name>/02_<descriptive_name>.png"
  ],
  "error": null,
  "failed_step": null
}
```

### On Failure

```json
{
  "test_name": "Test Name Here",
  "status": "failed",
  "steps_completed": 3,
  "total_steps": 10,
  "screenshots": [
    "<absolute_path>/agents/<adw_id>/<agent_name>/img/<test_name>/01_<descriptive_name>.png"
  ],
  "error": "Element 'submit-btn' not found on page",
  "failed_step": "(Step 4) Verify submit button is present and clickable"
}
```

## Best Practices

1. **Think deeply** about each test step before executing
2. **Wait for elements** to be visible before interacting (modern frameworks render asynchronously)
3. **Wait for API responses** when testing data-dependent components
4. **Use absolute paths** for all screenshot operations
5. **Create directories** if they don't exist before saving screenshots
6. **Review Success Criteria** at the end to ensure all conditions are met
7. **Capture intermediate screenshots** to document the test flow even on failure

## Integration with ADW

This test runner integrates with the AI Developer Workflow (ADW) system:
- Screenshots are stored in the ADW workspace: `agents/{adw_id}/`
- Test results can be consumed by `adw_test_iso.py` for automated testing
- Failed tests can be investigated and resolved using fix workflows
