from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone

@dataclass
class InMemoryMemory:
    messages: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    notes: dict[str, list[str]] = field(default_factory=dict)
    reminders: dict[str, list[dict]] = field(default_factory=dict)
    def history(self, user_id: str, limit: int = 12): return self.messages.get(user_id, [])[-limit:]
    def add_message(self, user_id, role, content): self.messages.setdefault(user_id, []).append({"role": role, "content": content})
    def save_note(self, user_id, note): self.notes.setdefault(user_id, []).append(note)
    def list_notes(self, user_id): return list(self.notes.get(user_id, []))
    def set_reminder(self, user_id, text, at):
        item = {"text": text, "at": at.isoformat() if isinstance(at, datetime) else str(at), "created_at": datetime.now(timezone.utc).isoformat()}
        self.reminders.setdefault(user_id, []).append(item); return item
    def delete_user(self, user_id):
        self.messages.pop(user_id, None); self.notes.pop(user_id, None); self.reminders.pop(user_id, None)
