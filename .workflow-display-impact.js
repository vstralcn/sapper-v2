export const meta = {
  name: 'assess-structured-display-impact',
  description: 'Assess impact of adopting a structured agent-output display (like the reference UI) into sapper web',
  phases: [{ title: 'Assess' }],
}

const FINDINGS = {
  type: 'object',
  properties: {
    summary: { type: 'string' },
    points: {
      type: 'array',
      items: {
        type: 'object',
        properties: { claim: { type: 'string' }, evidence: { type: 'string' }, severity: { type: 'string', description: 'high|medium|low|none' } },
        required: ['claim', 'evidence', 'severity'],
      },
    },
    verdict: { type: 'string', description: 'Overall: can it be done, and is it advisable given constraints' },
  },
  required: ['summary', 'points', 'verdict'],
}

const ROOT = '/Users/vstral/Code/AI/sapper-v2'
const PROBES = [
  {
    key: 'data-source-readiness',
    prompt: `Repo: ${ROOT}. A reference UI renders agent output as a structured, per-entry collapsible stream: assistant text bubbles, tool-call rows (Bash + truncated command), success/fail marks, a "含推理" (reasoning) tag, top token counters, and a message input box. QUESTION: does sapper already persist enough STRUCTURED event data to render this WITHOUT parsing raw JSONL in the browser or inventing new semantics? Inventory: (1) lib/sapper_chat.py ChatJournal.events and NativeConversation.messages — what fields are stored, are they parsed from JSON, do they include tool_use/tool_result and reasoning? (2) lib/sapper_run.py RunOutput activity.json/activity.log — what is recorded; does it include token usage? (3) lib/sapper_web.py chat_view / chat_message endpoints (around 1801-1822) and how app.js currently renders chat (search web/app.js and web/workspace.js for chat/inbox/message rendering). (4) Is there ANY token/usage number available per agent (grep usage, totalTokens, input_tokens, output_tokens across lib and web)? Be explicit about what data does NOT exist (e.g. usage for Pi). Cite exact file:line. Read-only.`,
  },
  {
    key: 'single-source-of-truth-risk',
    prompt: `Repo: ${ROOT}. Project principle (CODEBUDDY/MEMORY): the web layer is ONLY a visualization of sapper; lib/sapper.py is the single source of truth; web must NOT grow its own concepts or logic that diverges from the CLI/Store semantics. QUESTION: if we add a new structured agent-output view modeled on an external UI (per-entry tool rows, status marks, reasoning tags, message box), what are the concrete risks of violating this principle? Examine: (1) would rendering require the browser to re-interpret native transcripts Pi/Claude events with its own logic (e.g. deciding what counts as a "tool call" or "success"), and where does sapper already normalize events (lib/sapper_runtime.py PiAdapter.normalize_event)? (2) the message-input box in the reference UI implies injecting text into a worker — how does sapper currently do this safely (lib/sapper_web.py:785 send message, terminal input, chat_message, sapper_context.py), and what verification/wake chain (sapper_commander.py) must NOT be bypassed? (3) existing three views — native terminal, agent topology, chat view — would a fourth view duplicate or conflict; check web/index.html tabs and web/app.js. Cite exact file:line and rate each risk. Read-only.`,
  },
  {
    key: 'negative-impacts',
    prompt: `Repo: ${ROOT}. We are evaluating adding a structured agent-output display (collapsible message/tool stream + token counters + message box) to sapper web. Enumerate CONCRETE negative impacts on the EXISTING system. Consider: (1) performance/backpressure: SSE raw-byte terminal streaming (lib/sapper_terminal.py stream, 65536-byte chunks, base64) vs a new parsed structured stream — cost of parsing full transcripts, large file reads, polling. Check limits: activity.log read in sapper_web.py:1195, chat view before-cursor pagination 100 msgs. (2) security/privacy: raw transcripts and tool output contain credentials; note self.redact() usage in sapper_web.py (801, 856) and terminal.snapshot (sapper_terminal.py:82) — would a structured renderer with raw tool args bypass redaction? (3) state-machine/verification: commander phase/verification logic (sapper_commander.py event 386-472) treats tool_use/tool_result as liveness; a parallel display must not feed fake liveness. (4) maintenance/duplication: two renderings of the same events drifting. (5) robustness: Pi session.jsonl can be truncated/incomplete (sapper_pi.py session_checkpoint lines 87-104) — a strict structured parser could break where the tolerant terminal view survives. (6) token counters in reference UI vs reality (usage=0 on Pi deltas). Cite exact file:line, rate severity, and give a mitigation for each. Read-only.`,
  },
]

const results = await parallel(PROBES.map(p => () =>
  agent(p.prompt, { label: p.key, phase: 'Assess', schema: FINDINGS })
    .then(r => ({ key: p.key, ...r }))
))
return { results: results.filter(Boolean) }
