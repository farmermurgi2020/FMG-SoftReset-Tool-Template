require "ISUI/ISCollapsableWindow"
require "ISUI/ISPanel"
require "ISUI/ISTextEntryBox"
require "ISUI/ISTickBox"
require "ISUI/ISButton"
require "ISUI/ISComboBox"
require "ISUI/ISScrollingListBox"

PZWeeklyResetAdminSettings = ISCollapsableWindow:derive("PZWeeklyResetAdminSettings")
PZWeeklyResetAdminSettings.instance = nil

-- Keep the legacy command channel aligned with the server during the Workshop ID change.
local MODULE = "PZWeeklyResetExporter"
local CHUNK_SIZE = 8
local CELL_SIZE = 256
local BLUE = { 0.46, 0.78, 1.0 }
local GREEN = { 0.55, 0.90, 0.60 }
local ROW_HEIGHT = 38
-- Per-target reset modes; dropdown option N maps to RESET_MODES[N].
local RESET_MODES = { "protect", "reset_all", "skip" }
local RESET_MODE_LABELS = { "Reset, keep protected zones", "Reset everything (ignore zones)", "Don't touch" }
local RESET_TARGETS = {
    { key = "mapChunks", label = "Map chunks" },
    { key = "zombiePopulation", label = "Zombie population (zpop)" },
    { key = "animalPopulation", label = "Animal population (apop)" }
}
local TARGET_ROW_Y = 52
local TARGET_ROW_HEIGHT = 26
local SETTINGS_Y = 182
local fields = {
    { key = "frequency", label = "Recurrence (daily/weekly/monthly)", section = "schedule" },
    { key = "anchor_date", label = "Start date (YYYY-MM-DD)", section = "schedule" },
    { key = "time", label = "Reset time (HH:MM)", section = "schedule" },
    { key = "timezone", label = "Timezone (IANA)", section = "schedule" },
    { key = "pine_stop_time", label = "Server stop time (HH:MM)", section = "schedule" },
    { key = "pine_start_time", label = "Server start time (HH:MM)", section = "schedule" },
    { key = "minimum_start_buffer_minutes", label = "Start buffer (minutes)", section = "schedule" },
    { key = "approved_build", label = "Approved Build string", section = "safety" },
    { key = "max_delete_fraction", label = "Maximum fraction reset (0-0.999)", section = "safety" }
}

local function zoneCounts(zone)
    local width = math.max(1, tonumber(zone.w) or 1)
    local height = math.max(1, tonumber(zone.h) or 1)
    local chunks = math.ceil(width / CHUNK_SIZE) * math.ceil(height / CHUNK_SIZE)
    local x, y = tonumber(zone.x) or 0, tonumber(zone.y) or 0
    local cellsWide = math.floor((x + width - 1) / CELL_SIZE) - math.floor(x / CELL_SIZE) + 1
    local cellsHigh = math.floor((y + height - 1) / CELL_SIZE) - math.floor(y / CELL_SIZE) + 1
    return chunks, cellsWide * cellsHigh
end

function PZWeeklyResetAdminSettings.open(data)
    if not isClient() or getAccessLevel() ~= "admin" then return end
    if not PZWeeklyResetAdminSettings.instance then
        local width, height = 760, 680
        local x = (getCore():getScreenWidth() - width) / 2
        local y = (getCore():getScreenHeight() - height) / 2
        local window = PZWeeklyResetAdminSettings:new(x, y, width, height)
        window:initialise()
        window:instantiate()
        window:addToUIManager()
        PZWeeklyResetAdminSettings.instance = window
    end
    local window = PZWeeklyResetAdminSettings.instance
    window:setServerData(data)
    window.isToolActive = true
    window:setVisible(true)
    window:bringToTop()
end

function PZWeeklyResetAdminSettings:new(x, y, width, height)
    local window = ISCollapsableWindow.new(self, x, y, width, height)
    window.title = "FMG-SoftReset-Tool"
    window.resizable = false
    window.collapsable = true
    window.settingsData = nil
    window.entries = {}
    window.activeTab = "zones"
    window.zones = {}
    return window
end

function PZWeeklyResetAdminSettings:createChildren()
    ISCollapsableWindow.createChildren(self)
    local contentY = 74
    self.zonesTabButton = ISButton:new(12, 40, 190, 28, "Protected Zones", self, PZWeeklyResetAdminSettings.onZonesTab)
    self.zonesTabButton:initialise()
    self:addChild(self.zonesTabButton)
    self.settingsTabButton = ISButton:new(206, 40, 190, 28, "Soft Reset Settings", self, PZWeeklyResetAdminSettings.onSettingsTab)
    self.settingsTabButton:initialise()
    self:addChild(self.settingsTabButton)

    self.zonesPanel = ISPanel:new(12, contentY, self.width - 24, self.height - contentY - 48)
    self.zonesPanel:initialise()
    self:addChild(self.zonesPanel)
    self.zonesPanel.render = function(panel)
        ISPanel.render(panel)
        panel:drawText("Safehouses are protected automatically. Drag on the map to add a tool zone.", 2, 5, 0.82, 0.88, 0.92, 1, UIFont.Small)
        panel:drawText("Light green: safehouse  |  Light blue: tool zone", 2, 20, 0.82, 0.88, 0.92, 1, UIFont.Small)
        panel:drawText("New:", 2, panel.height - 82, 1, 1, 1, 1, UIFont.Small)
        panel:drawText("Rename:", 2, panel.height - 42, 1, 1, 1, 1, UIFont.Small)
    end
    self.zonesList = ISScrollingListBox:new(0, 34, self.zonesPanel.width, self.zonesPanel.height - 136)
    self.zonesList:initialise()
    self.zonesList.itemheight = ROW_HEIGHT
    self.zonesList.doDrawItem = PZWeeklyResetAdminSettings.drawZoneRow
    self.zonesList:setOnMouseDownFunction(self, PZWeeklyResetAdminSettings.onZoneSelected)
    self.zonesPanel:addChild(self.zonesList)
    self.zoneName = ISTextEntryBox:new("", 50, self.zonesPanel.height - 88, 230, 26)
    self.zoneName:initialise()
    self.zonesPanel:addChild(self.zoneName)
    self.addZoneButton = ISButton:new(292, self.zonesPanel.height - 88, 190, 26, "Add zone on map...", self, PZWeeklyResetAdminSettings.onStartZone)
    self.addZoneButton:initialise()
    self.zonesPanel:addChild(self.addZoneButton)
    self.removeZoneButton = ISButton:new(492, self.zonesPanel.height - 88, 226, 26, "Remove selected", self, PZWeeklyResetAdminSettings.onRemoveZone)
    self.removeZoneButton:initialise()
    self.zonesPanel:addChild(self.removeZoneButton)
    self.renameZoneName = ISTextEntryBox:new("", 68, self.zonesPanel.height - 48, 230, 26)
    self.renameZoneName:initialise()
    self.zonesPanel:addChild(self.renameZoneName)
    self.renameZoneButton = ISButton:new(308, self.zonesPanel.height - 48, 190, 26, "Save zone name", self, PZWeeklyResetAdminSettings.onRenameZone)
    self.renameZoneButton:initialise()
    self.renameZoneButton:setEnable(false)
    self.zonesPanel:addChild(self.renameZoneButton)

    self.settingsPanel = ISPanel:new(12, contentY, self.width - 24, self.height - contentY - 48)
    self.settingsPanel:initialise()
    self:addChild(self.settingsPanel)
    self.settingsPanel.render = function(panel)
        ISPanel.render(panel)
        panel:drawText("Hosting mode:", 0, 5, 1, 1, 1, 1, UIFont.Small)
        panel:drawText("What a reset does to each kind of data:", 0, 34, 1, 1, 1, 1, UIFont.Small)
        for index, target in ipairs(RESET_TARGETS) do
            panel:drawText(target.label, 0, TARGET_ROW_Y + (index - 1) * TARGET_ROW_HEIGHT + 4, 1, 1, 1, 1, UIFont.Small)
        end
        panel:drawText("Keep protected zones keeps whole 256-tile population cells; existing livestock may persist.", 0, 132, 0.82, 0.88, 0.92, 1, UIFont.Small)
        local y = SETTINGS_Y
        for _, field in ipairs(fields) do
            panel:drawText(field.label, 0, y + 5, 1, 1, 1, 1, UIFont.Small)
            y = y + 28
        end
        panel:drawText("Local scheduling uses a separate Windows worker while the hosted game is stopped.", 0, y + 64, 1, 0.78, 0.45, 1, UIFont.Small)
        panel:drawText("Dedicated (Pine Hosting) runs and schedules are set in the dedicated dashboard.", 0, y + 82, 1, 0.78, 0.45, 1, UIFont.Small)
    end
    self.executionMode = "local"
    self.executionModeCombo = ISComboBox:new(110, 0, 260, 28, self, PZWeeklyResetAdminSettings.onExecutionModeChanged)
    self.executionModeCombo:initialise()
    self.executionModeCombo:addOption("Local hosted")
    self.executionModeCombo:addOption("Dedicated hosted")
    self.executionModeCombo.selected = 1
    self.settingsPanel:addChild(self.executionModeCombo)
    self.resetModeCombos = {}
    for index, target in ipairs(RESET_TARGETS) do
        local combo = ISComboBox:new(230, TARGET_ROW_Y + (index - 1) * TARGET_ROW_HEIGHT, 280, 24, self, nil)
        combo:initialise()
        for _, label in ipairs(RESET_MODE_LABELS) do combo:addOption(label) end
        combo.selected = 3
        self.settingsPanel:addChild(combo)
        self.resetModeCombos[target.key] = combo
    end
    self.scheduleEnabled = ISTickBox:new(0, 152, 300, 24, "", self, nil)
    self.scheduleEnabled:initialise()
    self.scheduleEnabled:addOption("Enable scheduled reset")
    self.scheduleEnabled.enable = true
    self.settingsPanel:addChild(self.scheduleEnabled)
    self.entries = {}
    local y = SETTINGS_Y
    for _, field in ipairs(fields) do
        local entry = ISTextEntryBox:new("", 330, y, 360, 24)
        entry:initialise()
        self.settingsPanel:addChild(entry)
        self.entries[field.key] = entry
        y = y + 28
    end
    self.applyEnabled = ISTickBox:new(0, y + 2, 430, 30, "", self, nil)
    self.applyEnabled:initialise()
    self.applyEnabled:addOption("Allow reset apply")
    self.applyEnabled.enable = false
    self.settingsPanel:addChild(self.applyEnabled)
    self.stagingTested = ISTickBox:new(0, y + 34, 430, 30, "", self, nil)
    self.stagingTested:initialise()
    self.stagingTested:addOption("Staging and rollback tested")
    self.stagingTested.enable = false
    self.settingsPanel:addChild(self.stagingTested)
    self.saveButton = ISButton:new(self.width - 178, self.height - 40, 160, 26, "Save settings", self, PZWeeklyResetAdminSettings.onSave)
    self.saveButton:initialise()
    self:addChild(self.saveButton)
    self:setTab("zones")
end

function PZWeeklyResetAdminSettings:render()
    ISCollapsableWindow.render(self)
end

function PZWeeklyResetAdminSettings.drawZoneRow(list, y, item, alt)
    local zone = item and item.item
    if not zone then return y + list.itemheight end
    local color = zone.source == "safehouse" and GREEN or BLUE
    list:drawRect(0, y, list:getWidth(), list.itemheight, 0.18, color[1], color[2], color[3])
    list:drawRectBorder(0, y, list:getWidth(), list.itemheight, 0.55, color[1], color[2], color[3])
    if list.selected == item.index then
        list:drawRect(0, y, list:getWidth(), list.itemheight, 0.20, 0.02, 0.05, 0.10)
        list:drawRectBorder(1, y + 1, list:getWidth() - 2, list.itemheight - 2, 1.0, 0.05, 0.10, 0.18)
        list:drawRectBorder(2, y + 2, list:getWidth() - 4, list.itemheight - 4, 0.9, 0.02, 0.03, 0.08)
    end
    local chunks, cells = zoneCounts(zone)
    local label = string.format("%s  |  %d chunks  |  %d cells", item.text, chunks, cells)
    list:drawText(label, 8, y + 10, color[1], color[2], color[3], 1, UIFont.Small)
    return y + list.itemheight
end

function PZWeeklyResetAdminSettings:setTab(tab)
    self.activeTab = tab
    self.zonesPanel:setVisible(tab == "zones")
    self.settingsPanel:setVisible(tab == "settings")
    self.zonesTabButton:setEnable(tab ~= "zones")
    self.settingsTabButton:setEnable(tab ~= "settings")
end

function PZWeeklyResetAdminSettings:onZonesTab()
    self:setTab("zones")
end

function PZWeeklyResetAdminSettings:onSettingsTab()
    self:setTab("settings")
end

function PZWeeklyResetAdminSettings:setServerData(data)
    local selected = self.zonesList.items[self.zonesList.selected]
    local selectedId = selected and selected.item and selected.item.source == "tool" and selected.item.id or nil
    self.settingsData = data
    if not data or not data.settings then return end
    self.zonesList:clear()
    self.zones = {}
    if SafeHouse and SafeHouse.getSafehouseList then
        local safehouses = SafeHouse.getSafehouseList()
        local count = type(safehouses.size) == "function" and safehouses:size() or #safehouses
        for index = 0, count - 1 do
            local safehouse = type(safehouses.get) == "function" and safehouses:get(index) or safehouses[index + 1]
            if safehouse then
                local name = type(safehouse.getTitle) == "function" and safehouse:getTitle()
                    or (type(safehouse.getName) == "function" and safehouse:getName())
                    or ("Safehouse " .. tostring(index + 1))
                local zone = {
                    source = "safehouse",
                    name = name,
                    x = safehouse:getX(), y = safehouse:getY(), w = safehouse:getW(), h = safehouse:getH()
                }
                self.zonesList:addItem("Safehouse: " .. tostring(zone.name), zone)
                self.zones[#self.zones + 1] = zone
            end
        end
    end
    for _, zone in ipairs(data.protectedZones or {}) do
        zone.source = "tool"
        self.zonesList:addItem("Tool zone: " .. tostring(zone.name), zone)
        self.zones[#self.zones + 1] = zone
    end
    self.zonesList.selected = -1
    self.renameZoneName:setText("")
    self.renameZoneButton:setEnable(false)
    if selectedId then
        for index, item in ipairs(self.zonesList.items) do
            if item.item and item.item.id == selectedId then
                self.zonesList.selected = index
                self:onZoneSelected(item.item)
                break
            end
        end
    end
    local settings = data.settings
    self.executionMode = settings.executionMode or "local"
    self.executionModeCombo.selected = self.executionMode == "dedicated" and 2 or 1
    local resetModes = settings.resetModes or {}
    local resetTargets = settings.resetTargets or {}
    for _, target in ipairs(RESET_TARGETS) do
        local mode = resetModes[target.key]
        if mode == nil then
            -- Older saves only have on/off targets: on = keep protected zones, off = don't touch.
            mode = resetTargets[target.key] and "protect" or "skip"
        end
        local modeIndex = 3
        for index, value in ipairs(RESET_MODES) do
            if value == mode then modeIndex = index end
        end
        self.resetModeCombos[target.key].selected = modeIndex
    end
    self.scheduleEnabled:setSelected(1, self.executionMode == "local" and settings.schedule.enabled == true)
    self.scheduleEnabled.enable = self.executionMode == "local"
    self.applyEnabled:setSelected(1, settings.safety.apply_enabled == true)
    self.applyEnabled.enable = settings.safety.apply_enabled == true
    self.stagingTested:setSelected(1, settings.safety.staging_and_rollback_tested == true)
    for _, field in ipairs(fields) do
        local value = settings[field.section][field.key]
        self.entries[field.key]:setText(tostring(value or ""))
    end
end

function PZWeeklyResetAdminSettings:onExecutionModeChanged(combo)
    self.executionMode = combo.selected == 2 and "dedicated" or "local"
    self.scheduleEnabled.enable = self.executionMode == "local"
    if self.executionMode ~= "local" then
        self.scheduleEnabled:setSelected(1, false)
    end
end

function PZWeeklyResetAdminSettings:onStartZone()
    self.zoneNameValue = self.zoneName:getText():match("^%s*(.-)%s*$")
    if self.zoneNameValue == "" then
        HaloTextHelper.addText(getPlayer(), "Enter a name for this protected zone first.")
        return
    end
    if PZWeeklyResetMap then PZWeeklyResetMap.startZoneSelection() end
end

function PZWeeklyResetAdminSettings:onZoneSelected(zone)
    if zone and zone.source == "tool" then
        self.renameZoneName:setText(tostring(zone.name or ""))
        self.renameZoneButton:setEnable(true)
    else
        self.renameZoneName:setText("")
        self.renameZoneButton:setEnable(false)
    end
end

function PZWeeklyResetAdminSettings:onRenameZone()
    local selected = self.zonesList.items[self.zonesList.selected]
    local zone = selected and selected.item
    local name = self.renameZoneName:getText():match("^%s*(.-)%s*$")
    if not zone or zone.source ~= "tool" then
        HaloTextHelper.addText(getPlayer(), "Select a tool-created zone to rename.")
        return
    end
    if name == "" then
        HaloTextHelper.addText(getPlayer(), "Enter a name for this protected zone.")
        return
    end
    if PZWeeklyResetMap then PZWeeklyResetMap.renameZone(zone, name) end
end

function PZWeeklyResetAdminSettings:close()
    if PZWeeklyResetMap then PZWeeklyResetMap.cancelZoneSelection(false) end
    self.isToolActive = false
    self:setVisible(false)
end

function PZWeeklyResetAdminSettings:minimizeForZoneSelection()
    self.isToolActive = true
    self.pin = true
    self.collapseButton:setVisible(true)
    self.pinButton:setVisible(false)
    self.isCollapsed = true
    self:setMaxDrawHeight(self:titleBarHeight())
end

function PZWeeklyResetAdminSettings:restoreAfterZoneSelection()
    self.isToolActive = true
    self.isCollapsed = false
    self:clearMaxDrawHeight()
    self.pin = true
    self.collapseButton:setVisible(true)
    self.pinButton:setVisible(false)
    self.collapseButton:bringToTop()
    self:setVisible(true)
    self:bringToTop()
end

function PZWeeklyResetAdminSettings:onRemoveZone()
    local selected = self.zonesList.items[self.zonesList.selected]
    if not selected or not selected.item or selected.item.source ~= "tool" then
        HaloTextHelper.addText(getPlayer(), "Select a tool-created zone to remove.")
        return
    end
    if PZWeeklyResetMap then PZWeeklyResetMap.removeZone(selected.item) end
end

function PZWeeklyResetAdminSettings:onSave()
    self.executionMode = self.executionModeCombo.selected == 2 and "dedicated" or "local"
    local resetModes = {}
    local anyReset = false
    for _, target in ipairs(RESET_TARGETS) do
        local mode = RESET_MODES[self.resetModeCombos[target.key].selected] or "skip"
        resetModes[target.key] = mode
        if mode ~= "skip" then anyReset = true end
    end
    if not anyReset then
        HaloTextHelper.addText(getPlayer(), "Every reset target is set to Don't touch; choose what to reset.")
        return
    end
    local schedule = {
        enabled = self.executionMode == "local" and self.scheduleEnabled:isSelected(1),
        frequency = self.entries.frequency:getText(),
        anchor_date = self.entries.anchor_date:getText(),
        time = self.entries.time:getText(),
        timezone = self.entries.timezone:getText(),
        pine_stop_time = self.entries.pine_stop_time:getText(),
        pine_start_time = self.entries.pine_start_time:getText(),
        minimum_start_buffer_minutes = tonumber(self.entries.minimum_start_buffer_minutes:getText())
    }
    local safety = {
        apply_enabled = false,
        approved_build = self.entries.approved_build:getText(),
        staging_and_rollback_tested = false,
        max_delete_fraction = tonumber(self.entries.max_delete_fraction:getText())
    }
    if not schedule.minimum_start_buffer_minutes or not safety.max_delete_fraction then
        HaloTextHelper.addText(getPlayer(), "Enter valid numbers for buffer and reset fraction.")
        return
    end
    sendClientCommand(getPlayer(), MODULE, "saveSettings", {
        settings = {
            executionMode = self.executionMode or "local",
            resetModes = resetModes,
            schedule = schedule,
            safety = safety
        }
    })
    if schedule.enabled then
        HaloTextHelper.addText(getPlayer(), "Local scheduled dry runs enabled. Start run-local-scheduler.bat on the host PC; apply remains disabled.")
    else
        HaloTextHelper.addText(getPlayer(), "Settings saved. Scheduling and apply are disabled.")
    end
end
