-- ate.devices: the Device params builders (packet 16d).
-- A builder returns the params table itself: the fields are plain fields and the setters live in a
-- shared metatable's __index, so pairs() sees only the params, the table goes straight into
-- ATE.open/read/write with no :build(), and the unknown-field check passes. Open builders carry every
-- field their Device Type requires (open has no defaults); Read()/Write() start with only what the
-- call needs, and a per-call field left out falls back to the Device's open configuration. Setters
-- don't validate: the open/read/write check rejects a bad value and names the field. A setter the
-- Type lacks is a call on nil, like any other typo. The metatables are locked (__metatable = false)
-- so a schedule can't reach the shared setter tables through getmetatable and change every builder.
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
local VISA_SETTERS = {
  setResource = set("resource"), setTimeout = set("timeout"),
  setReadTerm = set("readTerm"), setWriteTerm = set("writeTerm"),
}
-- read/write overrides: every field is optional, so no defaults are seeded here.
local READ_SETTERS = {setSize = set("size"), setTimeout = set("timeout"), setReadTerm = set("readTerm")}
local WRITE_SETTERS = {setWriteTerm = set("writeTerm"), setTimeout = set("timeout")}

local function locked(setters) return {__index = setters, __metatable = false} end
local TCP_MT, SERIAL_MT, UDP_MT = locked(TCP_SETTERS), locked(SERIAL_SETTERS), locked(UDP_SETTERS)
local VISA_MT = locked(VISA_SETTERS)
local READ_MT, WRITE_MT = locked(READ_SETTERS), locked(WRITE_SETTERS)

return {
  Tcp = function(host, port)
    return setmetatable({host = host, port = port, timeout = 1.0, readTerm = "\n", writeTerm = "\n"},
      TCP_MT)
  end,
  Serial = function(port)
    return setmetatable({port = port, baud = 9600, dataBits = 8, stopBits = 1,
      parity = Parity.NONE, flowControl = FlowControl.NONE, timeout = 1.0, readTerm = "\n", writeTerm = "\n"},
      SERIAL_MT)
  end,
  Udp = function(host, port)
    return setmetatable({host = host, port = port, localPort = 0, timeout = 1.0}, UDP_MT)
  end,
  Visa = function(resource)
    return setmetatable({resource = resource, timeout = 1.0, readTerm = "\n", writeTerm = "\n"}, VISA_MT)
  end,
  Read = function()
    return setmetatable({}, READ_MT)
  end,
  Write = function(data)
    return setmetatable({data = data}, WRITE_MT)
  end,
}
