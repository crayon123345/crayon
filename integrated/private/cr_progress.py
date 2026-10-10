"""User-visible execution summaries only. Never reasoning, prompts, arguments or private results."""
from contextvars import ContextVar
callback=ContextVar('crayon_progress',default=None)
recorded=ContextVar('crayon_work_events',default=None)
def snapshot():return list(recorded.get() or [])[-16:]
LABELS={'web_search':'Searching public sources','read_url':'Reading a public page','research_web':'Checking sources','run_python':'Running a calculation','computer_browse':'Opening the public browser','computer_run':'Running a computer task','create_csv':'Building a CSV','create_bar_chart':'Drawing a chart','create_task':'Saving the requested task','list_tasks':'Reading your tasks','list_reminders':'Reading your reminders'}
def emit(label,state='running'):
    events=recorded.get()
    if events is not None:events.append({'label':label[:80],'state':state})
    fn=callback.get()
    if fn:
        try:fn({'label':label[:80],'state':state})
        except Exception:pass
