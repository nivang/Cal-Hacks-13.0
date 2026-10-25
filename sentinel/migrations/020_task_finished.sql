-- When the agent sent its final answer for a task (mandates.finish): the Live page then shows the task as done.
-- Set by the texting agent when it texts its answer, by the MCP tool finish_task, and by demo/agent.py. Display only.

alter table mandates add column if not exists finished_at timestamptz;
