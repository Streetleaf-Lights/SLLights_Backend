-- Drops 20 PoleTelemetry columns that are stored but never read back by
-- any business logic -- vitals, fault flags, API responses, or remote
-- control. Values for these fields continue to be captured in
-- ExtraFieldsJson for archival purposes.
--
-- Columns kept (genuinely consumed):
--   BatteryVoltage1/2          -- API response (batteryVoltage1/2)
--   BatteryElecCurrent1/2      -- Leadsun vitals formula
--   LampPower1/2               -- Leadsun vitals formula
--   SolarBoardVoltage/Current  -- Leadsun vitals formula (PanelPercentage)
--   ControllerCode             -- remote control device lookup
--   ProductId                  -- API response, remote control
--   UserName                   -- remote control
--   LeadsunId                  -- remote control (device UID)
--   GroupId                    -- remote control
--   GroupName                  -- updateLeadsunProjectDetails aggregation
--   GatewayCode                -- remote control
--   LeadsunProjectId           -- remote control, projects_loader
--   LeadsunProjectName         -- updateLeadsunProjectDetails aggregation
--   ModelId                    -- vitals formula (PoleModels join)
--   IsOnline/IsOpenIssueFault  -- fault flags
--   IsDaylight/IsDaylightFor*  -- fault flag conditions
--   Longitude/Latitude         -- provisioned pole coordinate updates
--   ExtraFieldsJson            -- catch-all storage
--   BatterySoC/LightRatio/PanelPercentage -- provisioned vitals
--   BatteryFault/LEDFault/ControllerFault -- provisioned fault flags

ALTER TABLE dbo.PoleTelemetry DROP COLUMN Battery1State;
ALTER TABLE dbo.PoleTelemetry DROP COLUMN Battery2State;
ALTER TABLE dbo.PoleTelemetry DROP COLUMN BatteryOutElecCurrent;
ALTER TABLE dbo.PoleTelemetry DROP COLUMN BatteryTemperature1;
ALTER TABLE dbo.PoleTelemetry DROP COLUMN BatteryTemperature2;
ALTER TABLE dbo.PoleTelemetry DROP COLUMN ControlModelCode;
ALTER TABLE dbo.PoleTelemetry DROP COLUMN ControlModelName;
ALTER TABLE dbo.PoleTelemetry DROP COLUMN CreateTime;
ALTER TABLE dbo.PoleTelemetry DROP COLUMN DcInState;
ALTER TABLE dbo.PoleTelemetry DROP COLUMN DcInVoltage;
ALTER TABLE dbo.PoleTelemetry DROP COLUMN DcOutState;
ALTER TABLE dbo.PoleTelemetry DROP COLUMN EnvTemperature;
ALTER TABLE dbo.PoleTelemetry DROP COLUMN Lamp1State;
ALTER TABLE dbo.PoleTelemetry DROP COLUMN Lamp2State;
ALTER TABLE dbo.PoleTelemetry DROP COLUMN LampBatteryStatus;
ALTER TABLE dbo.PoleTelemetry DROP COLUMN LightingState;
ALTER TABLE dbo.PoleTelemetry DROP COLUMN McuTemperature;
ALTER TABLE dbo.PoleTelemetry DROP COLUMN SolarBoardDcStatus;
ALTER TABLE dbo.PoleTelemetry DROP COLUMN SolarBoardState;
ALTER TABLE dbo.PoleTelemetry DROP COLUMN TimeoutFlag;
