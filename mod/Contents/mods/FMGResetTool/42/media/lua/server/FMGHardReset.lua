if not isServer() then return end

-- Hard Reset (Player Data only), second half.
-- The FMG Soft Reset backend removes the character files while the server is stopped and
-- leaves this note in Zomboid/Lua. Safehouses and factions are stored inside map_meta.bin
-- together with world data, so they are removed here with the game's own functions, once,
-- when the server starts and before anyone can join. Claim mods that rebuild safehouses
-- from their own records (Knox Claim) are cleared first. Ten in-game minutes later the
-- safehouses are counted again, so a mod that rebuilds them shows up in the note and log.
local NOTE_FILE = "fmg-hard-reset-pending.json"
local KNOX_CLAIM_KEY = "KnoxClaim"
local NOTE_FIELDS = {
    "status", "requestedAt", "saveName", "rollback", "completedAt", "safehousesRemoved",
    "factionsRemoved", "knoxClaimFound", "knoxLandClaimsRemoved", "knoxVehicleClaimsRemoved",
    "checkedAt", "safehousesAfterCheck", "error",
}
local result = nil -- fields of a hard reset finished on this start, until the follow-up check

local function escapeJson(value)
    local text = tostring(value or "")
    text = text:gsub("\\", "\\\\"):gsub('"', '\\"'):gsub("[%z\1-\31]", " ")
    return text
end

local function encodeNote(fields)
    local parts = {}
    for _, key in ipairs(NOTE_FIELDS) do
        local value = fields[key]
        if value ~= nil then
            local encoded
            if type(value) == "number" then
                encoded = string.format("%d", value)
            elseif type(value) == "boolean" then
                encoded = value and "true" or "false"
            else
                encoded = '"' .. escapeJson(value) .. '"'
            end
            parts[#parts + 1] = '"' .. key .. '":' .. encoded
        end
    end
    return "{" .. table.concat(parts, ",") .. "}"
end

local function readNote()
    local reader = getFileReader(NOTE_FILE, false)
    if not reader then return nil end
    local lines = {}
    local ok = pcall(function()
        local line = reader:readLine()
        while line do
            lines[#lines + 1] = line
            line = reader:readLine()
        end
    end)
    pcall(function() reader:close() end)
    if not ok then return nil end
    return table.concat(lines, "\n")
end

local function writeNote(fields)
    local writer = getFileWriter(NOTE_FILE, true, false)
    if not writer then error("could not open " .. NOTE_FILE) end
    local ok, err = pcall(function()
        writer:write(encodeNote(fields))
        writer:close()
    end)
    if not ok then
        pcall(function() writer:close() end)
        error("could not write " .. NOTE_FILE .. ": " .. tostring(err))
    end
end

local function noteString(note, key)
    return note:match('"' .. key .. '"%s*:%s*"([^"]*)"')
end

local function emptyTable(value)
    if type(value) ~= "table" then return 0 end
    local keys = {}
    for key in pairs(value) do keys[#keys + 1] = key end
    for _, key in ipairs(keys) do value[key] = nil end
    return #keys
end

-- Knox Claim keeps its own land and vehicle claims in global mod data and, every ten
-- in-game minutes, rebuilds a vanilla safehouse for each land claim that has none.
-- Its claims must go first, otherwise it puts the removed safehouses straight back.
-- The tables are emptied in place, so any reference Knox Claim holds sees them empty.
-- Vehicles stay where they are; a vehicle without a claim entry counts as unclaimed.
local function clearKnoxClaim()
    if not ModData.exists(KNOX_CLAIM_KEY) then return 0, 0, false end
    local store = ModData.get(KNOX_CLAIM_KEY)
    if type(store) ~= "table" then return 0, 0, false end
    local landClaims = emptyTable(store.claims)
    local vehicleClaims = emptyTable(store.vehicles)
    emptyTable(store.requests)
    ModData.transmit(KNOX_CLAIM_KEY)
    return landClaims, vehicleClaims, true
end

local function removeSafehouses()
    local list = SafeHouse.getSafehouseList()
    local items = {}
    for index = 0, list:size() - 1 do items[#items + 1] = list:get(index) end
    for _, safehouse in ipairs(items) do SafeHouse.removeSafeHouse(safehouse) end
    return #items, SafeHouse.getSafehouseList():size()
end

local function removeFactions()
    local list = Faction.getFactions()
    local count = list:size()
    list:clear()
    return count, Faction.getFactions():size()
end

-- Runs fn (which returns removed, left); records a failure in errors and returns the removed count.
local function runStep(label, fn, errors)
    local ok, removed, left = pcall(fn)
    if not ok then
        errors[#errors + 1] = label .. ": " .. tostring(removed)
        return 0
    end
    if left ~= 0 then errors[#errors + 1] = tostring(left) .. " " .. label .. " could not be removed" end
    return removed
end

local checkSafehousesStayedRemoved

local function runPendingHardReset()
    local note = readNote()
    if not note or not note:find('"status"%s*:%s*"pending"') then return end
    -- Local hosting shares one Zomboid/Lua folder between every hosted save; only the
    -- save the hard reset was made for may act on the note.
    local saveName = noteString(note, "saveName")
    local thisServer = getServerName and getServerName() or nil
    if saveName and saveName ~= "" and thisServer and thisServer ~= saveName then
        print(string.format("[FMGResetTool] Hard reset note is for save '%s', this server is '%s'; leaving it pending.",
            saveName, thisServer))
        return
    end
    print("[FMGResetTool] Hard reset pending: removing all safehouses and factions.")

    local errors = {}
    local okKnox, knoxLand, knoxVehicles, knoxPresent = pcall(clearKnoxClaim)
    if not okKnox then
        errors[#errors + 1] = "Knox Claim: " .. tostring(knoxLand)
        knoxLand, knoxVehicles, knoxPresent = 0, 0, true
    elseif knoxPresent then
        print(string.format("[FMGResetTool] Hard reset: cleared Knox Claim (%d land claims, %d vehicle claims).",
            knoxLand, knoxVehicles))
    end
    local safehouses = runStep("safehouses", removeSafehouses, errors)
    local factions = runStep("factions", removeFactions, errors)

    result = {
        status = #errors == 0 and "done" or "failed",
        requestedAt = noteString(note, "requestedAt") or "",
        saveName = saveName,
        rollback = noteString(note, "rollback") or "",
        completedAt = os.date("!%Y-%m-%dT%H:%M:%SZ"),
        safehousesRemoved = safehouses,
        factionsRemoved = factions,
        knoxClaimFound = knoxPresent,
        knoxLandClaimsRemoved = knoxLand,
        knoxVehicleClaimsRemoved = knoxVehicles,
        error = table.concat(errors, "; "),
    }
    local ok, err = pcall(writeNote, result)
    if not ok then print("[FMGResetTool] ERROR: " .. tostring(err)) end
    if result.status == "done" then
        print(string.format("[FMGResetTool] Hard reset finished: removed %d safehouses and %d factions.",
            safehouses, factions))
    else
        print("[FMGResetTool] ERROR: hard reset incomplete: " .. result.error)
    end
    Events.EveryTenMinutes.Add(checkSafehousesStayedRemoved)
end

-- Follow-up check, once, ten in-game minutes after a hard reset on this start: by then a mod
-- that rebuilds safehouses from its own data (as Knox Claim did) has run at least once.
checkSafehousesStayedRemoved = function()
    Events.EveryTenMinutes.Remove(checkSafehousesStayedRemoved)
    if not result then return end
    local fields = result
    result = nil
    local okCount, count = pcall(function() return SafeHouse.getSafehouseList():size() end)
    if not okCount then return end
    fields.checkedAt = os.date("!%Y-%m-%dT%H:%M:%SZ")
    fields.safehousesAfterCheck = count
    if count > 0 then
        print(string.format("[FMGResetTool] WARNING: %d safehouses exist 10 in-game minutes after the hard reset. "
            .. "Another mod may rebuild them from its own data, or players claimed new ones.", count))
    else
        print("[FMGResetTool] Hard reset check: no safehouses 10 in-game minutes later.")
    end
    local ok, err = pcall(writeNote, fields)
    if not ok then print("[FMGResetTool] ERROR: " .. tostring(err)) end
end

Events.OnServerStarted.Add(runPendingHardReset)
