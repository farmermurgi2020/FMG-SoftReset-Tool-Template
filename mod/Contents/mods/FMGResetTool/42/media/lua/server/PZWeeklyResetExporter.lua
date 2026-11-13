if not isServer() then return end

-- Keep the legacy command channel so older clients can communicate during the Workshop ID change.
local MODULE = "PZWeeklyResetExporter"
-- Keep the save key so existing protected zones and settings remain available.
local DATA_KEY = "PZWeeklyResetExporter"
local OUTPUT_FILE = "pz-reset-zones.json"
local EXPORT_INTERVAL_SECONDS = 300
local CHUNK_SIZE = 8
local MAX_ZONES = 500
local nextExport = 0
local nextZoneId = 0
local data

local function escapeJson(value)
    local text = tostring(value or "")
    text = text:gsub("\\", "\\\\")
    text = text:gsub('"', '\\"')
    text = text:gsub("\r", "\\r")
    text = text:gsub("\n", "\\n")
    text = text:gsub("\t", "\\t")
    text = text:gsub("[%z\1-\8\11\12\14-\31]", "")
    return text
end

local function jsonString(value)
    return '"' .. escapeJson(value) .. '"'
end

local function collectionSize(collection, label)
    if collection == nil then error(label .. " collection is unavailable") end
    if type(collection.size) == "function" then return collection:size() end
    if type(collection) == "table" then return #collection end
    error(label .. " collection has no supported size method")
end

local function collectionItem(collection, index)
    if type(collection.get) == "function" then return collection:get(index) end
    return collection[index + 1]
end

local function callFirst(object, names, label)
    for _, name in ipairs(names) do
        local method = object and object[name]
        if type(method) == "function" then
            local ok, value = pcall(method, object)
            if not ok then error(label .. "." .. name .. " failed: " .. tostring(value)) end
            return value
        end
    end
    error(label .. " does not expose " .. table.concat(names, " or "))
end

local function formatZone(kind, id, name, x, y, width, height)
    if type(x) ~= "number" or type(y) ~= "number"
        or type(width) ~= "number" or type(height) ~= "number"
        or width <= 0 or height <= 0 then
        error(kind .. " has invalid bounds")
    end
    return string.format(
        '{"id":%s,"name":%s,"x":%d,"y":%d,"w":%d,"h":%d,"source":%s}',
        jsonString(id),
        jsonString(name),
        math.floor(x),
        math.floor(y),
        math.floor(width),
        math.floor(height),
        jsonString(kind)
    )
end

local function readSafehouses()
    local result = {}
    local safehouses = SafeHouse.getSafehouseList()
    for index = 0, collectionSize(safehouses, "safehouse") - 1 do
        local safehouse = collectionItem(safehouses, index)
        if not safehouse then error("safehouse collection returned a missing item") end
        result[#result + 1] = formatZone(
            "safehouse",
            "safehouse-" .. tostring(index + 1),
            callFirst(safehouse, { "getTitle", "getName" }, "safehouse"),
            callFirst(safehouse, { "getX" }, "safehouse"),
            callFirst(safehouse, { "getY" }, "safehouse"),
            callFirst(safehouse, { "getW", "getWidth" }, "safehouse"),
            callFirst(safehouse, { "getH", "getHeight" }, "safehouse")
        )
    end
    return result
end

local function readManualZones()
    local result = {}
    for _, zone in ipairs(data.protectedZones or {}) do
        result[#result + 1] = formatZone(
            "tool",
            zone.id,
            zone.name,
            zone.x,
            zone.y,
            zone.w,
            zone.h
        )
    end
    return result
end

local function jsonObject(tableValue)
    local keys = {}
    for key in pairs(tableValue) do keys[#keys + 1] = key end
    table.sort(keys)
    local fields = {}
    for _, key in ipairs(keys) do
        local value = tableValue[key]
        local encoded
        if type(value) == "string" then
            encoded = jsonString(value)
        elseif type(value) == "boolean" then
            encoded = value and "true" or "false"
        elseif type(value) == "number" then
            encoded = tostring(value)
        else
            error("unsupported JSON value for " .. tostring(key))
        end
        fields[#fields + 1] = jsonString(key) .. ":" .. encoded
    end
    return "{" .. table.concat(fields, ",") .. "}"
end

local function buildExport()
    local zones = readSafehouses()
    for _, zone in ipairs(readManualZones()) do zones[#zones + 1] = zone end
    local core = getCore()
    if not core then error("game core is unavailable") end
    local build = callFirst(core, { "getVersion" }, "core")
    local settings = data.settings
    return string.format(
        '{"schemaVersion":2,"build":%s,"executionMode":%s,"generatedAt":%s,"settingsSavedAt":%s,"zones":[%s],"resetModes":%s,"resetTargets":%s,"schedule":%s,"safety":%s}',
        jsonString(build),
        jsonString(data.settings.executionMode or "local"),
        jsonString(os.date("!%Y-%m-%dT%H:%M:%SZ")),
        jsonString(data.settings.savedAt or ""),
        table.concat(zones, ","),
        jsonObject(settings.resetModes),
        jsonObject(settings.resetTargets),
        jsonObject(settings.schedule),
        jsonObject(settings.safety)
    )
end

local function writeExport()
    local payload = buildExport()
    local writer = getFileWriter(OUTPUT_FILE, true, false)
    if not writer then error("could not open output file " .. OUTPUT_FILE) end
    local ok, err = pcall(function()
        writer:write(payload)
        writer:close()
    end)
    if not ok then
        pcall(function() writer:close() end)
        error("could not write " .. OUTPUT_FILE .. ": " .. tostring(err))
    end
    nextExport = os.time() + EXPORT_INTERVAL_SECONDS
    print("[FMGResetTool] Updated " .. OUTPUT_FILE)
end

local function exportWithLog()
    local ok, err = pcall(writeExport)
    if not ok then
        nextExport = os.time() + EXPORT_INTERVAL_SECONDS
        print("[FMGResetTool] ERROR: export failed: " .. tostring(err))
    end
end

-- Per-target reset modes: protect = reset but keep protected zones, reset_all = ignore zones,
-- skip = don't touch. resetTargets (on/off) is kept alongside for older worker versions.
local RESET_MODE_VALUES = { protect = true, reset_all = true, skip = true }
local RESET_TARGET_KEYS = { "mapChunks", "zombiePopulation", "animalPopulation" }

local function targetsFromModes(modes)
    local targets = {}
    for _, key in ipairs(RESET_TARGET_KEYS) do targets[key] = modes[key] ~= "skip" end
    return targets
end

local function defaultSettings()
    return {
        executionMode = "local",
        resetModes = {
            mapChunks = "protect",
            zombiePopulation = "skip",
            animalPopulation = "skip"
        },
        resetTargets = {
            mapChunks = true,
            zombiePopulation = false,
            animalPopulation = false
        },
        schedule = {
            enabled = false,
            frequency = "weekly",
            anchor_date = os.date("%Y-%m-%d"),
            time = "03:00",
            timezone = "UTC",
            pine_stop_time = "02:30",
            pine_start_time = "06:00",
            minimum_start_buffer_minutes = 30,
            allowed_lateness_minutes = 10
        },
        safety = {
            apply_enabled = false,
            approved_build = "",
            staging_and_rollback_tested = false,
            max_delete_fraction = 0.9,
            file_stability_seconds = 120
        }
    }
end

local function initialize()
    data = ModData.getOrCreate(DATA_KEY)
    data.schemaVersion = 2
    if type(data.protectedZones) ~= "table" then data.protectedZones = {} end
    if type(data.settings) ~= "table" then data.settings = defaultSettings() end
    if data.settings.executionMode ~= "local" and data.settings.executionMode ~= "dedicated" then
        data.settings.executionMode = "local"
    end
    if type(data.settings.resetModes) ~= "table" and type(data.settings.resetTargets) == "table" then
        -- Saves from before reset modes: checked = keep protected zones, unchecked = don't touch.
        local modes = {}
        for _, key in ipairs(RESET_TARGET_KEYS) do
            modes[key] = data.settings.resetTargets[key] == true and "protect" or "skip"
        end
        data.settings.resetModes = modes
    end
    local defaults = defaultSettings()
    for section, values in pairs(defaults) do
        if type(values) == "table" then
            if type(data.settings[section]) ~= "table" then data.settings[section] = {} end
            for key, value in pairs(values) do
                if data.settings[section][key] == nil then
                    data.settings[section][key] = value
                end
            end
        end
    end
    for _, key in ipairs(RESET_TARGET_KEYS) do
        if not RESET_MODE_VALUES[data.settings.resetModes[key]] then data.settings.resetModes[key] = "skip" end
    end
    data.settings.resetTargets = targetsFromModes(data.settings.resetModes)
end

local function sendState(player, message)
    sendServerCommand(player, MODULE, "state", {
        data = data,
        error = message
    })
end

local function isAdmin(player)
    return player and player:getAccessLevel() == "admin"
end

local function validInteger(value, minimum, maximum)
    return type(value) == "number" and value == math.floor(value)
        and value >= minimum and value <= maximum
end

local function sanitizeName(value)
    if type(value) ~= "string" then return nil end
    local name = value:gsub("[%z\1-\31]", ""):match("^%s*(.-)%s*$")
    if #name == 0 or #name > 80 then return nil end
    return name
end

local function addZone(player, args)
    if #data.protectedZones >= MAX_ZONES then
        sendState(player, "Protected-area limit reached.")
        return
    end
    if type(args) ~= "table"
        or not validInteger(args.x, -100000, 100000)
        or not validInteger(args.y, -100000, 100000)
        or not validInteger(args.w, 1, 200000)
        or not validInteger(args.h, 1, 200000) then
        sendState(player, "Invalid protected-area bounds.")
        return
    end
    local name = sanitizeName(args.name) or ("Protected area " .. tostring(#data.protectedZones + 1))
    local x = math.floor(args.x / CHUNK_SIZE) * CHUNK_SIZE
    local y = math.floor(args.y / CHUNK_SIZE) * CHUNK_SIZE
    local right = math.ceil((args.x + args.w) / CHUNK_SIZE) * CHUNK_SIZE
    local bottom = math.ceil((args.y + args.h) / CHUNK_SIZE) * CHUNK_SIZE
    nextZoneId = nextZoneId + 1
    data.protectedZones[#data.protectedZones + 1] = {
        id = string.format("tool-%d-%d", os.time(), nextZoneId),
        name = name,
        x = x,
        y = y,
        w = right - x,
        h = bottom - y
    }
    ModData.transmit(DATA_KEY)
    exportWithLog()
    sendState(player)
end

local function removeZone(player, args)
    if type(args) ~= "table" or type(args.id) ~= "string" then
        sendState(player, "Invalid protected-area identifier.")
        return
    end
    for index, zone in ipairs(data.protectedZones) do
        if zone.id == args.id then
            table.remove(data.protectedZones, index)
            ModData.transmit(DATA_KEY)
            exportWithLog()
            sendState(player)
            return
        end
    end
    sendState(player, "Protected area no longer exists.")
end

local function renameZone(player, args)
    if type(args) ~= "table" or type(args.id) ~= "string" then
        sendState(player, "Invalid protected-area identifier.")
        return
    end
    local name = sanitizeName(args.name)
    if not name then
        sendState(player, "Enter a valid protected-area name (1-80 characters).")
        return
    end
    for _, zone in ipairs(data.protectedZones) do
        if zone.id == args.id then
            zone.name = name
            ModData.transmit(DATA_KEY)
            exportWithLog()
            sendState(player)
            return
        end
    end
    sendState(player, "Protected area no longer exists.")
end

local function validTime(value)
    if type(value) ~= "string" then return false end
    local hour, minute = value:match("^(%d%d):(%d%d)$")
    return hour ~= nil and tonumber(hour) < 24 and tonumber(minute) < 60
end

local function validDate(value)
    if type(value) ~= "string" then return false end
    local year, month, day = value:match("^(%d%d%d%d)%-(%d%d)%-(%d%d)$")
    if not year then return false end
    year, month, day = tonumber(year), tonumber(month), tonumber(day)
    if year < 1 then return false end
    if month < 1 or month > 12 then return false end
    local days = { 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31 }
    if year % 400 == 0 or (year % 4 == 0 and year % 100 ~= 0) then days[2] = 29 end
    return day >= 1 and day <= days[month]
end

local function saveSettings(player, args)
    local value = args and args.settings
    if type(value) ~= "table" or type(value.schedule) ~= "table"
        or type(value.safety) ~= "table" or type(value.resetModes) ~= "table" then
        sendState(player, "Invalid settings.")
        return
    end
    local schedule, safety = value.schedule, value.safety
    local resetModes = {}
    local anyReset = false
    for _, key in ipairs(RESET_TARGET_KEYS) do
        local mode = value.resetModes[key]
        if not RESET_MODE_VALUES[mode] then
            sendState(player, "Invalid reset mode for " .. key .. ".")
            return
        end
        resetModes[key] = mode
        if mode ~= "skip" then anyReset = true end
    end
    if not anyReset then
        sendState(player, "Every reset target is set to Don't touch; choose what to reset.")
        return
    end
    local executionMode = value.executionMode or "local"
    local frequency = schedule.frequency
    local fraction = tonumber(safety.max_delete_fraction)
    local buffer = tonumber(schedule.minimum_start_buffer_minutes)
    if schedule.enabled and executionMode ~= "local" then
        sendState(player, "Scheduled resets are currently available only for Local hosted mode.")
        return
    end
    if safety.apply_enabled then
        sendState(player, "Scheduled apply is locked off; local schedules can run dry-run plans only.")
        return
    end
    if (executionMode ~= "local" and executionMode ~= "dedicated")
        or type(schedule.enabled) ~= "boolean"
        or (frequency ~= "daily" and frequency ~= "weekly" and frequency ~= "monthly")
        or not validDate(schedule.anchor_date)
        or not validTime(schedule.time)
        or not validTime(schedule.pine_stop_time)
        or not validTime(schedule.pine_start_time)
        or type(schedule.timezone) ~= "string" or #schedule.timezone > 64
        or not buffer or buffer ~= math.floor(buffer) or buffer < 0 or buffer > 1440
        or type(safety.apply_enabled) ~= "boolean"
        or type(safety.staging_and_rollback_tested) ~= "boolean"
        or type(safety.approved_build) ~= "string" or #safety.approved_build > 100
        or not fraction or fraction < 0 or fraction >= 1 then
        sendState(player, "One or more schedule or safety fields are invalid.")
        return
    end
    local core = getCore()
    local currentBuild = callFirst(core, { "getVersion" }, "core")
    if safety.apply_enabled
        and (not safety.staging_and_rollback_tested or safety.approved_build ~= currentBuild) then
        sendState(player, "Apply requires a passing rollback test and exact current build approval.")
        return
    end
    data.settings.executionMode = executionMode
    data.settings.resetModes = resetModes
    data.settings.resetTargets = targetsFromModes(resetModes)
    -- Lets the local dashboard tell which settings were saved last.
    data.settings.savedAt = os.date("!%Y-%m-%dT%H:%M:%SZ")
    data.settings.schedule = {
        enabled = schedule.enabled,
        frequency = frequency,
        anchor_date = schedule.anchor_date,
        time = schedule.time,
        timezone = schedule.timezone,
        pine_stop_time = schedule.pine_stop_time,
        pine_start_time = schedule.pine_start_time,
        minimum_start_buffer_minutes = buffer,
        allowed_lateness_minutes = 10
    }
    data.settings.safety = {
        apply_enabled = safety.apply_enabled,
        approved_build = safety.approved_build,
        staging_and_rollback_tested = safety.staging_and_rollback_tested,
        max_delete_fraction = fraction,
        file_stability_seconds = 120
    }
    ModData.transmit(DATA_KEY)
    exportWithLog()
    sendState(player)
end

local function onClientCommand(module, command, player, args)
    if module ~= MODULE then return end
    if not isAdmin(player) then
        print("[FMGResetTool] Rejected non-admin client command.")
        return
    end
    if command == "request" then
        sendState(player)
    elseif command == "addZone" then
        addZone(player, args)
    elseif command == "removeZone" then
        removeZone(player, args)
    elseif command == "renameZone" then
        renameZone(player, args)
    elseif command == "saveSettings" then
        saveSettings(player, args)
    end
end

Events.OnInitGlobalModData.Add(initialize)
Events.OnServerStarted.Add(function()
    initialize()
    exportWithLog()
end)
Events.OnClientCommand.Add(onClientCommand)
Events.OnTick.Add(function()
    if isServer() and os.time() >= nextExport then exportWithLog() end
end)
