# Codex UI/UX Enhancement Prompt — 2025-10-17

## Goals
- Automatically display the up-to-date list (or remaining lists) after every data-changing action such as `add_task`, `delete_task`, `mark_done`, `restore_task`, `move_entity`, `delete_list`, and `update_task`.
- Present every assistant response in a visually rich Markdown style that relies on emojis, short sections, and clear spacing.

## Presentation Template
- **Action line**: `{ACTION_ICON} {ACTION_TEXT} в {LIST_ICON} *{LIST_NAME}:*`
- **Details block**: `—` Optional list of affected tasks, each preceded by the same action emoji (e.g., `🟢` for added tasks).
- **List recap**:
  ```
  📋 *Актуальный список:*  
  1. Первая задача
  2. Вторая задача
  _— пусто —_  # when there are no tasks
  ```
- For deleted lists, show the remaining lists instead:
  ```
  🗂 *Оставшиеся списки:*  
  📋 Вторник
  📋 Четверг
  ```

## Emoji Reference
| Action | Icon | Example |
| --- | --- | --- |
| Add task | 🟢 | `🟢 Добавлены задачи в 📘 *Домашние дела:*` |
| Mark done | ✔️ | `✔️ Готово в 📘 *Домашние дела:*` |
| Restore task | ♻️ | `♻️ Восстановлено в 📘 *Домашние дела:*` |
| Delete task/list | 🗑 | `🗑 Удалено из 📘 *Домашние дела:*` |
| Move task | 🔄 | `🔄 Перемещено: *Задача* → в Четверг` |
| Create list | 📘 | `📘 Создан новый список *Вторник*` |
| Show lists | 🗂 | `🗂 *Твои списки:*` |

## Additional Notes
- Keep a single empty line between semantic blocks (action, details, recap).
- Use italics for captions (e.g., `*Актуальный список:*`) and bold for list names.
- Provide numbered tasks for clarity; display `_— пусто —_` when a list has no items.
- The assistant should maintain this style consistently across Telegram clients (desktop and mobile).
