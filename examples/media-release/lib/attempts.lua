-- Example Media Release: a helper module, required as "lib.attempts" - the name derived from the
-- path with .lua dropped and / turned into . (test-definition-format.md, "Module names").
--
-- A module runs once per Test Run and require returns the cached table, so the top-level schedule
-- and every include of a Sub-schedule share these counters. That is what makes the retry work and
-- what the harness asserts about state shared across includes.
local state = { attempts = 0, includes = 0 }

-- Run-global: nextAttempt counts its own calls across the whole Test Run, not the Attempt number of
-- whichever step calls it - the spec gives a step no way to read its own Attempt number.
local function nextAttempt()
  state.attempts = state.attempts + 1
  return state.attempts
end

-- Called by each included instance, so the count shows one shared table across the includes.
local function noteInclude(label)
  state.includes = state.includes + 1
  return state.includes
end

return { state = state, nextAttempt = nextAttempt, noteInclude = noteInclude }
