"""Request-local channel restrictions; never widen existing member permissions."""
from contextvars import ContextVar
channel = ContextVar('crayon_channel', default='telegram')
WEB_TOOLS = frozenset({'get_time','remember','save_note','list_notes','set_reminder',
    'list_reminders','cancel_reminder','create_task','list_tasks','update_step','close_task',
    'web_search','read_url','run_python','draft_message','research_web','convert_units','currency_rate',
    'mcp_public_tool','mcp_review_tool','create_csv','create_bar_chart','computer_status','computer_task','computer_browse','schedule_job'})
