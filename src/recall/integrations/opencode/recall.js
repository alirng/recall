// recall's edit hook for OpenCode, added by `recall install` and removed by `recall uninstall`.
// Before an edit it may refuse it once, with recall's reason as the model's next instruction;
// after an edit it may add a note to the tool's result. See recall/integrations/hooks.py.
import { execFile } from "node:child_process"

const RECALL = "__RECALL__"
const EDITS = ["edit", "write", "apply_patch"]

function check(phase, sessionID, tool, args, cwd) {
  const payload = JSON.stringify({ hook_event_name: phase, session_id: sessionID, tool, args, cwd })
  return new Promise((resolve) => {
    const child = execFile(RECALL, ["status", "--hook", "--agent", "opencode"], { timeout: 10000 }, (err, out) => {
      try {
        resolve(!err && out.trim() ? JSON.parse(out) : null)
      } catch {
        resolve(null)
      }
    })
    child.stdin.end(payload)
  })
}

export const Recall = async ({ directory }) => ({
  "tool.execute.before": async (input, output) => {
    if (!EDITS.includes(input.tool)) return
    const r = await check("before", input.sessionID, input.tool, output.args, directory)
    if (r?.deny) throw new Error(r.deny)
  },
  "tool.execute.after": async (input, output) => {
    if (!EDITS.includes(input.tool)) return
    const r = await check("after", input.sessionID, input.tool, input.args, directory)
    if (r?.tell) output.output += `\n\n${r.tell}`
  },
})
