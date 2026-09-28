# TODO

Core agent loop
- ~~Additional providers: kiro-cli, claude~~ — shipped: both live-validated and registered
  - Aanlyze session results, prompt success/failure and lengths
- Recommended length limit on operator prompts; be less specific? more guidance on human-like prompting
- Persistent session for operator? Always start with fork of previous operator session
  - Goal is to burn less context re-treading the same ground each time

High level interface
- Background daemon
- Backed by sqlite
- Web interface
- Add workspace: directory containing .backseat directory
  - Add task: web user enters a new meta-prompt, creates file
- Add triggered jobs: start with cron-style (e.g. hourly), runs a prompt with a specified workspace and agent
  - Does not run if job is still running from prior cycle
  - Intended use: agent knows all workspaces, also has permission to read task tracker; triages top task, generates workspace with a task meta-prompt
  - Configured somewhere in workspace .backseat dir
- Periodically (30 minutes?) scan workspaces for incomplete jobs
  - If job is inactive for more than 30 minutes, and not human-interactive (state is "continue"?), continue it with a regular backseat-harness app
  - Note that human takeover must change its state to disable automatic activation, until the takeover app exits 0, then re-enable it
- File watching of tasks directory on all workspaces
  - Trigger same logic as workspace scan, but for a single workspace, and 30 seconds after workspace file activity ceases
  - Also the path for "add task" actually starting work
- Task takeover: invoke provider TUI from web shell?
  - Connect to the correct session
- Session search
  - Filter out operator sessions somehow (meta marker when those are started? Provider-specific CLI params to not store the session?)
  - If session is in a supported provider: button for web shell TUI
    - Ensure any running agentic operator task is cancelled
  - Non-supported provider: button to fork and continue from [default TUI provider]
