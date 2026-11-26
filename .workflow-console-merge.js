export const meta = {
  name: 'console-tab-merge-research',
  description: 'Code-level research for merging 运行记录 tab into 终端与文件 without touching backend',
  phases: [{ title: 'Research' }],
}

const FINDINGS = {
  type: 'object',
  properties: {
    summary: { type: 'string' },
    items: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          what: { type: 'string' },
          location: { type: 'string', description: 'exact file:line' },
          action: { type: 'string', description: 'delete | keep | migrate | rewire | verify' },
          detail: { type: 'string' },
        },
        required: ['what', 'location', 'action', 'detail'],
      },
    },
    conclusion: { type: 'string', description: 'concrete merge plan and risks' },
  },
  required: ['summary', 'items', 'conclusion'],
}

const ROOT = '/Users/vstral/Code/AI/sapper-v2'
const PROBES = [
  {
    key: 'console-dependency-graph',
    prompt: `Repo: ${ROOT}. TASK: code-level research for DELETING the "运行记录" top-level tab (data-tab="console", #tab-console in web/index.html:174,246-250) and folding its useful bits into "终端与文件" (#tab-workspace), frontend-only, NO backend/state-machine changes. Build a COMPLETE dependency graph of everything tied to console so nothing breaks.

Trace precisely (cite exact file:line for each):
1. Every call site of setTab("console") and openProject(pid,"console") across web/app.js, web/workspace.js, web/overview.js, web/native.js. For each, say what user action triggers it and where it should be rewired if the tab is gone.
2. Every call site of openLog(), openRunBudget(), openPiTask(), openRun(), close-log, follow-log, and the DOM elements #log-panel/#runs-list/#log-output/#pane-select/#terminal-output/#terminal-form/[data-key] buttons/#worker-manage/#open-shell. Which are ONLY used inside console (safe to delete/move) and which are called from other tabs (must be preserved/relocated)?
3. The 2-second poll loop at web/app.js:901-911 (refreshTerminal+refreshLog) and baseSetTab console branch at :357 — what must be removed vs kept.
4. refreshTask() at app.js:136-175 unconditionally calls renderPanes()+renderRuns() for every tab; note that their DOM targets live only in #tab-console. What happens if those DOM nodes are deleted? (silent no-op? throw?) Cite code.
5. Cross-file: web/workspace.js:350-354,403-405,441 redirect to console; web/overview.js:211 and web/native.js:274 call openProject(...,"console")+openLog. List each.

Return an itemized delete/keep/migrate/rewire classification. Read-only, do NOT modify files.`,
  },
  {
    key: 'other-tabs-overlap',
    prompt: `Repo: ${ROOT}. The user suspects "原生功能" (data-tab="native", #tab-native in web/index.html:254-258, web/native.js) and "文件与附件" (data-tab="files", #tab-files in web/index.html:252) may also be historical overlap, like console/workspace were. Verify rigorously.

Investigate:
1. "原生功能" tab: what does web/native.js actually show? Is it the sapper CLI command catalog/preview/execute (web/native.js, lib/sapper_web.py native catalog endpoints ~1761-1777)? Does it duplicate anything in other tabs? Read native.js fully enough to list its sections and what data source it uses.
2. "文件与附件" tab: web/app.js loadFiles() ~492+ and #files-list/#files-path/#files-up. Compare to the "题目文件" panel inside 终端与文件 (workspace.js workspaceFiles() ~223-236, #workspace-file-list). Are they the SAME data (taskApi files?path=) or different (uploaded attachments vs project working dir)? Look at attachment upload flow (app.js ~877-885, staged attachments, taskApi upload) and whether it is recorded as hint. Determine if they are redundant or complementary.
3. For each of the two tabs, give a verdict: unique indispensable function vs redundant with another tab, with exact file:line evidence. Explicitly state if there is NO overlap.

Be precise and cite exact file:line. Read-only, do NOT modify files.`,
  },
  {
    key: 'folding-mechanics',
    prompt: `Repo: ${ROOT}. Research the MECHANICS for folding console's low-frequency functions into "终端与文件" (#tab-workspace), frontend-only, no backend changes.

Determine:
1. "停止此会话/延长预算" as emergency brake in Agent status panel: web/workspace.js renderAgents() ~300-323 already has an "延长预算" button at :404 calling openRunBudget(a.run_id). Confirm exactly what exists today and what is missing (stop button? where would it go?). Check what data an agent row has (run_id, status, execution_mode) to decide if stop/extend can be rendered there. Cite lines.
2. Secondary entry point styled like the existing "原 worker 管理" button (web/index.html:247 #worker-manage, dialog at :554-556 #worker-dialog, handler app.js:841). Describe the EXACT existing pattern (button in toolbar -> <dialog> showModal -> form submit -> api call -> toast) so a new "运行记录/历史输出" drawer/dialog can mirror it. Which dialog markup pattern to copy?
3. Watchdog heartbeat noise filtering: run log content comes from lib/sapper_run.py RunOutput.observe() (activity.log) and app.log() (sapper_web.py:1192-1211). Find what a "[watchdog]" heartbeat line actually looks like in the logs (grep repo for watchdog heartbeat text in sapper_commander.py / run notes, e.g. '看门' 'watchdog' '保活' 'heartbeat'). Assess: can the frontend filter/fold these lines in #log-output with a pure client-side string filter, without backend changes? What are the risks (hiding real evidence)? Cite code.
4. Flag any CSS in web/style.css tied to .console-grid / .run-sidebar / .terminal-output / .log-panel that would become dead after deletion.

Return concrete, code-grounded mechanics with exact file:line. Read-only, do NOT modify files.`,
  },
]

const results = await parallel(PROBES.map(p => () =>
  agent(p.prompt, { label: p.key, phase: 'Research', schema: FINDINGS })
    .then(r => ({ key: p.key, ...r }))
))
return { results: results.filter(Boolean) }
