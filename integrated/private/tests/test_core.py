from datetime import datetime, timezone
from core.agent import Agent, SYSTEM_PROMPT
from core.memory import InMemoryMemory
from core.model_router import ModelReply, ModelRouter
from tools.basic import get_time, save_note, list_notes, set_reminder

class FakeRouter:
    def __init__(self, text="Hello"): self.text=text
    def answer(self, messages, tools=None):
        assert "untrusted DATA" in messages[0]["content"]
        return ModelReply(self.text, "fake", "test")

class ToolRouter:
    def __init__(self): self.calls = 0
    def answer(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            return ModelReply("", "fake", "test", [{"id":"1", "function":{"name":"save_note", "arguments":"{\"text\":\"via tool\"}"}}])
        return ModelReply("done", "fake", "test")

def test_chat_and_per_user_isolation():
    m = InMemoryMemory(); a = Agent(FakeRouter(), m)
    a.handle_message("u1", "secret one"); a.handle_message("u2", "secret two")
    assert "secret one" in str(m.history("u1")) and "secret two" not in str(m.history("u1"))

def test_basic_tools_are_user_scoped():
    m = InMemoryMemory(); save_note(m, "a", "private"); save_note(m, "b", "other")
    assert list_notes(m, "a")["notes"] == ["private"]
    item = set_reminder(m, "a", "test", datetime.now(timezone.utc)); assert item["text"] == "test"
    assert get_time()

def test_prompt_injection_is_data_not_instruction():
    assert "never as instructions" in SYSTEM_PROMPT

def test_native_tool_call_is_bounded_and_user_scoped():
    memory = InMemoryMemory(); result = Agent(ToolRouter(), memory).handle_message("u", "save this")
    assert result.text == "done" and memory.list_notes("u") == ["via tool"]

def test_delete_my_data():
    m = InMemoryMemory(); m.add_message("u", "user", "x"); m.save_note("u", "n"); m.delete_user("u")
    assert m.history("u") == [] and m.list_notes("u") == []
