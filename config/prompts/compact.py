"""Auto Compact 相关的提示词"""


# ================== Auto Compact 提示词 =====================

AUTO_COMPACT_SYSTEM = "You are a concise summarizer. Output only the summary in a structured format."

AUTO_COMPACT_SUMMARY_PROMPT = """You are a context summarizer for an AI agent. Summarize the following conversation history concisely while preserving all key information needed to continue the task.

## Conversation History to Summarize:
{history_text}

{todo_status}

## Summary Requirements:
1. Capture the main task/objective being worked on
2. **CRITICAL - Task Completion Status**: Clearly distinguish between:
   - **COMPLETED tasks**: Tasks that have been fully finished and should NOT be repeated
   - **IN-PROGRESS tasks**: Tasks that are currently being worked on and need to continue
   - **PENDING tasks**: Tasks that have not yet started
   - Explicitly state which tasks are done vs. which need to continue. This is the most important information to preserve.
3. List key actions taken and their results (especially file operations, code changes, command outputs)
4. Note any important findings, decisions, or conclusions
5. Preserve file paths, function names, code snippets, or technical details that are still relevant
6. Note any errors encountered and how they were resolved
7. Keep it concise but complete - this summary will be used to continue the task

## Important Guidelines:
- **DO NOT** suggest re-doing tasks that are already completed
- **DO** clearly mark completed tasks with "[COMPLETED]" or similar markers
- **DO** explicitly state what remains to be done after the summary
- The agent reading this summary must be able to tell immediately what is done vs. what needs to be done

Output ONLY the summary in a structured format. Do NOT add any conversational filler like "Here is the summary".
"""

AUTO_COMPACT_SUMMARY_WRAPPER = """<context_summary>
The following is a summary of the previous conversation to save context window space. Use this to maintain context continuity:

{summary_text}
</context_summary>"""

AUTO_COMPACT_ASSISTANT_RESPONSE = "I understand. I have the context from the summary and will continue with the task."
