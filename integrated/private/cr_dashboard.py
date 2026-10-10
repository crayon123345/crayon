"""Deterministic private task dashboard and explicit step controls."""
import re
import cr_tools as T
import cr_db as db

def render(uid):
    tasks=T.list_tasks({'uid':uid})['tasks'];reminders=T.list_reminders({'uid':uid})['reminders']
    lines=['YOUR TASKS']
    if not tasks:lines+=['No active tracked tasks.']
    for t in tasks[:8]:
        blocked=[s for s in t['steps'] if s['status']=='blocked'];nxt=next((s for s in t['steps'] if s['status'] in ('todo','doing')),None)
        lines+=['',f"#{t['id']} {t['title']}",f"Progress: {t['done']}/{t['total']}"]
        if blocked:lines+=['Blocked: '+blocked[0]['title']]
        if nxt:lines+=['Next '+str(nxt['n'])+': '+nxt['title']]
    if reminders:
        lines+=['','UPCOMING REMINDERS']
        for r in reminders[:5]:lines+=[r['due_local']+' - '+r['text']]
    import cr_work
    cr_work.init();rows=db.q("SELECT * FROM work_jobs WHERE user_id=%s AND status IN ('queued','running','pausing','paused','blocked') ORDER BY created_at LIMIT 5",(uid,))
    if rows:
        lines+=['','INTERNAL WORK QUEUE']
        lines+=[f"#{r['id']} {r['status']}: {r['title']} ({len(r['results'])}/{len(r['steps'])})" for r in rows]
    lines+=['','Tell me a goal to plan or work on. Ask to export your tasks or research a topic in the background.']
    return '\n'.join(lines)

def natural_control(uid,text):
    m=re.fullmatch(r'(?i)(?:show|open)(?: my)? task (.+)',text.strip())
    status=re.fullmatch(r'(?i)mark (?:the )?(.*?) (?:step|task step) (?:in|for|of) (.+?) (?:as )?(done|doing|blocked|todo)(?:[,:] (.+))?',text.strip())
    if not m and not status:return None
    tasks=T.list_tasks({'uid':uid})['tasks']
    name=m[1] if m else status[2]
    choices=[t for t in tasks if name.lower() in t['title'].lower()]
    if len(choices)!=1:raise ValueError('Which task? '+('; '.join(t['title'] for t in choices[:6]) or 'No matching task found. Ask to show your tasks.'))
    task=choices[0]
    if m:return '/tasks show '+str(task['id'])
    steps=task['steps'];needle=status[1].lower()
    matches=[s for s in steps if needle in s['title'].lower()]
    if needle=='next':matches=[s for s in steps if s['status'] in ('todo','doing')][:1]
    if len(matches)!=1:raise ValueError('Which step? '+('; '.join(s['title'] for s in steps[:8])))
    note=status[4] or ('You reported this step '+status[3].lower()+'.')
    return '/tasks '+status[3].lower()+' '+str(task['id'])+' '+str(matches[0]['n'])+' | '+note

def handle(uid,chat,text,out):
    try:
        text=natural_control(uid,text) or text
    except ValueError as e:
        out.send(chat,str(e));return True
    if text.strip().lower() in ('my tasks','task dashboard','what should i work on next?','what should i work on next','list my pending tasks'):text='/tasks'
    if not (text=='/tasks' or text.startswith('/tasks ')):return False
    try:
        parts=text.split(None,3)
        if len(parts)==1:out.send(chat,render(uid))
        elif parts[1]=='add':
            T.mem.touch_user(uid)
            fields=[x.strip() for x in text[len('/tasks add '):].split(' | ')]
            if not 2<=len(fields)<=9 or any(not x for x in fields):raise ValueError('Tell me the task title and the steps you want to track.')
            r=T.create_task({'uid':uid,'chat_id':chat},fields[0],fields[1:])
            if not r['verified']:raise ValueError(r.get('error','Task not confirmed'))
            out.send(chat,'Task saved and checked.\n\n'+render(uid))
        elif parts[1]=='export':
            tasks=T.list_tasks({'uid':uid})['tasks']
            if not tasks:raise ValueError('No active tasks to export.')
            import cr_artifacts
            records=[]
            for t in tasks:
                for step in t['steps']:records.append([str(t['id']),t['title'],str(step['n']),step['title'],step['status'],step['result']])
            if len(records)>100:raise ValueError('Too many task rows for this bounded export.')
            out.artifact(chat,{'filename':'crayon-tasks.csv','mime':'text/csv','data':cr_artifacts.csv_bytes(['task_id','task','step','title','status','reported_result'],records)})
            chart=cr_artifacts.chart_bytes('Your recorded task progress',[t['title'][:22] for t in tasks[:12]],[100*t['done']/max(1,t['total']) for t in tasks[:12]],'% complete')
            out.artifact(chat,{'filename':'crayon-task-progress.png','mime':'image/png','data':chart})
            out.send(chat,'Task CSV and recorded-progress chart prepared. Step completion reflects recorded results, not independent proof.')
        elif parts[1]=='show':
            r=T.list_tasks({'uid':uid},int(parts[2]))
            if not r['verified']:raise ValueError('No such task in your account')
            t=r['task'];lines=[f"TASK #{t['id']}: {t['title']}",t['goal']]
            for s in t['steps']:lines+=['',f"{s['n']}. {s['title']} [{s['status']}]",s['result']]
            out.send(chat,'\n'.join(lines))
        elif parts[1] in ('done','doing','blocked','todo'):
            if len(parts)<4 or ' | ' not in parts[3]:raise ValueError('Name the task and step, and tell me its status. Only mark done when finished.')
            step,note=parts[3].split(' | ',1)
            if not note.strip():raise ValueError('Add a completion note.')
            r=T.update_step({'uid':uid},int(parts[2]),int(step),parts[1],note[:600])
            if not r['verified']:raise ValueError(r.get('error','Update unverified'))
            out.send(chat,'Your status note was saved. This is your reported result, not independently checked.\n\n'+render(uid))
        else:raise ValueError('Ask to show or export your tasks, or name a task and step to update.')
    except Exception as e:out.send(chat,'Task request not completed: '+str(e)[:200])
    return True
