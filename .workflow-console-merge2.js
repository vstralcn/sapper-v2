export const meta = {
  name: 'console-tab-merge-research2',
  description: 'Verify other-tab overlap and folding mechanics for console merge',
  phases: [{ title: 'Research' }],
}

const ROOT = '/Users/vstral/Code/AI/sapper-v2'
const PROBES = [
  {
    key: 'other-tabs-overlap',
    prompt: `Repo: ${ROOT}. Research question, answer in structured prose with exact file:line citations. Do NOT modify files.

The user suspects "原生功能" (data-tab="native", #tab-native in web/index.html:254-258, web/native.js) and "文件与附件" (data-tab="files", #tab-files in web/index.html:252) may be historical overlap like console/workspace were. Verify rigorously.

1. "原生功能" tab: what does web/native.js actually show and do? Is it the sapper CLI command catalog/preview/execute (check web/native.js and lib/sapper_web.py native catalog endpoints around 1761-1777)? List its sections and data sources. Does it duplicate anything in another tab, or is it unique?

2. "文件与附件" tab: web/app.js loadFiles() around 492-540 and #files-list/#files-path/#files-up. Compare to the "题目文件" panel inside 终端与文件 (web/workspace.js workspaceFiles() around 223-236, #workspace-file-list). Are they the SAME data source (taskApi files?path=) or different in purpose (uploaded attachments vs project working dir)? Look at attachment upload flow (web/app.js around 877-885, staged attachments, recorded as hint). Determine if these two file surfaces are redundant or complementary.

3. Verdict for each of the two tabs: unique indispensable function vs redundant with another tab. If no overlap, say so explicitly.

Give exact file:line for every claim.`,
  },
  {
    key: 'folding-mechanics',
    prompt: `Repo: ${ROOT}. Research question, answer in structured prose with exact file:line citations. Do NOT modify files.

Research MECHANICS for folding console's low-frequency functions into "终端与文件" (#tab-workspace), frontend-only, no backend changes.

1. Emergency brake "停止此会话/延长预算" in Agent status panel: web/workspace.js renderAgents() around 300-323 already has an "延长预算" button at :404 calling openRunBudget(a.run_id). Confirm exactly what exists today, what is missing (stop button), and whether an agent row has enough data (run_id, status, execution_mode) to render stop/extend there. Cite lines. Note app.js:461-467 has the stop logic to reuse.

2. Secondary entry point styled like the existing "原 worker 管理" button (web/index.html:247 #worker-manage, dialog at :554-556 #worker-dialog, handler app.js:841, form submit app.js:808-813). Describe the EXACT existing pattern (button in toolbar -> <dialog> showModal -> form submit -> api call -> toast). A new "运行记录/历史输出" drawer/dialog should mirror it. Which dialog markup pattern to copy, and where would the button physically sit in #tab-workspace?

3. Watchdog heartbeat noise filtering: log lines are produced by lib/sapper_commander.py WatchdogLog.line() at :198-221 with format "[watchdog] " + kind("heartbeat"|"state") + " " + json. app.log() reads activity.log in lib/sapper_web.py:1192-1211. Assess: can the frontend filter/fold ONLY "[watchdog] heartbeat" lines in #log-output with a pure client-side string filter, without backend changes? What is the risk (hiding real evidence — note "state" lines may carry recovery evidence)? Cite code.

4. List CSS in web/style.css tied to .console-grid/.run-sidebar/.terminal-output/.log-panel that becomes dead after deletion (style.css lines 33,35,36,37).

Give exact file:line for every claim.`,
  },
]

const results = await parallel(PROBES.map(p => () =>
  agent(p.prompt, { label: p.key, phase: 'Research' })
))
return { results }
