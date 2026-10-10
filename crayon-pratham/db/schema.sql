create extension if not exists pgcrypto;
create table if not exists public.users (id text primary key, created_at timestamptz default now());
create table if not exists public.messages (id uuid primary key default gen_random_uuid(), user_id text references public.users(id) on delete cascade, role text not null, content text not null, created_at timestamptz default now());
create table if not exists public.notes (id uuid primary key default gen_random_uuid(), user_id text references public.users(id) on delete cascade, body text not null, created_at timestamptz default now());
create table if not exists public.reminders (id uuid primary key default gen_random_uuid(), user_id text references public.users(id) on delete cascade, body text not null, remind_at timestamptz not null, sent_at timestamptz);
create table if not exists public.connected_accounts (user_id text primary key references public.users(id) on delete cascade, provider text not null, encrypted_token text not null, updated_at timestamptz default now());
alter table public.users enable row level security;
alter table public.messages enable row level security;
alter table public.notes enable row level security;
alter table public.reminders enable row level security;
alter table public.connected_accounts enable row level security;
-- The service role is used only server-side; never expose it to Telegram users.
create index if not exists messages_user_created on public.messages(user_id, created_at);
create index if not exists reminders_due on public.reminders(remind_at) where sent_at is null;
