"""Adversarial tests for /ports/journal living journal.

Acceptance criteria under test (owner spec for P1 #3):
- OPT-IN: no recording unless the journal is on
- dated entries with original-message links
- editable summaries (edits never lose source links; recording never
  overwrites an owner edit)
- JSON + Markdown portability
Plus: cross-owner isolation, explicit command grammar (ambiguous phrasing
falls through), caps, off-pauses-recording, wipe. Every test counts
EFFECTS (store rows, source links), not prose replies.
"""
import sys, os, json, unittest

sys.path.insert(0, os.path.dirname(__file__))
from journal import (Journal, JournalEntry, MemJournalStore, JournalError,
                     JournalLimitReached, default_summarizer,
                     MAX_ENTRIES_PER_OWNER)

A, B = "v_owner_a", "v_owner_b"
D1, D2 = "2026-10-09", "2026-10-10"


def fresh():
    s = MemJournalStore()
    return Journal(s), s


def journal_on(j, owner=A):
    j.handle_message(owner, "journal on")


class TestOptInGate(unittest.TestCase):
    def test_no_recording_without_optin(self):
        j, s = fresh()
        out = j.record(A, D1, "hello", source_sid="c1", source_ts=1000)
        self.assertIsNone(out)
        self.assertEqual(s.count(A), 0)          # zero stored rows

    def test_recording_after_optin(self):
        j, s = fresh()
        journal_on(j)
        e = j.record(A, D1, "hello", source_sid="c1", source_ts=1000)
        self.assertIsNotNone(e)
        self.assertEqual(s.count(A), 1)

    def test_off_pauses_but_keeps_entries(self):
        j, s = fresh()
        journal_on(j)
        j.record(A, D1, "hello", source_sid="c1", source_ts=1000)
        j.handle_message(A, "journal off")
        self.assertIsNone(j.record(A, D1, "more", source_sid="c1",
                                   source_ts=2000))
        self.assertEqual(s.count(A), 1)          # kept, not wiped
        self.assertEqual(len(j.get_entry(A, D1).sources), 1)  # no accrual

    def test_wipe_deletes_everything_and_turns_off(self):
        j, s = fresh()
        journal_on(j)
        j.record(A, D1, "x", source_sid="c", source_ts=1)
        j.record(A, D2, "y", source_sid="c", source_ts=2)
        reply = j.handle_message(A, "delete my journal")
        self.assertIn("2", reply)
        self.assertEqual(s.count(A), 0)
        self.assertIsNone(j.record(A, D2, "z", source_sid="c", source_ts=3))


class TestEntriesAndSources(unittest.TestCase):
    def test_dated_entries_and_source_links(self):
        j, s = fresh()
        journal_on(j)
        j.record(A, D1, "first message", source_sid="chat-1", source_ts=1000)
        j.record(A, D1, "second message", source_sid="chat-1", source_ts=2000)
        j.record(A, D2, "next day", source_sid="chat-2", source_ts=3000)
        e1 = j.get_entry(A, D1)
        self.assertEqual(len(e1.sources), 2)
        self.assertEqual(e1.sources[0]["chat"], "chat-1")
        self.assertEqual(e1.sources[0]["ts"], 1000)
        self.assertEqual(e1.sources[0]["excerpt"], "first message")
        self.assertEqual(s.count(A), 2)          # one entry per day

    def test_default_summary_is_extractive_only(self):
        j, s = fresh()
        journal_on(j)
        j.record(A, D1, "went running", source_sid="c", source_ts=1000)
        j.record(A, D1, "felt great", source_sid="c", source_ts=2000)
        text = j.get_entry(A, D1).text
        self.assertIn("went running", text)
        self.assertIn("felt great", text)
        # every line comes from a recorded excerpt - no invention possible
        for line in text.splitlines():
            self.assertTrue(line.startswith("- "))
            self.assertIn(line[2:], ("went running", "felt great"))

    def test_voice_note_style_source(self):
        j, s = fresh()
        journal_on(j)
        j.record(A, D1, "[voice note] talked about the move",
                 source_sid="chat-9", source_ts=5000)
        e = j.get_entry(A, D1)
        self.assertEqual(e.sources[0]["chat"], "chat-9")
        self.assertIn("voice note", e.text)


class TestEditing(unittest.TestCase):
    def test_edit_keeps_sources_and_marks_edited(self):
        j, s = fresh()
        journal_on(j)
        j.record(A, D1, "raw words", source_sid="c1", source_ts=1000)
        j.edit_entry(A, D1, "My own summary of the day.")
        e = j.get_entry(A, D1)
        self.assertEqual(e.text, "My own summary of the day.")
        self.assertTrue(e.edited)
        self.assertEqual(len(e.sources), 1)      # links untouched
        self.assertEqual(e.sources[0]["excerpt"], "raw words")

    def test_recording_never_overwrites_an_edit(self):
        j, s = fresh()
        journal_on(j)
        j.record(A, D1, "raw words", source_sid="c1", source_ts=1000)
        j.edit_entry(A, D1, "My words.")
        j.record(A, D1, "evening update", source_sid="c1", source_ts=2000)
        e = j.get_entry(A, D1)
        self.assertEqual(e.text, "My words.")    # edit survives
        self.assertEqual(len(e.sources), 2)      # but links keep accruing

    def test_edit_missing_entry_refused(self):
        j, s = fresh()
        journal_on(j)
        with self.assertRaises(JournalError):
            j.edit_entry(A, D1, "nothing there")
        self.assertEqual(s.count(A), 0)


class TestExports(unittest.TestCase):
    def test_export_json_shape_and_provenance(self):
        j, s = fresh()
        journal_on(j)
        j.record(A, D1, "hello", source_sid="c1", source_ts=1000)
        data = j.export_json(A)
        self.assertEqual(data["format"], "crayon-journal-v1")
        self.assertTrue(data["journal_on"])
        e = data["entries"][0]
        self.assertEqual(e["date"], D1)
        self.assertEqual(e["sources"][0]["chat"], "c1")
        self.assertEqual(e["sources"][0]["message_ts"], 1000)
        json.dumps(data)                          # serializable

    def test_export_markdown_contains_links_and_edit_mark(self):
        j, s = fresh()
        journal_on(j)
        j.record(A, D1, "raw", source_sid="c1", source_ts=1000)
        j.edit_entry(A, D1, "Edited summary.")
        md = j.export_markdown(A)
        self.assertIn("## %s (edited)" % D1, md)
        self.assertIn("Edited summary.", md)
        self.assertIn("chat c1, message 1000", md)

    def test_export_empty_journal(self):
        j, s = fresh()
        self.assertEqual(j.export_json(A)["entries"], [])
        self.assertIn("# Crayon journal export", j.export_markdown(A))


class TestIsolation(unittest.TestCase):
    def test_cross_owner_zero_effects(self):
        j, s = fresh()
        journal_on(j, A)
        j.record(A, D1, "A's day", source_sid="cA", source_ts=1000)
        # B never opted in: record is a no-op, reads are empty
        self.assertIsNone(j.record(B, D1, "B's day", source_sid="cB",
                                     source_ts=1000))
        self.assertEqual(j.list_entries(B), [])
        self.assertIsNone(j.get_entry(B, D1))
        self.assertEqual(j.export_json(B)["entries"], [])
        # B's commands do not touch A
        j.handle_message(B, "journal on")
        j.handle_message(B, "delete my journal")
        self.assertEqual(s.count(A), 1)
        self.assertTrue(s.get_optin(A))
        # A's entry invisible to B
        self.assertNotIn("A's day", j.export_markdown(B))


class TestCommands(unittest.TestCase):
    def test_show_entry_and_status(self):
        j, s = fresh()
        journal_on(j)
        j.record(A, D1, "hello", source_sid="c1", source_ts=1000)
        reply = j.handle_message(A, "show my journal for %s" % D1)
        self.assertIn("hello", reply)
        status = j.handle_message(A, "journal status")
        self.assertIn("ON", status)
        self.assertIn("1", status)

    def test_show_missing_date(self):
        j, s = fresh()
        journal_on(j)
        self.assertIn("No journal entry",
                      j.handle_message(A, "show journal for %s" % D2))

    def test_edit_command(self):
        j, s = fresh()
        journal_on(j)
        j.record(A, D1, "raw", source_sid="c1", source_ts=1000)
        reply = j.handle_message(A, "edit journal for %s: my summary" % D1)
        self.assertIn("Updated", reply)
        self.assertEqual(j.get_entry(A, D1).text, "my summary")

    def test_export_commands(self):
        j, s = fresh()
        journal_on(j)
        j.record(A, D1, "hello", source_sid="c1", source_ts=1000)
        self.assertIn("crayon-journal-v1",
                      j.handle_message(A, "export my journal as json"))
        self.assertIn("# Crayon journal export",
                      j.handle_message(A, "export my journal as markdown"))

    def test_ambiguous_phrasing_falls_through(self):
        j, s = fresh()
        for msg in ("journal about my day", "can you journal this?",
                    "dear journal", "journaling", "show me the journal of "
                    "yesterday"):
            self.assertIsNone(j.handle_message(A, msg), msg)
        self.assertEqual(s.count(A), 0)
        self.assertFalse(s.get_optin(A))

    def test_commands_create_no_entries(self):
        j, s = fresh()
        j.handle_message(A, "journal status")
        j.handle_message(A, "export my journal as json")
        self.assertEqual(s.count(A), 0)


class TestCaps(unittest.TestCase):
    def test_entry_cap_refuses_without_evicting(self):
        j, s = fresh()
        journal_on(j)
        s.set_optin(A, True)
        # fill to the cap with distinct days
        for i in range(MAX_ENTRIES_PER_OWNER):
            day = "2025-%02d-%02d" % (1 + i // 28, 1 + i % 28)
            s.put(JournalEntry(A, day, "x"))
        self.assertEqual(s.count(A), MAX_ENTRIES_PER_OWNER)
        with self.assertRaises(JournalLimitReached):
            j.record(A, D1, "overflow", source_sid="c", source_ts=1)
        self.assertEqual(s.count(A), MAX_ENTRIES_PER_OWNER)  # nothing evicted

    def test_invalid_date_refused(self):
        j, s = fresh()
        journal_on(j)
        with self.assertRaises(JournalError):
            j.record(A, "yesterday", "x", source_sid="c", source_ts=1)
        self.assertEqual(s.count(A), 0)


if __name__ == "__main__":
    unittest.main()
