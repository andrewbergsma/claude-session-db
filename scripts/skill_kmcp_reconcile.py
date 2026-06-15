import csv, os

# --- kmcp skill entities (app, path) : 75 total ---
kmcp = []
agent = ["skill/drawings-db","skill/ingest","skill/ingest-docx","skill/ingest-email","skill/ingest-pdf",
"skill/ingest-structured-text","skill/ingest-xlsx","skill/kmcp-dev","skill/kmcp-dev-asyncpg","skill/kmcp-dev-bcrypt",
"skill/kmcp-dev-embeddings","skill/kmcp-dev-httpx","skill/kmcp-dev-jinja2","skill/kmcp-dev-mcp-sdk","skill/kmcp-dev-mistune",
"skill/kmcp-dev-nh3","skill/kmcp-dev-pgvector","skill/kmcp-dev-pyyaml","skill/kmcp-dev-starlette","skill/kmcp-dev-uvicorn",
"skill/new-skill","skill/open-tasks"]
cs = ["chezmoi/management","consulting/client-context","consulting/register-db","consulting/salesforce-ops",
"controltech/db-design","engineering/diagnose","engineering/grill-with-docs","engineering/improve-codebase-architecture",
"engineering/prototype","engineering/setup-matt-pocock-skills","engineering/tdd","engineering/to-issues","engineering/to-prd",
"engineering/triage","engineering/zoom-out","in-progress/review","in-progress/writing-beats","in-progress/writing-fragments",
"in-progress/writing-shape","knowledge-mcp/audit-overview","knowledge-mcp/create-task","knowledge-mcp/know",
"knowledge-mcp/knowledge-quality","knowledge-mcp/maintain-knowledge","knowledge-mcp/new-app","knowledge-mcp/sync-overview-claudemd",
"misc/git-guardrails-claude-code","misc/migrate-to-shoehorn","misc/scaffold-exercises","misc/setup-pre-commit",
"p6/admin","p6/api-dev","p6/cloud","p6/layouts","p6/migration","p6/operator","productivity/caveman","productivity/grill-me",
"productivity/handoff","productivity/write-a-skill","workflow/delegate","workflow/follow-up-harvest","workflow/run-tasks",
"workflow/session-retrospective","workflow/session-summary"]
kcode = ["skill/curate","skill/dispatch","skill/frontend","skill/overview","skill/relate","skill/security","skill/taxonomy","skill/ui-test"]
for p in agent: kmcp.append(("agent",p))
for p in cs: kmcp.append(("claude_skills",p))
for p in kcode: kmcp.append(("knowledge_mcp_code",p))

# entity_type=knowledge but is really a skill:
kmcp_extra_alias = {"lesson": ("claude_skills","knowledge-mcp/lesson (typed 'knowledge')")}

def match_key(app, path):
    leaf = path.split('/')[-1]
    bucket = path.split('/')[0]
    if bucket == 'skill': return leaf
    if bucket == 'p6': return 'p6-'+leaf
    if path == 'chezmoi/management': return 'chezmoi-management'
    return leaf

kmcp_by_key = {}
for app,path in kmcp:
    kmcp_by_key.setdefault(match_key(app,path), []).append(f"{app}::{path}")

# dev/internal kmcp skills that are NOT meant to be local global skills
DEV_INTERNAL = lambda loc: ('kmcp-dev' in loc) or (loc.startswith('knowledge_mcp_code::') and '::skill/dispatch' not in loc)

# --- local global skills + usage ---
local = {}
with open('skills-usage-audit.csv') as f:
    for r in csv.DictReader(f):
        if r['is_global']=='yes':
            local[r['skill']] = int(r['total_uses'])

# apply lesson alias
for k,v in kmcp_extra_alias.items():
    kmcp_by_key.setdefault(k, []).append(f"{v[0]}::{v[1]}")

local_keys = set(local)
kmcp_keys = set(kmcp_by_key)

both       = sorted(local_keys & kmcp_keys)
local_only = sorted(local_keys - kmcp_keys)
kmcp_only  = sorted(kmcp_keys - local_keys)

rows=[]
for k in both:
    rows.append((k, 'in-both', local[k], '; '.join(kmcp_by_key[k]),
                 'ALIGN: make local a thin pointer to kmcp canonical body'))
for k in local_only:
    rows.append((k, 'local-only', local[k], '',
                 'PUSH: ingest into kmcp claude_skills (no canonical entry yet)'))
for k in kmcp_only:
    locs='; '.join(kmcp_by_key[k])
    if DEV_INTERNAL(locs):
        act='LEAVE: kmcp-internal agent/dev skill, not a global skill'
    else:
        act='PULL?: kmcp-only portable skill, materialize local pointer if wanted'
    rows.append((k, 'kmcp-only', '', locs, act))

with open('skills-kmcp-reconciliation.csv','w',newline='') as out:
    w=csv.writer(out)
    w.writerow(['skill','status','local_uses','kmcp_location','recommended_action'])
    w.writerows(rows)

print(f"local global skills : {len(local_keys)}")
print(f"kmcp skill entities : {len(kmcp)}  (unique match-keys: {len(kmcp_keys)})")
print(f"  in-both     : {len(both)}")
print(f"  local-only  : {len(local_only)}  (need PUSH to kmcp)")
print(f"  kmcp-only   : {len(kmcp_only)}")
ki=[k for k in kmcp_only if DEV_INTERNAL('; '.join(kmcp_by_key[k]))]
kp=[k for k in kmcp_only if not DEV_INTERNAL('; '.join(kmcp_by_key[k]))]
print(f"     - dev/internal (leave) : {len(ki)}")
print(f"     - portable (pull?)     : {len(kp)}")
print("\n=== IN BOTH (align to pointer) ===")
print(', '.join(both))
print("\n=== KMCP-ONLY portable (candidates to pull local) ===")
print(', '.join(kp))
print("\n=== KMCP-ONLY dev/internal (leave) ===")
print(', '.join(ki))
print("\nWROTE skills-kmcp-reconciliation.csv")
