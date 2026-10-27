# CATO Token Efficiency & Context Preservation Rules

1. **Surgical File Reads:** When reading files larger than 300 lines, prefer passing `offset` and `limit` to `Read` to inspect only the target function or class, or use the MCP tool `cato_read_symbol`. If CATO returns an AST Skeleton (`[L<start>-L<end>]`), use those exact line numbers in your next `Read` call.
2. **Repository Map over Blind Exploration:** Use `cato_repo_map` to get a PageRank-ranked AST overview of the codebase instead of chaining multiple blind `Glob` and `Grep` calls.
3. **Avoid Duplicate Reads:** Do not re-read a file that is already in the current conversation context unless it was modified externally.
4. **Delegate Broad Searches:** When you need to search across many unknown files to locate a feature or bug, delegate to the `cato-explorer` subagent so intermediate search outputs stay out of the main context window.
5. **Concise Outputs:** Never reprint large blocks of code in your final chat response that were already written or modified via `Edit` or `Write`. Summarize changes briefly with file and line references.
