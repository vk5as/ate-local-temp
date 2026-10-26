-- ate.devices: the Device params builders (packet 16d).
-- A builder returns the params table itself: the fields are plain fields and the setters live in a
-- shared metatable's __index, so pairs() sees only the params, the table goes straight into
-- ATE.open/read/write with no :build(), and the unknown-field check passes. Open builders carry every
-- field their Device Type requires (open has no defaults); Read()/Write() start with only what the
-- call needs, and a per-call field left out falls back to the Device's open configuration. Setters
-- don't validate: the open/read/write check raises and names the field. A setter the Type lacks is a
-- call on nil, like any other typo.
local function set(field)
  return function(t, value) t[field] = value return t end
end

local TCP_SETTERS = {
  setHost = set("host"), setPort = set("port"), setTimeout = set("timeout"),
  setReadTerm = set("readTerm"), setWriteTerm = set("writeTerm"),
}
local SERIAL_SETTERS = {
  setPort = set("port"), setBaud = set("baud"), setDataBits = set("dataBits"),
  setStopBits = set("stopBits"), setParity = set("parity"), setFlowControl = set("flowControl"),
  setTimeout = set("timeout"), setReadTerm = set("readTerm"), setWriteTerm = set("writeTerm"),
}
local UDP_SETTERS = {
  setHost = set("host"), setPort = set("port"), setLocalPort = set("localPort"), setTimeout = set("timeout"),
}
-- read/write overrides: every field is optional, so no defaults are seeded here.
local READ_SETTERS = {setSize = set("size"), setTimeout = set("timeout"), setReadTerm = set("readTerm")}
local WRITE_SETTERS = {setWriteTerm = set("writeTerm"), setTimeout = set("timeout")}

return {
  Tcp = function(host, port)
    return setmetatable({host = host, port = port, timeout = 1.0, readTerm = "\n", writeTerm = "\n"},
      {__index = TCP_SETTERS})
  end,
  Serial = function(port)
    return setmetatable({port = port, baud = 9600, dataBits = 8, stopBits = 1,
      parity = Parity.NONE, flowControl = FlowControl.NONE, timeout = 1.0, readTerm = "\n", writeTerm = "\n"},
      {__index = SERIAL_SETTERS})
  end,
  Udp = function(host, port)
    return setmetatable({host = host, port = port, localPort = 0, timeout = 1.0}, {__index = UDP_SETTERS})
  end,
  Read = function()
    return setmetatable({}, {__index = READ_SETTERS})
  end,
  Write = function(data)
    return setmetatable({data = data}, {__index = WRITE_SETTERS})
  end,
}
