require "ISUI/Maps/ISWorldMap"
require "ISUI/ISContextMenu"
require "PZWeeklyResetAdminSettings"

PZWeeklyResetMap = PZWeeklyResetMap or {}
PZWeeklyResetMap.pendingZoneSelection = false
PZWeeklyResetMap.dragSelecting = false
PZWeeklyResetMap.dragStart = nil
PZWeeklyResetMap.dragCurrent = nil
PZWeeklyResetMap.serverData = nil

-- Keep the legacy command channel aligned with the server during the Workshop ID change.
local MODULE = "PZWeeklyResetExporter"
local previousContextGet = ISContextMenu.get
local previousMapRightClick = ISWorldMap.onRightMouseUp
local previousMapMouseDown = ISWorldMap.onMouseDown
local previousMapMouseMove = ISWorldMap.onMouseMove
local previousMapMouseMoveOutside = ISWorldMap.onMouseMoveOutside
local previousMapMouseUp = ISWorldMap.onMouseUp
local previousMapMouseUpOutside = ISWorldMap.onMouseUpOutside
local previousMapRender = ISWorldMap.render
local activeMapContext = nil

local function isAdmin()
    return isClient() and getAccessLevel() == "admin"
end

local function send(command, args)
    if isClient() then sendClientCommand(getPlayer(), MODULE, command, args or {}) end
end

function PZWeeklyResetMap.requestState()
    if isAdmin() then send("request") end
end

function PZWeeklyResetMap.openTool()
    if not isAdmin() then return end
    PZWeeklyResetAdminSettings.open(PZWeeklyResetMap.serverData)
    if not PZWeeklyResetMap.serverData then PZWeeklyResetMap.requestState() end
end

function PZWeeklyResetMap.onServerCommand(module, command, args)
    if module ~= MODULE or command ~= "state" or type(args) ~= "table" then return end
    PZWeeklyResetMap.serverData = args.data
    if args.error then HaloTextHelper.addText(getPlayer(), tostring(args.error)) end
    if PZWeeklyResetAdminSettings.instance then
        PZWeeklyResetAdminSettings.instance:setServerData(args.data)
    end
end

function PZWeeklyResetMap.startZoneSelection()
    if not isAdmin() then return end
    PZWeeklyResetMap.pendingZoneSelection = true
    PZWeeklyResetMap.dragSelecting = false
    PZWeeklyResetMap.dragStart = nil
    PZWeeklyResetMap.dragCurrent = nil
    local window = PZWeeklyResetAdminSettings.instance
    if window then window:minimizeForZoneSelection() end
    HaloTextHelper.addText(getPlayer(), "Left-click and drag across the map to draw the zone.")
end

function PZWeeklyResetMap.cancelZoneSelection(restoreWindow)
    PZWeeklyResetMap.pendingZoneSelection = false
    PZWeeklyResetMap.dragSelecting = false
    PZWeeklyResetMap.dragStart = nil
    PZWeeklyResetMap.dragCurrent = nil
    local window = PZWeeklyResetAdminSettings.instance
    if restoreWindow ~= false and window and window:getIsVisible() then window:restoreAfterZoneSelection() end
end

function PZWeeklyResetMap.removeZone(zone)
    if zone and zone.id then send("removeZone", { id = zone.id }) end
end

function PZWeeklyResetMap.renameZone(zone, name)
    if zone and zone.id then send("renameZone", { id = zone.id, name = name }) end
end

local function addToolMenu(context)
    context:addOption("FMG-SoftReset-Tool", PZWeeklyResetMap, PZWeeklyResetMap.openTool)
end

ISContextMenu.get = function(player, x, y)
    local context = previousContextGet(player, x, y)
    if activeMapContext and isAdmin() then addToolMenu(context) end
    return context
end

ISWorldMap.onRightMouseUp = function(map, x, y)
    if not isAdmin() then return previousMapRightClick(map, x, y) end
    activeMapContext = { map = map, x = x, y = y }
    local ok, result = pcall(previousMapRightClick, map, x, y)
    activeMapContext = nil
    if not ok then error(result) end
    return result
end

local function mapWorldPoint(map, x, y)
    return {
        x = math.floor(map.mapAPI:uiToWorldX(x, y)),
        y = math.floor(map.mapAPI:uiToWorldY(x, y))
    }
end

local function finishZoneDrag(map, x, y)
    if not PZWeeklyResetMap.dragSelecting then return false end
    local first = PZWeeklyResetMap.dragStart
    local last = mapWorldPoint(map, x, y)
    PZWeeklyResetMap.pendingZoneSelection = false
    PZWeeklyResetMap.dragSelecting = false
    PZWeeklyResetMap.dragStart = nil
    PZWeeklyResetMap.dragCurrent = nil
    local window = PZWeeklyResetAdminSettings.instance
    local name = window and window.zoneNameValue or "Protected area"
    local left, top = math.min(first.x, last.x), math.min(first.y, last.y)
    send("addZone", {
        name = name,
        x = left,
        y = top,
        w = math.abs(first.x - last.x) + 1,
        h = math.abs(first.y - last.y) + 1
    })
    HaloTextHelper.addText(getPlayer(), "Protected area sent to server.")
    if window and window:getIsVisible() then window:restoreAfterZoneSelection() end
    return true
end

ISWorldMap.onMouseDown = function(map, x, y)
    if isAdmin() and PZWeeklyResetMap.pendingZoneSelection then
        PZWeeklyResetMap.dragSelecting = true
        PZWeeklyResetMap.dragStart = mapWorldPoint(map, x, y)
        PZWeeklyResetMap.dragCurrent = PZWeeklyResetMap.dragStart
        return true
    end
    return previousMapMouseDown(map, x, y)
end

ISWorldMap.onMouseMove = function(map, dx, dy)
    if isAdmin() and PZWeeklyResetMap.pendingZoneSelection then
        if PZWeeklyResetMap.dragSelecting then
            PZWeeklyResetMap.dragCurrent = mapWorldPoint(map, map:getMouseX(), map:getMouseY())
        end
        return true
    end
    return previousMapMouseMove(map, dx, dy)
end

ISWorldMap.onMouseMoveOutside = function(map, dx, dy)
    if isAdmin() and PZWeeklyResetMap.pendingZoneSelection then
        if PZWeeklyResetMap.dragSelecting then
            PZWeeklyResetMap.dragCurrent = mapWorldPoint(map, map:getMouseX(), map:getMouseY())
        end
        return true
    end
    return previousMapMouseMoveOutside(map, dx, dy)
end

ISWorldMap.onMouseUp = function(map, x, y)
    if isAdmin() and PZWeeklyResetMap.pendingZoneSelection then
        return finishZoneDrag(map, x, y)
    end
    return previousMapMouseUp(map, x, y)
end

ISWorldMap.onMouseUpOutside = function(map, x, y)
    if isAdmin() and PZWeeklyResetMap.pendingZoneSelection then
        return finishZoneDrag(map, x, y)
    end
    return previousMapMouseUpOutside(map, x, y)
end

local function drawZone(map, zone, color)
    local x1 = map.mapAPI:worldToUIX(zone.x, zone.y)
    local y1 = map.mapAPI:worldToUIY(zone.x, zone.y)
    local x2 = map.mapAPI:worldToUIX(zone.x + zone.w, zone.y)
    local y2 = map.mapAPI:worldToUIY(zone.x + zone.w, zone.y)
    local x3 = map.mapAPI:worldToUIX(zone.x + zone.w, zone.y + zone.h)
    local y3 = map.mapAPI:worldToUIY(zone.x + zone.w, zone.y + zone.h)
    local x4 = map.mapAPI:worldToUIX(zone.x, zone.y + zone.h)
    local y4 = map.mapAPI:worldToUIY(zone.x, zone.y + zone.h)
    map:drawPolygon(nil, x1, y1, x2, y2, x3, y3, x4, y4, color[1], color[2], color[3], 0.24)
    map.javaObject:DrawLine(nil, x1, y1, x2, y2, 2, color[1], color[2], color[3], 0.95)
    map.javaObject:DrawLine(nil, x2, y2, x3, y3, 2, color[1], color[2], color[3], 0.95)
    map.javaObject:DrawLine(nil, x3, y3, x4, y4, 2, color[1], color[2], color[3], 0.95)
    map.javaObject:DrawLine(nil, x4, y4, x1, y1, 2, color[1], color[2], color[3], 0.95)
end

ISWorldMap.render = function(map, ...)
    local result = previousMapRender(map, ...)
    local window = PZWeeklyResetAdminSettings.instance
    if not isAdmin() or not window or not window.isToolActive or not window:getIsVisible() then return result end
    if SafeHouse and SafeHouse.getSafehouseList then
        local safehouses = SafeHouse.getSafehouseList()
        local count = type(safehouses.size) == "function" and safehouses:size() or #safehouses
        for index = 0, count - 1 do
            local safehouse = type(safehouses.get) == "function" and safehouses:get(index) or safehouses[index + 1]
            if safehouse then
                drawZone(map, {
                    x = safehouse:getX(), y = safehouse:getY(),
                    w = safehouse:getW(), h = safehouse:getH()
                }, { 0.55, 0.90, 0.60 })
            end
        end
    end
    local zones = PZWeeklyResetMap.serverData and PZWeeklyResetMap.serverData.protectedZones or {}
    for _, zone in ipairs(zones) do drawZone(map, zone, { 0.46, 0.78, 1.0 }) end
    if PZWeeklyResetMap.dragSelecting and PZWeeklyResetMap.dragStart and PZWeeklyResetMap.dragCurrent then
        local first, last = PZWeeklyResetMap.dragStart, PZWeeklyResetMap.dragCurrent
        drawZone(map, {
            x = math.min(first.x, last.x),
            y = math.min(first.y, last.y),
            w = math.abs(first.x - last.x) + 1,
            h = math.abs(first.y - last.y) + 1
        }, { 1.0, 0.85, 0.25 })
    end
    return result
end

Events.OnServerCommand.Add(PZWeeklyResetMap.onServerCommand)
Events.OnGameStart.Add(PZWeeklyResetMap.requestState)
