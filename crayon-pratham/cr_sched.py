"""Background scheduler: delivers due reminders. Runs in-process; the keep-warm ping keeps the host awake."""
import logging
import threading
import time
from datetime import timedelta

import cr_db as db
from cr_safety import redact

log = logging.getLogger("crayon.sched")
_started = False


def tick(out=None, only_user=None):
    """Deliver due reminders. Returns list of delivered ids. `out` is a sender with .send(chat_id, text)."""
    if out is None:
        import cr_telegram as tg
        out = tg.Out()
    db.q("UPDATE reminders SET status='pending' WHERE status='sending' AND claimed_at < now() - interval '3 minutes'", (), "none")
    cond, params = ("AND user_id=%s", (only_user,)) if only_user is not None else ("AND user_id>0", ())
    rows = db.q(f"""UPDATE reminders SET status='sending', claimed_at=now() WHERE id IN (
                      SELECT id FROM reminders WHERE status='pending' AND due_at <= now() {cond}
                      ORDER BY due_at LIMIT 10 FOR UPDATE SKIP LOCKED) RETURNING *""", params)
    delivered = []
    for r in rows:
        late = (db.q("SELECT now() AS n", (), "one")["n"] - r["due_at"]).total_seconds()
        late_note = f"\n(sent {int(late // 60)} min late, the free host was asleep)" if late > 300 else ""
        try:
            if r.get("kind") == "job":
                import cr_agent
                reply, _meta = cr_agent.respond(r["user_id"], r["chat_id"],
                    "[Scheduled job you set for me earlier - do it now and reply with the result only, don't create new reminders or jobs] " + r["text"], "", readonly=True)
                text = "Scheduled: " + reply + late_note
            else:
                text = "Reminder: " + r["text"] + late_note
            import cr_accounts
            if cr_accounts.telegram_destination(r['user_id']) is None:
                import cr_web_notifications as N
                N.publish(r['user_id'],text,'reminder:'+str(r['id'])+':'+r['due_at'].isoformat())
            else:out.send(r['chat_id'],text)
        except Exception as e:
            attempts = (r["attempts"] or 0) + 1
            log.warning("reminder %s send failed (%s)", r["id"], redact(str(e))[:120])
            db.q("UPDATE reminders SET status=%s, attempts=%s WHERE id=%s", ("failed" if attempts >= 5 else "pending", attempts, r["id"]), "none")
            continue
        if r["recurrence"] in ("daily", "weekly"):
            step = timedelta(days=1 if r["recurrence"] == "daily" else 7)
            db.q("UPDATE reminders SET status='pending', due_at=due_at + %s, sent_at=now(), attempts=0 WHERE id=%s", (step, r["id"]), "none")
        else:
            db.q("UPDATE reminders SET status='sent', sent_at=now() WHERE id=%s", (r["id"],), "none")
        delivered.append(r["id"])
    return delivered


def nudge(out=None):
    """At most one check-in per task per ~day for stale active tasks, during waking hours (user local time)."""
    if out is None:
        import cr_telegram as tg
        out = tg.Out()
    import cr_tools as T
    rows = db.q("""UPDATE tasks SET last_nudge=now() WHERE id IN (
                     SELECT t.id FROM tasks t WHERE t.user_id>0 AND t.user_id<1000000000000000 AND t.status='active' AND t.updated_at < now() - interval '20 hours'
                     AND (t.last_nudge IS NULL OR t.last_nudge < now() - interval '22 hours')
                     AND EXISTS (SELECT 1 FROM subtasks s WHERE s.task_id=t.id AND s.status<>'done')
                     ORDER BY t.updated_at LIMIT 5 FOR UPDATE SKIP LOCKED) RETURNING id,user_id,chat_id,title""", ())
    sent = []
    for t in rows:
        hr = T.now_local(t["user_id"]).hour
        if not (9 <= hr < 21):
            db.q("UPDATE tasks SET last_nudge=NULL WHERE id=%s", (t["id"],), "none")
            continue
        v = T._task_view(t["user_id"], t["id"])
        nxt = next((x for x in v["steps"] if x["status"] != "done"), None)
        try:
            out.send(t["chat_id"], f"Task check-in: \"{t['title']}\" is {v['done']}/{v['total']} done. Next: {nxt['title'] if nxt else 'wrap up'}. Want to continue, change it, or drop it?")
            sent.append(t["id"])
        except Exception as e:
            log.warning("nudge failed %s", redact(str(e))[:120])
            db.q("UPDATE tasks SET last_nudge=NULL WHERE id=%s", (t["id"],), "none")
    return sent


def _loop():
    while True:
        try:
            tick()
            # Opt-in check-ins replace the old unsolicited per-task nudge.
            import cr_proactive, cr_telegram
            cr_proactive.tick(cr_telegram.Out())
            import cr_wake
            cr_wake.idle_stop()
            import cr_mail_watch
            cr_mail_watch.tick(cr_telegram.Out())
        except Exception as e:
            log.warning("tick failed: %s", redact(f"{type(e).__name__}: {e}")[:200])
        time.sleep(20)


def start():
    global _started
    if _started:
        return
    _started = True
    import cr_work
    cr_work.start()
    threading.Thread(target=_loop, daemon=True, name="scheduler").start()
    log.info("scheduler started")
