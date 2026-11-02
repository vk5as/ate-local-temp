-- Example Media Release: a helper module with an explicit module name. example.atedef lists this
-- file with "module": "example_helper", so require("example_helper") finds it rather than the
-- derived "modules.helper".

local VERSION = "1.0"

-- Run-global like lib.attempts: require is cached for the Test Run, so every schedule that requires
-- this module shares one table, and this count is how the harness proves it.
local uses = 0

-- One line describing a Device from params a builder made, without opening anything.
local function describe(what, host, port)
  uses = uses + 1
  return what .. " at " .. host .. ":" .. port .. " (helper " .. VERSION .. ", use " .. uses .. ")"
end

return { describe = describe, version = VERSION }
