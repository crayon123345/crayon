"""Dedicated opt-in Discord process. Does not start Telegram/web/public engine."""
import os
import pathlib
import sys
import threading
import time
ROOT=pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'integrated'/'private'))
import cr_discord as D

def background():
    """Only Discord accounts, passed explicit output; no unrelated users/schedulers."""
    import cr_sched,cr_proactive,cr_work,cr_mail_watch
    while True:
        try:
            rows=D.db.q('SELECT d.user_id,d.subject,d.dm_channel FROM discord_accounts d JOIN users u ON u.user_id=d.user_id')
            for row in rows:
                if row['subject'] not in D.allowed_users():continue
                uid=row['user_id'];out=D.Out(uid,row['dm_channel'])
                cr_sched.tick(out,only_user=uid)
                cr_proactive.tick(out,only_user=uid)
                cr_work.tick(out,only_user=uid)
                cr_mail_watch.tick_user(out,uid)
        except Exception:
            D.log.warning('Discord background delivery unavailable; no completion claimed')
        time.sleep(30)

def main():
    # Dedicated DB namespace required until live duplicate-consumer/routing tests pass.
    # Never point this prototype at the production DB just to reuse its credentials.
    if os.getenv('CRAYON_DISCORD_PILOT_DATABASE')!='confirmed-isolated':
        raise SystemExit('Use a separate free pilot database; production cutover not accepted')
    import main as health
    health.setup_logging()
    if not D.configured():raise SystemExit('Discord is not configured; no provider started')
    # Keep one database-session lock for the lifetime of this dedicated consumer.
    D.db.init()
    lock=D.db.q('SELECT pg_try_advisory_lock(817420260017) AS acquired',fetch='one')
    if not lock or not lock['acquired']:
        raise SystemExit('Another Discord pilot consumer already owns this database')
    import cr_google,cr_connections,cr_web_app,cr_computer,cr_work
    cr_google.init();cr_connections.init();cr_web_app.init();cr_computer.init();cr_work.init();D.init()
    health.start_server()
    threading.Thread(target=background,daemon=True,name='discord-background').start()
    D.start()

if __name__=='__main__':main()
