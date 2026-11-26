export const meta = {
  name: 'check-tab-overlap',
  description: 'Verify whether console and workspace tabs are functionally duplicated',
  phases: [{ title: 'Verify' }],
}

const FINDINGS = {
  type: 'object',
  properties: {
    summary: { type: 'string' },
    unique_to_tab: { type: 'array', items: { type: 'string' }, description: 'Functions ONLY available in the assigned tab' },
    shared_with_other: { type: 'array', items: { type: 'string' }, description: 'Functions duplicated in both tabs' },
    evidence: { type: 'array', items: { type: 'string' } },
    verdict: { type: 'string', description: 'Are they truly redundant? What can/cannot be merged?' },
  },
  required: ['summary', 'unique_to_tab', 'shared_with_other', 'evidence', 'verdict'],
}

const ROOT = '/Users/vstral/Code/AI/sapper-v2'
const PROBES = [
  {
    key: 'workspace-tab',
    prompt: `Repo: ${ROOT}. Inventory ALL functions of the "终端与文件" tab (data-tab="workspace", section tab-workspace in web/index.html). Read web/index.html:221, web/workspace.js (especially renderNativePanes/selectNative around 481-525, setTab override 86-119), web/app.js:348-388. Determine: (1) what terminal transport it uses (SSE stream xterm.js vs snapshot) and the API endpoint; (2) its UNIQUE functions not available elsewhere — e.g. 题目文件列表 (workspace-files), Agent 状态 panel, service list, 接管输入 (terminal-control), live PTY with ANSI; (3) whether it can show run logs / run management (budget/stop/download of print runs). Cite exact file:line. Be exhaustive; do NOT modify files.`,
  },
  {
    key: 'console-tab',
    prompt: `Repo: ${ROOT}. Inventory ALL functions of the "运行记录" tab (data-tab="console", section tab-console in web/index.html:246-250). Read web/app.js:360-388 (renderPanes/refreshTerminal), 427+ (runs-list), 477+ (log panel), and lib/sapper_terminal.py snapshot logic. Determine: (1) what terminal transport it uses (snapshot polling endpoint) vs the workspace tab's SSE; (2) its UNIQUE functions — e.g. runs-list with 延长预算/停止此会话/下载原始输出, log-panel showing RunOutput activity for print/pi_task runs that have NO pane; (3) whether those unique functions are reachable from the workspace tab. (4) Confirm if both tabs embed a "select pane -> view PTY output" terminal and whether that is the duplication. Cite exact file:line. Be exhaustive; do NOT modify files.`,
  },
]

const results = await parallel(PROBES.map(p => () =>
  agent(p.prompt, { label: p.key, phase: 'Verify', schema: FINDINGS })
    .then(r => ({ key: p.key, ...r }))
))
return { results: results.filter(Boolean) }
