"""User-requested natural check-in wording. No inferred milestones or automatic snooping."""
import re
import cr_tools as T

def handle(uid,chat,text,out):
    natural=re.fullmatch(r'(?i)(?:please )?(?:check in|follow up|ask me)(?: with me)? (?:in|after) (\d{1,3}) (hours?|days?) (?:about|on) (.{1,180})',text.strip())
    if natural:text='/followup '+str(int(natural[1])*(24 if natural[2].lower().startswith('day') else 1))+'h '+natural[3]
    if not text.startswith('/followup'):return False
    if chat<0:out.send(chat,'Follow-ups are private-DM only.');return True
    m=re.fullmatch(r'/followup (\d{1,3})h (.{1,180})',text.strip())
    if not m or not 1<=int(m[1])<=168:out.send(chat,'Ask me to check in after a number of hours or days about your topic (1-168hours). This schedules only the follow-up you request, not ongoing monitoring.');return True
    # Respect the account quiet-hour window by deferring to its next awake hour.
    import cr_proactive as P
    from datetime import timedelta
    settings=P.settings(uid);due=T.now_local(uid)+timedelta(hours=int(m[1]));adjusted=0
    while not P.awake(due.hour,int(settings['quiet_start']),int(settings['quiet_end'])) and adjusted<24:
        due+=timedelta(hours=1);adjusted+=1
    # Uses existing requested reminder semantics and current timezone calculation.
    result=T.set_reminder({'uid':uid,'chat_id':chat},text='How did '+m[2].rstrip('?.!')+' go? Want to talk it through?',in_minutes=(int(m[1])+adjusted)*60)
    if result.get('verified') and result.get('delivery'):
        result['note']='Follow-up scheduled for '+str(result.get('due_local','your requested time'))+'. Delivery: '+result['delivery']+'. Free-host delivery is best-effort.'
    out.send(chat,result.get('note') or ('Follow-up scheduled for '+str(result.get('due_local','your requested time'))+'. Free-host delivery is best-effort.' if result.get('verified') else 'Follow-up was not confirmed.'))
    return True
