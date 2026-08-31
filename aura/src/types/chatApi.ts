/**
 * The wire shape of `GET /api/chat`, in one place.
 *
 * Five components independently read this response and each one typed the rows
 * `any`: ChatThread, ContextPanel, ConversationSidebar, Sidebar and
 * ConversationHistoryScreen. That is not five small lint warnings, it is five
 * copies of an unwritten contract — when `backend/main.py::get_chat_history`
 * renames a column, nothing here fails to compile and the symptom is a blank
 * sidebar or a message with no timestamp.
 *
 * Fields mirror that handler exactly, including the parts that are less tidy than
 * one would like:
 *
 *   - `role` is `string`, not `'user' | 'assistant'`. The database column is a
 *     free-text field, so narrowing it here would be a claim this code cannot
 *     keep. Call sites that need the union should narrow explicitly.
 *   - `timestamp` is nullable because the handler emits `None` when the row
 *     predates the column.
 *   - `display_order` and `branch_from_id` come through `getattr(..., None)` and
 *     are genuinely absent on older rows.
 */
export interface ChatHistoryMessage {
  id: number;
  session_id: string | null;
  role: string;
  content: string;
  timestamp: string | null;
  pinned: boolean;
  display_order: number | null;
  branch_from_id: number | null;
}

export interface ChatHistoryResponse {
  messages?: ChatHistoryMessage[];
}

/**
 * The wire shape of the memory rows on `GET /api/memories`.
 *
 * Same reasoning as above. Note `topic` / `insight` rather than the
 * `category` / `content` the UI displays them as — the rename happens in the
 * mapping, and typing the row lets the compiler catch a mapping that drifts.
 */
export interface MemoryRow {
  id: number;
  topic: string;
  insight: string;
  importance: number;
  timestamp: string | null;
}

export interface MemoriesResponse {
  memories?: MemoryRow[];
}
