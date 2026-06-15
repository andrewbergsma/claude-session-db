#!/usr/bin/env python3
"""Audit Claude Code skill usage across all session transcripts.

Counts how often each skill was invoked, via two signals:
  - Skill tool calls:  "name":"Skill","input":{"skill":"<name>"   (model-invoked)
  - slash commands:    <command-name>/<name></command-name>        (user-typed)

Then joins against the on-disk global skills (~/.claude/skills) and a
hand-maintained move-map proposing which repo each domain-bound skill should
move to, so global context stays lean.

Output: skills-usage-audit.csv at the repo root.
"""
import re, glob, json, csv, os, collections

PROJECTS = os.path.expanduser('~/.claude/projects')
GLOBAL_SKILLS_DIR = os.path.expanduser('~/.claude/skills')
OUT = os.path.join(os.path.dirname(__file__), '..', 'skills-usage-audit.csv')

tool_re = re.compile(r'"name":"Skill","input":\{"skill":"([a-zA-Z0-9_-]+)"')
cmd_re  = re.compile(r'<command-name>/([a-zA-Z0-9_-]+)</command-name>')

# Built-in CLI commands that are NOT skills — excluded from the audit.
BUILTINS = {
    'clear','loop','continue','exit','effort','mcp','compact','plan','model',
    'login','plugin','theme','remote-env','usage-credits','context','doctor',
    'branch','git-status','keybindings-help','rate-limit-options','reload-plugins',
    'remote-control','statusline','update-config','schedule',
}

# Proposed move target per domain-bound global skill.
# Anything not listed defaults to keep-global (cross-cutting / harness meta).
MOVE = {
    # session/transcript tooling -> this repo
    'session-browser':      ('GitHub/claude-session-db', 'operates on session transcript DB'),
    'session-retrospective':('GitHub/claude-session-db', 'session transcript analysis'),
    'session-watchdog':     ('GitHub/claude-session-db', 'session monitoring'),
    'summary-rollup':       ('GitHub/claude-session-db', 'rolls up session summaries'),
    # P6 ecosystem
    'p6-admin':    ('Projects/p6',                 'P6 EPPM server admin'),
    'p6-api-dev':  ('GitHub/p6eppm_api',           'P6 REST API development'),
    'p6-cloud':    ('GitHub/p6opc_api',            'Oracle Primavera Cloud API'),
    'p6-layouts':  ('GitHub/p6-layout-to-markdown','PLF layout parsing/migration'),
    'p6-migration':('Projects/p6',                 'P6 data migration ETL'),
    'p6-operator': ('GitHub/p6-tools',             'p6eppm CLI operations'),
    'smartpm-operator':('GitHub/p6-tools',         'SmartPM REST API ops'),
    'register-db': ('Projects/gfiber',             'prod_local_register migration views'),
    'client-gfiber':('Projects/gfiber',            'Google Fiber engagement context'),
    # engineering-source / drawings
    'drawings-db': ('Projects/controltech',        'controltech drawings.db pipeline'),
    # infrastructure / homelab
    'media-stack':         ('GitHub/infrastructure','arr/jellyfin media stack on ubuntu103'),
    'infra-ops':           ('GitHub/infrastructure','homelab/proxmox/truenas ops'),
    'open-ports':          ('GitHub/infrastructure','port/network inventory'),
    'server-access':       ('GitHub/infrastructure','server connectivity (x1c etc.)'),
    'windows-admin':       ('GitHub/infrastructure','Windows server admin'),
    'cloudflare-zero-trust':('GitHub/infrastructure','Cloudflare Access/Tunnel admin'),
    # desktop / input devices
    'fileindex-ops':      ('GitHub/fileindex',         'fileindex CLI'),
    'karabiner-config':   ('GitHub/karabiner',         'Karabiner-Elements config'),
    'keyboard-layout':    ('Projects/keyboard-layout', 'ZSA Voyager/QMK layout'),
    'shortcuts-management':('GitHub/keyboard',         'keybinding registry across stack'),
    'raycast':            ('GitHub/raycast-extensions','Raycast ops'),
    'raycast-extension':  ('GitHub/raycast-extensions','Raycast extension dev'),
    'raycast-script-creator':('GitHub/raycast-extensions','Raycast script commands'),
    'visidata-ops':       ('GitHub/visidata-databases','VisiData operations'),
    # macOS / personal data MCPs
    'email-management':   ('GitHub/gmail',                 'email read/draft/search'),
    'email-contact-sync': ('GitHub/gmail',                 'email->Obsidian contact sync'),
    'macos-calendar':     ('GitHub/macos-local-mcp-server','macOS Calendar ops'),
    'reminders':          ('GitHub/macos-local-mcp-server','macOS Reminders ops'),
    'obsidian-curator':   ('GitHub/obsidian-mcp',          'Obsidian vault curation'),
    'run-ui-tests':       ('GitHub/knowledge',             'Knowledge Browser UI tests'),
    # salesforce
    'salesforce-ops':     ('GitHub/salesforce-agent','Salesforce REST ops'),
    # finance / business (personal)
    'canadian-bookkeeping':    ('GitHub/business-dev','CA self-employed bookkeeping'),
    'canadian-finance-planner':('GitHub/business-dev','CA personal finance planning'),
    'billable-timesheet':      ('GitHub/invoicing',   'time entry / billing'),
    'market-intel':            ('GitHub/business-dev','market research'),
    'insurance-agent':         ('GitHub/business-dev','insurance research'),
    # design
    'excalidraw-from-mermaid':('GitHub/design','excalidraw diagram generation'),
}

def main():
    files = glob.glob(os.path.join(PROJECTS, '**', '*.jsonl'), recursive=True)
    tool = collections.Counter(); cmd = collections.Counter()
    for f in files:
        try:
            data = open(f, errors='ignore').read()
        except Exception:
            continue
        for m in tool_re.findall(data): tool[m] += 1
        for m in cmd_re.findall(data): cmd[m] += 1

    global_skills = {d for d in os.listdir(GLOBAL_SKILLS_DIR)
                     if os.path.isdir(os.path.join(GLOBAL_SKILLS_DIR, d))}

    names = (set(tool) | set(cmd) | global_skills) - BUILTINS
    rows = []
    for n in names:
        is_global = n in global_skills
        total = tool[n] + cmd[n]
        if not is_global:
            rec, repo, why = 'n/a (not a global skill)', '', ''
        elif n in MOVE:
            repo, why = MOVE[n]
            rec = 'move'
        else:
            rec, repo, why = 'keep-global', '', 'cross-project / harness meta'
        rows.append((n, tool[n], cmd[n], total, 'yes' if is_global else 'no', rec, repo, why))

    rows.sort(key=lambda r: (r[5] != 'move', -r[3], r[0]))
    with open(OUT, 'w', newline='') as out:
        w = csv.writer(out)
        w.writerow(['skill','tool_invocations','slash_invocations','total_uses',
                    'is_global','recommendation','proposed_repo','rationale'])
        w.writerows(rows)
    print(f"scanned {len(files)} transcripts; wrote {os.path.abspath(OUT)}")

if __name__ == '__main__':
    main()
