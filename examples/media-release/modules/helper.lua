-- Example Media Release: a helper module with an explicit module name. example.atedef lists this
-- file with "module": "example_helper", so require("example_helper") finds it rather than the
-- derived "modules.helper".

local VERSION = "1.0"

-- One line describing a Device from params a builder made, without opening anything.
local function describe(what, host, port)
  return what .. " at " .. host .. ":" .. port
end

return { describe = describe, version = VERSION }
