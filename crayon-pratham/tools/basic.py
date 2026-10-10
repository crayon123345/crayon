from datetime import datetime, timezone

def get_time(): return datetime.now(timezone.utc).isoformat()

def save_note(memory, user_id, text): memory.save_note(user_id, text); return {"ok": True, "message": "Saved."}

def list_notes(memory, user_id): return {"notes": memory.list_notes(user_id)}

def set_reminder(memory, user_id, text, at): return memory.set_reminder(user_id, text, at)

TOOLS = [
 {"type":"function", "function":{"name":"get_time", "description":"Get current UTC time", "parameters":{"type":"object","properties":{},"additionalProperties":False}}},
 {"type":"function", "function":{"name":"save_note", "description":"Save a short note for this user", "parameters":{"type":"object","properties":{"text":{"type":"string"}},"required":["text"]}}},
 {"type":"function", "function":{"name":"list_notes", "description":"List notes belonging to this user", "parameters":{"type":"object","properties":{},"additionalProperties":False}}},
 {"type":"function", "function":{"name":"set_reminder", "description":"Create a reminder; never contacts third parties", "parameters":{"type":"object","properties":{"text":{"type":"string"},"at":{"type":"string"}},"required":["text","at"]}}},
]
