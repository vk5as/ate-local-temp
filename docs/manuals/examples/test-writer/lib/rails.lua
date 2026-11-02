-- lib/rails.lua: an ordinary Lua module. Its module name is derived from its path:
-- "lib/rails.lua" becomes "lib.rails". require runs a module once per Test Run and
-- caches what it returns; later requires of the same name get the cached value.
local rails = {}

function rails.nominalVolts(channel)
  return 1.5 * channel
end

return rails
