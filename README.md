# Thermia Genesis Modbus — local Home Assistant integration

Installable **beta 0.1.22**, designed for a Thermia Calibra E Cool 8 400V BW with Genesis firmware 17.01. Requires **Home Assistant Core 2026.9.0 or newer**; Core 2026.9.4 meets this requirement. The register catalogue follows Thermia’s domestic Genesis 17.1 protocol. Automated checks use simulated devices and Home Assistant API stand-ins, with additional source-contract checks against Core 2026.9.4. This release has not been tested on your physical heat pump or a running Home Assistant instance.

## Dutch user manual

See [HANDLEIDING_NL.pdf](HANDLEIDING_NL.pdf) for the eight-page Dutch control reference, or [HANDLEIDING_NL.txt](HANDLEIDING_NL.txt) for the plain-text edition. It covers switches, thermostats, presets, temperature controls, sensor groups, recovery, external ownership, solar gains and staged EMHASS migration. The manual does not replace validation on the actual pump.

The bundled manual describes 0.1.21 and remains applicable to the controls in 0.1.22. The new installation default in 0.1.22 is explained in [INSTALLATIE_NL.txt](INSTALLATIE_NL.txt): newly created entries already use External Control / EMHASS; existing entries retain their chosen mode.

## Install or update on Home Assistant OS

1. Download and extract `thermia-genesis-modbus-0.1.22.zip`.
2. Use Studio Code Server, Samba or your existing configuration-folder access to copy **`custom_components/thermia_genesis_modbus`** into Home Assistant’s **`/config/custom_components/`**, replacing that folder when updating from 0.1.5 or newer. See the one-time migration below for older versions.
3. Check that `/config/custom_components/thermia_genesis_modbus/manifest.json` exists, without an extra nested folder.
4. Restart **Home Assistant Core**.
5. For a new installation, open **Settings → Devices & services → Add integration → Thermia Genesis Modbus**, then enter the pump’s IP address, port (usually **502**) and unit ID (usually **1**). An existing `thermia_genesis_modbus` entry is retained when updating.
6. Open **Configure**. Page 1 groups the model, external sensors, verified hot-water limits and sensor/room-control settings. Page 2 contains the native heat curve, heating supply limits, cooling-water target and power-control mode. Preset settings and native switches appear once on the device page. Select **Submit** to save deliberate changes.

On the pump, enable Modbus TCP/IP under **Settings → BMS** and connect it to an accessible local network. No separate Modbus YAML hub is needed. Home Assistant pools matching Modbus connections. Avoid overlapping automations that issue competing heat-pump settings.

The public repository is [renaatdb/thermia-genesis-modbus](https://github.com/renaatdb/thermia-genesis-modbus). Test releases are listed under [Releases](https://github.com/renaatdb/thermia-genesis-modbus/releases). For HACS, add this URL as a custom repository of type **Integration**; prereleases may require enabling beta versions. This project is not included in the default HACS catalogue. Installing either reference repository installs that project's integration. Publishing or downloading does not install anything on Home Assistant or migrate existing automations.

## Changes in 0.1.22

- New connection entries are created with **External Control / EMHASS** already selected. The connection probe reads only the operating-status register; it sends no write requests.
- Existing entries retain their chosen mode. The legacy internal fallback is deliberately unchanged, so an update does not silently switch existing control ownership.
- No changes to register addresses, entity identities, external-control behavior, EMHASS automation timing or solar/inertia policy. Added a focused connection-flow regression test and Dutch installation notes.

## Changes in 0.1.21

- Added **Control Ownership** on Configure page 1: **Integration Thermostats And Presets** or **External Control / EMHASS**. Existing installations keep internal control until this option is deliberately changed.
- External control makes both thermostat entities unavailable, preserving their unique IDs. Their commands, presets and managed-Boost aliases are rejected even when called directly. Native switches, independent temperature numbers, telemetry and manual native Boost remain available.
- External hot-water permission changes write only that permission and display its native readback, without rewriting START/STOP or other permissions.
- Active internal overrides must restore before ownership changes. An unreachable controller or failed restoration keeps the change pending/refused. Returning to internal control does not automatically start a thermostat; select its mode explicitly.
- Optional humidity protection, interrupted-write recovery and a deliberately selected outside-temperature feed remain active. External control is not a read-only connection and cannot arbitrate writes from other integrations.
- Added external-control regression tests, including solar-driven room-temperature swings, restart recovery, separate SWW permissions and failed handoff. Updated the Dutch manual; no user automations or EMHASS helpers are changed by this update.

### Solar Gains And Slow Floor Response

Use **External Control / EMHASS** when your automations own preheating, solar waiting and cooling windows. Room-temperature changes alone then cause no new heating/cooling command from the integration. Permission off, actual equipment idle and the floor's remaining thermal response are different conditions; keep separate native readback, activity and electrical-power checks. A warmer room after stopping heating is not by itself a new cooling plan.

This release does not predict sunlight through windows, retune EMHASS's thermal model, change automation timers or cancel residual floor heat/cooling. Existing solar and thermal-learning helpers still need their entity references checked. Their policy and timing must be evaluated separately before claiming that the solar/inertia problem is solved.

In Configure page 1, select **Control Ownership > External Control / EMHASS**, continue to page 2 and save. With no active temporary overrides and humidity protection off, changing ownership sends no new native function-permission requests. Restoration of an old temporary program can change its settings back to the saved baseline; an enabled humidity guard can still pause cooling. The thermostat entities becoming unavailable is intentional in this mode; use A1 switches, B1.07 and B2.06/B2.07 instead.

## Changes in 0.1.20

- Added **B1.07 Heating - Native Target**, a separate number control for preparing the native heating dial before granting heating permission. It accepts 0.01 degree steps between 10 and 35 degrees and works with both permissions off.
- Restored separate **B2.06 Hot Water - Native START** and **B2.07 Hot Water - Native STOP** controls, keeping their earlier unique IDs when still registered. Each edits exactly one threshold, within the configured pump limit. Invalid START/STOP pairs are rejected rather than moving the other endpoint.
- Native temperature commands leave Heating, Cooling, Hot Water, Boost and anti-legionella permissions unchanged. Active thermostat control, relevant presets and Smart Grid ownership are checked to prevent competing edits. Failed writes retain a recovery journal.
- Older UI migrations now preserve registered hot-water START/STOP controls and user choices.
- Added regression tests for the EMHASS sequence: prepare a fractional room target with Heating off, separately enable Heating; temporarily change boiler START from 45 to 55 while STOP stays 58, then restore START to 45. No new automatic energy policy is installed.

### Using Existing EMHASS Automations

Keep EMHASS as the planner and your existing automations as the decision maker. Use `number.set_value` on the native heating target to prepare the room setting, confirm its numeric readback, then use **A1.01 Heating** to grant permission. Do not replace this sequence with `climate.set_temperature`: that is managed thermostat control and has different behavior. The native number refuses changes while the room thermostat is managing the pump; a deliberate use of the native Heating/Cooling switches releases that management. Do not select the thermostat's Auto mode or an energy preset concurrently with external permission automation.

Use the two native hot-water numbers for separate threshold edits; they support whole-degree changes and respect the START/STOP limits in Configure. With START 45 and STOP 58, setting START to 55 writes only START; restoring 45 also writes only START. The number's successful journal is removed immediately; your existing automation remains responsible for restoring a successfully committed temporary 55 setting to 45 after the cycle, including after an HA restart. The integration journal covers interrupted or unconfirmed edits, not the lifecycle of an external automation.

HA-generated entity IDs depend on the registry and cannot be known from this ZIP. Confirm new IDs and actual state meanings before switching automations. Auxiliary templates such as your EMHASS heating window and boiler plan may also refer to old Thermia entities; migrate those references as well. The supplied example solar-surplus package remains separate and should stay disabled when EMHASS already controls those loads. Start with no external outside-temperature feed selected until its ownership has been assigned during migration.

## Changes in 0.1.19

- Confirmed the Heating and cooling thermostat exposes Off, Heat, Cool and Heat/Cool without changing its entity identity.
- Added regression coverage for Heating and cooling Off: it writes only the space-heating and passive-cooling permissions off, leaving the hot-water permission and hot-water thermostat untouched.
- Updated package metadata and install instructions for the 0.1.19 ZIP.

## Where each setting is configured

**CP1** is Configure page 1; **CP2** is Configure page 2; **DEV** is the device's Configuration section. Native automation temperature numbers also expose the physical settings shown by the thermostats. Read-only sensors can report the same underlying pump value.

| Setting | Location |
|---|---|
| Control Ownership: internal or External Control / EMHASS | CP1 |
| Heat-pump model | CP1 |
| Inside temperature sensor | CP1 |
| Inside humidity sensor | CP1 |
| Outside temperature sensor | CP1 |
| Maximum configurable hot-water START, default 55°C | CP1 |
| Maximum configurable hot-water STOP, default 60°C | CP1 |
| External sensor freshness limit | CP1 |
| Room-temperature restart margin | CP1 |
| Heat curve point 1, warmest outside | CP2 |
| Heat curve point 2 | CP2 |
| Heat curve point 3 | CP2 |
| Heat curve point 4 | CP2 |
| Heat curve point 5 | CP2 |
| Heat curve point 6 | CP2 |
| Heat curve point 7, coldest outside | CP2 |
| Supply line minimum | CP2 |
| Supply line maximum | CP2 |
| Desired cooling supply | CP2 |
| Heat-pump power-control mode | CP2 |
| A1.01 Heating | DEV toggle |
| A1.02 Electric Auxiliary Heater Allowed | DEV toggle |
| A1.03 Hot Water Enabled | DEV toggle |
| A1.04 Thermia Boost | DEV toggle |
| A1.05 Anti-Legionella Programme | DEV toggle |
| A1.06 Cooling (Passive) | DEV toggle |
| A1.07 Humidity Protection In Normal Cooling | DEV toggle |
| B1.01 Heating — Low Mode Reduction | DEV slider |
| B1.02 Heating — Vacation Target | DEV slider |
| B1.03 Heating — Excess Energy Ceiling | DEV slider |
| B1.04 Heating — Excess Energy Heat Stop Increase, default 2°C | DEV slider |
| B1.05 Heating — Excess Energy Duration | DEV slider |
| B1.06 Heating — Heat Stop | DEV slider |
| B1.07 Heating - Native Target | DEV number |
| B2.01 Hot Water — Excess Energy Restart Gap, default 2°C | DEV slider |
| B2.02 Hot Water — Excess Energy Duration | DEV slider |
| B2.03 Hot Water — Low Mode Start | DEV slider |
| B2.04 Hot Water — Low Mode Stop | DEV slider |
| B2.05 Hot Water — Low Mode Duration | DEV slider |
| B2.06 Hot Water - Native START | DEV number |
| B2.07 Hot Water - Native STOP | DEV number |
| B3.01 Cooling — Vacation Reduction | DEV slider |
| B3.02 Cooling — Normal Humidity Limit, default 80% | DEV slider |
| B3.03 Cooling — Vacation Humidity Limit, default 65% | DEV slider |
| B3.04 Cooling — Dew Point Margin | DEV slider |

Modes, presets and Normal room/tank targets stay on the two thermostats. The initial connection form separately sets the pump address, Modbus port and unit ID. The former device sliders for curve points and supply limits are retired when updating; change these settings on CP2. The former separate Heating Excess Energy water target is also retired: charging uses the ordinary native Supply line maximum from CP2. Device controls that remain keep their entity IDs and custom names.

## Sensor categories and names (0.1.19)

Sensors and binary sensors now use sortable **C1-C7** labels. **A1** stays reserved for configuration toggles and **B1-B3** for configuration sliders. Each word in a display name starts with a capital; Inside and Outside replace Indoor and Outdoor throughout visible labels and explanations.

| Category | Examples |
|---|---|
| C1 Climate | Inside Temperature (Effective); Outside Temperature (Effective); Inside Humidity; Thermia Inside Temperature |
| C2 Heating | System Supply Temperature; System Return Temperature; Calculated Supply Target; Excess Energy Time Remaining |
| C3 Hot Water | Tank Top Temperature; Tank Lower Temperature; Tank Weighted Temperature; Low Mode Time Remaining |
| C4 Cooling | Running; Humidity Protection |
| C5 Heat Pump | Compressor Speed; Brine Inlet Temperature; Condenser Circulation Pump Speed; Auxiliary Heater Running |
| C6 Energy | Electrical Power; Thermal Power; Instantaneous COP |
| C7 System | Operating Status; Control Status; Control Logic |

For example, **C1.01 Climate — Inside Temperature (Effective)** is the reading selected for control; **C1.05 Climate — Thermia Inside Temperature** is the native Thermia probe. System supply/return probes remain distinct from heat-pump condenser inlet/outlet probes. The catalogue also covers optional and disabled telemetry. [The complete sensor name table](SENSOR_NAMES.md) lists all 277 old and new names.

Updating changes default display names only. Existing entity IDs, unique IDs, history, custom names, enabled/disabled choices, diagnostic placement, measurements and control behaviour are preserved. User-customized names take precedence, so those entries keep their custom labels. Home Assistant still separates its Sensors and Diagnostic sections; prefixes sort related entries within each section.

## Thermostats

### Hot water

One **Hot water thermostat** now contains both adjustable temperatures, with a **30–60°C** range and **1°C steps**:

- **Lower target = START:** restart automatic tank heating below this temperature.
- **Upper target = STOP:** stop automatic tank heating at this temperature.
- **Normal preset / Auto mode:** use the normal start/stop pair; start must be lower than stop.
- **Excess Energy preset:** save the original pair, enable native Thermia Boost, and temporarily set **STOP = 60°C** and **START = the lower of 60°C minus the requested restart gap and the configured maximum START**. With this controller's default START maximum of **55°C**, selecting a 1°C or 2°C gap produces **55/60°C**, an effective **5°C** gap. A controller confirmed to support START 59°C can use 59/60°C with a 1°C gap.
- **Low Mode preset:** turn native Thermia Boost off and use the configured lower range, default **START 35°C / STOP 40°C**, while saving the normal pair.
- **Off:** disable automatic hot-water production and cancel Excess Energy or Low Mode, restoring its saved normal temperatures. The preset menu and target adjustment disappear while Off; current tank temperature remains visible. Selecting Auto restores Normal operation with the saved pair.

Selecting **Normal** after Excess Energy turns native Boost **off** and restores the saved pair. This explicitly turns Boost off even when its original flag was on. For example, with the default maximum START 55°C: normal **48/55°C → Excess Energy 55/60°C → Normal 48/55°C**.

While Excess Energy remains selected, STOP stays at 60°C and START respects both the requested gap and the configured controller limit. The hotter of the top and weighted tank readings enforces the 60°C ceiling: Boost and the hot-water request pause there, then resume when the hotter valid top/weighted reading reaches the effective START or below. With the default 55/60°C limits, that restart threshold is 55°C. The preset remains selected until you choose Normal, Low Mode or Off, or its configured duration expires (default 6 hours). While actively charging below the ceiling, the integration reasserts native Boost if the pump clears its one-time request. If all tank readings are lost, it cancels Excess Energy and restores normal settings while the pump is reachable.

The thermostat’s **current temperature** is the reported **weighted hot-water temperature**, with the top reading as fallback if weighted is unavailable. The top, lower and weighted sensors are separate readings: the displayed value does not identify the sensor Thermia uses to start a native cycle. In Normal and Low Mode, this integration writes native START/STOP thresholds and leaves the start/stop decision to Thermia. The Genesis17.1 register guide specifies that these thresholds apply in the pump’s own **Normal** hot-water programme, but does not document the comparison algorithm. For integration-controlled Excess Energy, both the 60°C ceiling and the hysteresis restart check use the hotter valid top/weighted reading. The lower sensor is not part of those integration checks. The thermostat details show the display source and all three reported tank readings.

The thermostat displays the actual controller thresholds, including the effective Excess Energy START and 60°C STOP while charging. Its attributes show the requested and effective restart gaps, effective START/STOP, saved normal pair, boost availability and actual pump demands. **Changing a temperature on the thermostat automatically returns it to Normal.** Automation calls to `climate.set_temperature` follow the same rule. Only the changed end of the hot-water range replaces its saved Normal value; the untouched end returns to its saved Normal value unless that would make START equal to or higher than STOP. If you change both ends, both changes apply. Sending the unchanged preset pair leaves the preset and its timer running. Change the Low Mode profile itself using the device sliders. Selecting the Normal preset or reselecting the HVAC Auto mode also restores normal operation.

For a saved Normal range of **45/50°C**:

| Active preset | Requested change | Result in Normal |
|---|---|---|
| Excess Energy, 55/60°C | STOP to 59°C | 45/59°C |
| Excess Energy, 55/60°C | START to 46°C | 46/50°C |
| Low Mode, 35/40°C | START to 36°C | 36/50°C |
| Low Mode, 35/40°C | STOP to 41°C | 36/41°C, automatic 5°C gap |
| Either preset | Set both ends to 46/56°C | 46/56°C |

A requested START above its configured maximum is automatically capped before applying the edit. With the default START maximum of **55°C**, requesting **57/60°C** settles at **55/60°C** without an error. This also applies when leaving a preset; the original requested change still returns the thermostat to Normal. The entity republishes the accepted temperatures after a successful command so the dial can replace its provisional value, including when the pump was already at 55/60°C. STOP remains subject to its configured maximum. The standard Home Assistant range dial shares one maximum between its handles, so START can still be dragged above 55°C while STOP remains selectable up to 60°C; the integration corrects the START request.

When an edit produces a Normal START equal to or higher than STOP, the integration automatically creates a **5°C gap**. A STOP edit preserves STOP and lowers START; an edit to START alone preserves START and raises STOP. When both ends change, STOP takes priority. For the reported START 54°C / STOP 54°C collision caused by a STOP edit, the result becomes **49/54°C**. If the preferred pair reaches a 30–60°C boundary or the configured pump maxima, both ends shift as needed to fit a 5°C gap. For example, a colliding STOP edit to 30°C becomes 30/35°C. Valid existing pairs with gaps below 5°C remain unchanged; 5°C is a collision repair, not a new minimum for Normal operation. Invalid inputs, STOP above its configured maximum, or limits that cannot accommodate any legal 5°C pair are rejected before presets or timers change.

Home Assistant's range dial prevents the two displayed handles from crossing. For a STOP below the active preset's START, make a first adjustment to leave the preset, then adjust the restored Normal range to the final value. Automation calls to `climate.set_temperature` must include both range fields; use an explicit Normal pair when you intend to replace both values.

Configure page 1 defaults to maximum configurable **START 55°C / STOP 60°C**, based on the limits confirmed on this controller. These are capability limits, separate from the Normal temperatures selected on the thermostat. Other users can enter the maxima confirmed on their model. Missing or previously unset limits receive these defaults on upgrade; deliberately entered values are preserved. If your existing START maximum still says 60°C, change it to 55°C for this controller.

The device-page **B2.01 Hot Water — Excess Energy Restart Gap** slider accepts **1–30°C** in **1°C steps** and defaults to **2°C**. On upgrade from releases before 0.1.13, the former 3°C default becomes 2°C; other custom gaps remain unchanged. Normal START/STOP remain the pair selected on the hot-water thermostat; this gap applies to Excess Energy. Excess Energy requires a STOP maximum of at least 60°C, a START limit that permits a valid threshold of at least 30°C, and a valid native Boost readback. It caps its START at the configured maximum rather than trying an unsupported value. With maximum START 55°C, requested gaps of 1–5°C all use START 55°C / STOP 60°C; a gap of 6°C uses 54/60°C. An explicit activation still requests native Boost immediately below the 60°C ceiling, including when the water is already above START. After a ceiling pause, the hotter valid top/weighted reading must reach the effective START or below. Changing the device gap slider during an active run updates its effective thresholds through verified writes without restarting its timer or replacing the saved normal pair. Unsupported Boost cannot be replaced with an unverified command. Recovery settings are saved before temporary writes, and each write is checked by reading back its value.

**Low Mode temperatures** are two sliders on the device page, each within **30–60°C**; START must be lower than STOP. Low Mode enables ordinary automatic hot-water production without Boost and returns to Normal after its configurable time limit, default **24 hours**. Its **C3.05 Hot Water — Low Mode Time Remaining** sensor shows the remaining hours. Explicitly selecting Low Mode again restarts this timer; changing its configured temperatures or routine updates do not. It allows the tank to cool naturally instead of requesting a high-temperature refill; it does not actively cool stored water. A tank above 40°C need not reheat, and normal controller reheating uses the 35/40°C range once it cools. Selecting **Normal/Auto** restores the saved normal pair. Selecting Low Mode during Excess Energy first clears Boost and its timer, then applies the lower range. Selecting Excess Energy from Low Mode preserves the original normal pair for the eventual return to Normal, rather than treating the Low Mode pair as normal. Changing Low Mode temperatures while selected applies them after verified writes and retains the original normal pair. On restart the recovery journal restores Normal; Low Mode does not automatically resume.

The preset was previously named **Evening**. Update existing automation calls to `preset_mode: Low Mode`; saved temperature settings and entity identities are retained.

Select Low Mode at any time on the hot-water thermostat, or have a Home Assistant automation call the action below. Replace the example entity ID with your own. Another action can select `Normal` again. This integration does not schedule this preset automatically.

```yaml
action: climate.set_preset_mode
target:
  entity_id: climate.thermia_genesis_modbus_hot_water
data:
  preset_mode: Low Mode
```

The entity’s more-info dialog includes the preset selector. To show Normal, Excess Energy and Low Mode directly on a dashboard thermostat card, add the preset feature as below and replace the example entity ID with your actual Hot water climate entity:

```yaml
type: thermostat
entity: climate.thermia_genesis_modbus_hot_water
name: Hot water
features:
  - type: climate-hvac-modes
    hvac_modes:
      - "off"
      - auto
  - type: climate-preset-modes
    style: dropdown
    preset_modes:
      - Normal
      - Excess Energy
      - Low Mode
```

### Heating and cooling

The **Heating and cooling thermostat** offers Off, Heat, Cool and Heat/Cool. Its preset selector follows the selected mode:

| HVAC mode | Available presets |
|---|---|
| Heat | Normal, Excess Energy, Low Mode, Vacation |
| Cool | Normal, Vacation |
| Heat/Cool | Normal, Excess Energy (Heating Only), Low Mode (Heating Only), Vacation |
| Off | None; turn on to select a preset |

Low Mode and Excess Energy are heating functions and are hidden in Cool. Changing from Low Mode or Excess Energy to Cool returns to Normal. Low Mode and Vacation preserve the selected active HVAC mode. Selecting Off ends the active preset, restores saved Normal targets and clears its timer. Presets and temperature adjustment are hidden while Off; current inside readings remain visible. Selecting an active mode returns to Normal. In Normal:

- **Heat:** enable native Heating and disable Cooling, regardless of the external room reading. Copy the selected target to Thermia's heating/comfort dial.
- **Cool:** disable native Heating and enable Cooling regardless of the external room reading when humidity protection is inactive. Active humidity protection may pause cooling. Genesis 17.1 exposes no verified writable cooling room target, so this target remains in Home Assistant; Normal Cool does not regulate around it.
- **Heat/Cool (Auto):** use the selected inside reading to choose heating, cooling or neither against the lower/upper targets and configured hysteresis. Copy the lower heating target to Thermia; the upper cooling target is used only by Home Assistant.
- **Off:** disable both native space-conditioning permissions, Heating and Passive Cooling. It does not disable the separate hot-water permission.

Normal room targets support **10–35°C in 1°C steps**. Thermia temperature controls use whole-degree changes; reported readings and exact restored settings retain their original precision. The Home Assistant room-control hysteresis remains separate and defaults to 0.3°C. Enabling a permission does not prove that the compressor or cooling circuit is running: Thermia retains its own demand, outside-temperature and startup conditions. The entity's action and Operating status report actual activity.

Both thermostats keep **whole-degree targets and 1°C adjustments**. Measured inside and tank temperatures retain the precision reported by their source, without additional rounding to tenths. For example, 22.3°C remains 22.3°C and 50.26°C remains 50.26°C in the published measurement. Native register sensors keep their register-based display defaults; effective temperature sensors do not impose a display precision. Display choices in Home Assistant still control presentation: the built-in thermostat removes trailing zeros from whole readings, and its dial may format readings differently from the stored value. The integration cannot override that popup formatting.

When an inside humidity sensor is selected in Configure and its reading is valid, the heating thermostat also shows **current humidity** beside the inside temperature. This display does not require enabling humidity protection. Without a selected sensor or valid reading, the humidity value is omitted.

The heating history records the selected target as equal lower/upper bounds in Heat and Cool, and the separate heating/cooling targets in Heat/Cool. This keeps single targets visible when Home Assistant plots a period containing automatic-mode ranges. Off has no active target. Earlier history containing missing bounds is not rewritten by an update.

For both thermostats, a temperature-only action while Off is rejected. An automation can turn on and set a temperature in the same `climate.set_temperature` action by supplying an active `hvac_mode`; otherwise select an active mode before changing temperatures or presets.

**Excess Energy** requests heat above the normal room target to store surplus energy in the house. While actively charging, it copies the configured inside ceiling, **10–35°C**, to Thermia's native comfort dial, temporarily raises **the original Heat stop by the configured offset, default 2°C**, disables cooling and enables heating with a fixed water target equal to the pump's normal **Supply line maximum**, configured on CP2. There is no separate charging-water target. Legacy values from the removed setting are ignored. The offset is added to the saved original value without stacking across updates. For example, original Heat stop 17°C plus 2°C gives 19°C. If outside is warmer than this threshold, Thermia can still block heating; the selected offset deliberately does not track outside temperature. The resulting Heat stop must remain within the integration's supported −10–40°C range. Readbacks must confirm these changes. The original comfort dial, Heat stop and fixed-supply settings are saved before writing. At the room ceiling, heating pauses and restores the original Heat stop and fixed-supply settings, while the comfort dial keeps the charging ceiling. Leaving through Normal, Off, timeout or restart also restores the original comfort dial. In Heat/Cool, Excess Energy shows this single heating ceiling while preserving the Normal heating/cooling range for the eventual return to Normal. It requires a valid room-temperature measurement and readable native settings. The separate heating time limit defaults to 12 hours. Compressor timing, native priorities and safety restrictions still apply.

**Low Mode** reduces the saved Normal heating target by **2°C** by default, with a configurable whole-degree reduction and a minimum target of 10°C. It keeps the Normal cooling target. The reduction is always calculated from Normal, so reselecting it does not stack reductions.

**Vacation** uses a configurable heating target, default **17°C**. Cooling uses the saved Normal cooling target minus the configurable **Vacation — cooling temperature reduction**, default **0°C**. For example, Normal cooling 24°C with a 2°C reduction gives Vacation cooling 22°C, with a minimum of 10°C. In Heat/Cool, the derived heating target must stay below the derived cooling target; invalid settings are rejected before writes. In Cool, Vacation uses the inside temperature to regulate cooling around its derived target. Vacation retains humidity protection whenever an inside humidity sensor is selected.

**Changing a thermostat temperature returns every preset to Normal in every active HVAC mode**, retaining the mode and using the new temperature. In Heat/Cool, only the changed endpoint replaces its saved Normal value; the untouched endpoint returns to its saved Normal value. For example, Normal 21/25°C → Vacation 17/23°C → edit the upper target to 24°C gives Normal 21/24°C. A scalar target edit in Auto changes the lower target. Turn the thermostat on before editing an Off thermostat. Unchanged companion values sent by Home Assistant do not replace saved Normal values, and an unchanged request leaves the preset running. Invalid resulting ranges are rejected without changing the preset or timer. Changing the separate **Heating charge temperature limit** slider updates the charging ceiling while keeping Excess Energy selected.

| Mode / preset | Target sent to Thermia | Native operation |
|---|---|---|
| Heat / Normal | Normal heating target | Heating allowed, cooling off; ordinary Heat stop applies |
| Heat / Low Mode | Normal heating target minus configured reduction | Heating allowed, cooling off; ordinary Heat stop applies |
| Heat / Vacation | Configured Vacation heating target | Heating allowed, cooling off; ordinary Heat stop applies |
| Heat or Heat/Cool / Excess Energy | Configured charging ceiling | Original Heat stop plus configured offset and fixed water target equal to normal Supply line maximum; pauses at the inside ceiling |
| Cool / Normal | No native room-target register available | Native cooling permission, subject to enabled humidity protection |
| Cool / Vacation | No native room-target register available | Home Assistant regulates to the derived cooling target with configured humidity protection |
| Heat/Cool / Normal | Lower heating target | Home Assistant selects heating/cooling using the measured inside temperature |
| Heat/Cool / Low Mode | Reduced lower heating target | Normal upper cooling target remains unchanged |
| Heat/Cool / Vacation | Vacation heating target | Cooling uses Normal upper target minus Vacation reduction |
| Off | Restore temporary native targets | Both heating and cooling permissions off; hot water unchanged |

Normal, Low Mode and Vacation copy their heating target but respect Thermia's ordinary seasonal Heat stop, so a changed dial alone does not prove that heating will start. Genesis 17.1 has no documented writable **cooling room target**; the cooling-water supply target is a different setting.

Heating Low Mode and Vacation have no automatic reset timer. Select Normal or change a thermostat temperature to leave them. Temporary native settings are restored during restart recovery.

### Cooling humidity protection

Select an optional **Optional inside humidity sensor** reporting relative humidity in `%`. Enable **Humidity protection in Normal cooling** using its device Configuration switch to apply it to Cool and Heat/Cool, including Low Mode's Normal cooling. It is off by default. **Vacation always uses protection with a selected humidity sensor**, regardless of that toggle. Without a selected sensor, the integration cannot monitor humidity and retains ordinary cooling behavior.

Two separate sliders on the device page set the limits, each within **30–90%** in **1 percentage-point steps**:

| Cooling profile | Default pause limit | Resume at or below |
|---|---|---|
| Normal and Low Mode | **80%** | **78%** |
| Vacation | **65%** | **63%** |

The active preset selects its limit automatically; each has a 2 percentage-point restart gap. Changing either slider updates the saved preference, and an active limit is applied immediately. When upgrading from an older release, the former shared 65% default becomes 80% for Normal; other custom shared limits remain the Normal limit. The new Vacation limit starts at 65%. After upgrading, either limit can be set independently, including a Normal limit of 65% if desired.

The guard also calculates dew point from inside temperature and humidity, then raises the cooling-water target to at least the dew point plus the configured margin, default **2°C**, rounded upward to a whole degree. It keeps any higher original water target. For example, 25°C air at 60% humidity gives a dew point near 16.7°C and a minimum water target of 19°C with the default margin. If the required water target cannot be verified or exceeds the supported controller range, cooling pauses. Invalid or stale selected humidity/inside temperature readings also pause guarded cooling.

The device shows **C1.03 Climate — Inside Humidity**, **C1.04 Climate — Inside Dew Point** and **C4.02 Cooling — Humidity Protection** sensors. They explain missing readings, humidity pauses, the calculated water limit and pending restoration; valid measurements can still be monitored when Normal protection is disabled. Original cooling-water settings are saved before temporary changes and restored with cooling disabled first.

Radiant cooling lowers temperature without removing moisture. Keeping water above dew point helps reduce condensation risk, but this software does not measure floor-surface temperature or replace native protections. See [Uponor's radiant cooling guidance](https://www.uponor.com/en-us/customer-support/faq).

The preset name **Excessive Energy** has changed to **Excess Energy**. Update automation actions to the new name; heating actions in Heat/Cool must use **Excess Energy (Heating Only)**. Low Mode similarly uses **Low Mode (Heating Only)** in Heat/Cool. Existing entity identities and saved settings are retained.

## Mode and preset history

The device page adds two read-only sensors, enabled by default:

| History sensor | Example selections |
|---|---|
| C2.06 Heating — Mode And Preset | Heat · Normal; Heat · Low Mode; Heat/Cool · Excess Energy (Heating Only); Cool · Vacation; Off |
| C3.08 Hot Water — Mode And Preset | Auto · Normal; Auto · Excess Energy; Auto · Low Mode; Off |

Each sensor records the selected mode and preset together as a normal Home Assistant state. Open either sensor's History, or select it in the main **History** view. The mode and preset are also separate attributes for inspection. Off has no active preset. These sensors describe the thermostat selections, not whether the compressor is running; use Operating status for native activity. They retain the saved selection during a controller outage and expose whether the controller is available.

Home Assistant already records the climate mode and selected preset attribute, but its built-in thermostat history chart does not plot a preset timeline. The new sensors provide visible timeline segments without changing that thermostat screen. They start recording after installation, follow your Recorder retention and inclusion/exclusion settings, and do not backfill earlier climate history. Under the default Recorder configuration they need no extra setup; these text states do not have long-term statistics.

For temperatures and selection timelines together, add the native History graph card in [examples/mode_preset_history_dashboard.yaml](examples/mode_preset_history_dashboard.yaml), replacing its four example entity IDs with your own. The mode/preset timelines appear above the temperature graphs. No custom card or frontend resource is needed.

## Configuration switches

The device’s **Configuration** section starts with all seven **A1.xx toggles**, ordered Heating → Hot water → Cooling. Sliders follow as **B1.xx Heating**, **B2.xx Hot water** and **B3.xx Cooling**. The electric-assistance switch is grouped under Heating, but its native permission can also apply to hot-water production. Device controls that remain keep their existing entity IDs and user-chosen names.

- **A1.01 Heating:** native heating permission.
- **A1.02 Electric Auxiliary Heater Allowed:** permit automatic electric assistance; it does not force the heater to run.
- **A1.03 Hot Water Enabled:** automatic tank heating on/off. Off cancels an active preset and restores Normal settings.
- **A1.04 Thermia Boost:** directly changes only the native Boost flag, leaving the water thresholds unchanged.
- **A1.05 Anti-Legionella Programme:** native hygiene programme on/off.
- **A1.06 Cooling (Passive):** native passive-cooling permission.
- **A1.07 Humidity Protection In Normal Cooling:** the integration’s saved humidity-protection preference.

The Heating and Cooling switches are separate native permissions and can both be enabled. A deliberate change returns space conditioning to native control. A thermostat mode applies the behavior described above: direct permissions for Heat or Cool, room-temperature switching for Auto, or temporary charging for Excess Energy.

The separate Thermia Boost switch mirrors the real controller flag, so the pump can clear it after a cycle. During Excess Energy, leave the preset through Normal or Off before turning the direct switch off; this avoids the sustained preset immediately requesting Boost again. Direct Boost is available whenever its valid native flag can be read, independent of the confirmed 60°C maxima needed for Excess Energy. The direct native Boost switch is not governed by the Excess Energy thermostat timers.

Thermia’s [Calibra E / E Cool manual, page 15](https://www.thermia.pl/storage/Attachment_File/1-2000/817-file-instruction-manual-doc-00059502.pdf) describes native Boost as additional hot-water production including the electric top-up heater. Excess Energy preserves your electric-heater permission; it does not automatically enable it.

**Humidity protection in Normal cooling** controls the saved integration preference rather than a native pump register. Configure does not duplicate this switch. Vacation protection with a selected humidity sensor remains active independently.

Home Assistant alphabetically sorts its built-in device Configuration card. A/B prefixes keep all toggles together above the sliders; slider categories run Heating → Hot water → Cooling. A custom name you have assigned takes precedence and can change that order; dashboards can arrange entities freely.

## Folder/domain migration in 0.1.5

The component folder and internal domain are now **`thermia_genesis_modbus`** as requested. This is a one-time migration from `thermia_pv`, including 0.1.4 even when that old folder was manually renamed. Home Assistant's public configuration API cannot change the domain of an existing entry.

1. While the old integration is still installed and connected, return the heating thermostat to **normal** and hot water to **Auto**, so it restores all charging overrides. Note your Configure settings and entity references.
2. Remove the old integration entry in **Settings → Devices & services** while its source is still installed.
3. Remove the obsolete `thermia_pv` source folder, if present. Install the new **`custom_components/thermia_genesis_modbus`** folder from this archive, replacing a manually renamed old folder if necessary.
4. Restart Core, add **Thermia Genesis Modbus**, and enter your model, temperature sensors and confirmed pump limits in Configure.
5. Update dashboard/automation entity IDs where necessary. Change old preset names (`normal`, `pv_charge`, `Auto`, `Energy Excess`) to **Normal** or **Excess Energy**. Use the heating-only label for Excess Energy in Heat/Cool.

Do not run the old and new entries for the same pump together. Setup refuses a duplicate connection while the old `thermia_pv` entry exists. If the old integration can no longer load because its source folder was renamed, first restore its original folder and restart to recover/restore its settings before removing the entry. Do not remove a recovery journal while temporary charging overrides are active.

### Preset reset timers

The device page offers three independent maximum-duration sliders in hours: **Heating Excess Energy 12h**, **Hot-water Excess Energy 6h**, and **Hot-water Low Mode 24h**, adjustable from **1 to 168h in 1-hour steps**. Existing fractional durations and their deadlines are preserved until you deliberately select a new whole-hour value. At expiry, heating returns to the **Normal** preset and restores its temporary native settings; hot water returns to **Normal/Auto**, turns native Thermia Boost **off**, and restores the saved START/STOP pair.

Every explicit selection of Excess Energy starts a fresh timer, including reselecting it through `climate.set_preset_mode`. The standard Home Assistant dropdown does not re-submit its already selected option, so use **Normal → Excess Energy** there. Automatic polling, native Boost reassertion and pauses at temperature ceilings do not restart the clock. Editing a duration recalculates the deadline from the current run's start time. Selecting Normal or Off ends the relevant timer; selecting Low Mode ends the hot-water Excess Energy timer and starts its own reset timer. Explicitly selecting Low Mode again resets its clock. Routine updates and changing Low Mode START/STOP do not reset it.

The device's **Sensors** section shows three default-enabled duration entities:

- **C2.04 Heating — Excess Energy Time Remaining**
- **C3.04 Hot Water — Excess Energy Time Remaining**
- **C3.05 Hot Water — Low Mode Time Remaining**

All three display remaining hours, refresh every 30 seconds independently of pump polling, and show 0 when inactive or elapsed. They remain available during a connection failure. Their details include the start time, reset deadline, configured duration, controller availability and **reset_pending**. If the timer reaches 0 while restoration cannot be confirmed, reset_pending stays true; a countdown at 0 is not confirmation that the pump has restored its settings. These entities only report time and do not send commands or extend timers. You can add them to a dashboard.

The thermostats also expose start time, deadline, maximum duration and remaining hours as attributes. Expiration is checked before further charging requests, during the normal 30-second update cycle. On restart the saved journal restores normal settings; charging does not automatically resume. If the connection is lost at expiry, restoration stays pending and is retried when communication returns. Home Assistant must be running and able to reach the pump to send these changes; this is an integration timer, not a timer stored in the Thermia controller.

For automations, replace the sample entity ID with your actual hot-water climate entity:

```yaml
action: climate.set_preset_mode
target:
  entity_id: climate.thermia_genesis_modbus_hot_water
data:
  preset_mode: Excess Energy
```

Use `preset_mode: Normal` to finish charging. Change both normal hot-water thresholds together with `climate.set_temperature`, using `target_temp_low` and `target_temp_high`.

### Device model and display name

The first Configure page includes **Heat-pump model**. Enter the model for your device, for example **Calibra E Cool 8 400V BW**; its generic default is **Genesis heat pump**. Saving updates only the model label under Device info. Use Home Assistant's **Edit device** to change its display name. These labels do not change device identities, register mappings or supported controls.

### Read the control guide on the device page

Open **Settings → Devices & services → Thermia Genesis Modbus → your heat-pump device**. In its **Sensors** section, select **Control logic — Open for explanation**. The details explain all heating and hot-water presets, temperature-edit behavior, cooling humidity protection, reset timers, remaining-time sensors, external sensor fallback and configuration switches. The guide uses your current configured temperatures and durations. It remains readable while the controller is unavailable and does not send requests to the pump.

## Native temperature sliders

Configure page 2 groups the seven heat-curve points, the heating-water minimum/maximum, the cooling-water target and the Smart Grid mode. Heat stop remains a device slider; the other native sliders are not duplicated on the device page:

| Setting | Integration slider range | Meaning |
|---|---|---|
| Heat stop | −10–40°C | Outside threshold for stopping heating |
| Supply line minimum | 5–65°C | Lower heating-water supply limit |
| Supply line maximum | 5–65°C | Upper heating-water supply limit |
| Desired cooling supply (mixing valve 1) | 5–30°C | Native passive-cooling target for that circuit |
| Heat curve points 1–7 | 5–65°C each | Native supply-water targets, ordered from warmest to coldest outside curve point |

The manufacturer's register guide gives addresses and encoding but no valid UI ranges. These are conservative software bounds, with 1°C steps, rather than a claim about your pump's permitted range. Only settings with a finite controller readback are offered. Unchanged saved values generate no writes, including existing fractional values. New writes are verified against actual readback; a rejected or clamped setting causes an error and restoration. Minimum and maximum are written in an order that keeps their pair valid.

The app's single **Heat curve** selection (28 in your screenshot) is a read-only register in the documented domestic Genesis 17.1 map. The editable seven curve points are exposed individually. Writing one point is not equivalent to changing the whole app curve. The documented cooling target controls **passive-cooling mixing valve 1**; the app's two main Cooling/Passive cooling sliders do not have two verified independent writable mappings in this register guide, so the integration does not invent them. Unsupported settings are omitted with an explanation.

Changing heating limits during Excess Energy restores the temporary fixed supply override and returns heating to Normal/native control before applying the new limit. Re-select a thermostat mode/preset to apply its Heating/Cooling behavior again.

### Heating target in Home Assistant versus the native Thermia control

The native Thermia dial was verified on this controller: the reported comfort value **23** corresponds directly to the displayed **23°C**. Heating uses holding register 5 with its documented 0.01°C encoding; no extra 20°C offset is applied. Selecting Heat copies the single Home Assistant target, and Auto copies its lower heating target, with readback verification. An unset Home Assistant heating target is initialized from a valid native reading. Changing the heating thermostat's temperature during Excess Energy ends charging, returns to Normal and applies the new target. The separate **Heating — Excess Energy ceiling** slider changes the charging ceiling while keeping Excess Energy selected. A deliberate change of the native **Heating — Heat stop** slider ends Excess Energy, restores its temporary settings, then applies the new ordinary Heat stop value.

When Thermia has no native inside sensor, its comfort dial adjusts the heating curve rather than measuring the room. The external inside sensor remains a Home Assistant input for Auto, Excess Energy, Vacation cooling and active humidity protection. Normal Heat and unguarded Normal Cool permissions do not depend on that reading.

The thermostat details show the **native heating target**, its synchronization status, and the separate saved Normal target. The public domestic Genesis 17.1 map has **no verified writable cooling room target**. The passive-cooling supply slider controls water temperature for mixing valve 1; it is not a cooling room thermostat. The integration therefore copies only the heating target, while Auto's upper cooling target stays in Home Assistant.

The Excess Energy inside ceiling supports **10–35°C**, matching the integration’s native room-target range. It is now copied to Thermia’s comfort dial in Heat and Heat/Cool, including while charging is paused. Leaving the preset restores the exact original native dial value.

## External temperature sources

Choose inside and outside Home Assistant sensors independently on Configure page 1. Valid finite readings are preferred over Thermia’s readings. Celsius, Fahrenheit and Kelvin are converted to Celsius; an otherwise valid selected sensor without a unit is interpreted as Celsius.

The default **freshness limit is 0**: a stable valid state stays usable until it becomes unknown, unavailable or invalid. A positive limit enforces a maximum age, measured from the sensor’s last report. The integration now handles external update callbacks on Home Assistant’s event loop and recovers on unchanged sensor reports after an enforced timeout. Effective temperature attributes include the selected entity, its raw value and unit, plus the reason when an external or native input cannot be used. Two always-available diagnostic sensors, Inside temperature source and Outside temperature source, report the chosen source/fallback reason even during pump connection failures. The selected source sensor now notifies entities before an offline Modbus refresh, avoiding stale source diagnostics.

When the inside input fails, the measured Thermia room temperature is used if available and without a sensor alarm. A thermostat target is not a room measurement. Auto requires a valid inside reading; Excess Energy is cancelled if neither measurement remains valid. Normal Heat and unguarded Normal Cool retain native control independently of the room reading. Vacation cooling and active humidity protection pause when their required readings are unavailable.

Outside readings are written to BMS holding register 118 and source register 117 selects BMS. Invalid or expired external readings switch back to the physical PT1000 sensor while Home Assistant is running. The original source is saved for restoration when the selected input is removed or the integration unloads. If Home Assistant stops abruptly, the [Thermia Genesis 17.1 guide](https://odoo.geotherma.be/shop/th-206943-thermia-calibra-e-cool-8-3x400v-n-13581/document/1097) specifies a BMS fallback delay of 12 hours, then 0°C if the physical outside sensor is absent.

## Supply and return readings

**Heat-pump return temperature** reads condenser inlet, input register 8; **Heat-pump supply temperature** reads condenser outlet, register 9. These names clarify the real measurement locations and preserve their existing entity identities.

**C2.01 Heating — System Supply Temperature** (input 12, marked EM in Thermia’s guide) and **C2.02 Heating — System Return Temperature** (input 27) are separate system-probe readings. They remain unavailable if the controller does not provide those measurements or refuses their registers. The live Home Assistant inspection confirmed that condenser supply and return are present while the two system readings are unavailable; it did not establish whether the cause is an absent probe or a refused register. Their attributes now explain the absent reading. The integration does not silently substitute condenser temperatures under the system-probe names.

## Status, telemetry and recovery

One **C7.01 System — Operating Status** sensor shows the native main operating state. Attributes include concurrent functions, queued demands, active alarms, startup delay and control warnings. The catalogue covers 255 documented logical readings/settings/signals, plus the two model-specific Boost and anti-legionella controls, which are probed without an opt-in checkbox. Unsupported registers are isolated; optional equipment and advanced diagnostic entities start disabled.

Readings include tank, room, outside, brine and refrigerant temperatures, pressure, compressor gear/speed, pumps and valves, operating hours, electrical/thermal power, metering, settings, software versions and individual alarms. Instantaneous COP is available only with valid reported powers and positive electrical consumption. Accessory readings require the corresponding equipment.

Thermia keeps its compressor delays, demand priorities and equipment protections. A confirmed command is a demand request, not proof of an immediate compressor start. Heating and hot water may be served in sequence. Check actual status when verifying surplus-energy operation.

Temporary overrides are restored after a restart before new commands are accepted; Excess Energy does not automatically resume after restart. If restoration fails, the saved recovery journal is retried when the pump is reachable. A failed Configure change rolls back previous settings, with pending recovery retained if the connection is lost. Expired charging and restart cancellation receive priority over unrelated configuration restoration. Recovery still accepts an older 60/60°C journal solely to restore its original normal settings; new activations always keep START below STOP.

**Download diagnostics** provides readings, unsupported keys and temperature-source reasons, with connection host and selected sensor identifiers redacted. No data is uploaded automatically. To remove the integration, leave charging while the pump is reachable, delete its entry under Devices & services, then remove the source folder.

## Sources and development

- [Thermia domestic Genesis 17.1 register guide](https://odoo.geotherma.be/shop/th-206943-thermia-calibra-e-cool-8-3x400v-n-13581/document/1097)
- [renaatdb/thermia-calibra](https://github.com/renaatdb/thermia-calibra), for model-specific register hints and shared Modbus conventions.
- [CJNE/thermiagenesis](https://github.com/CJNE/thermiagenesis), for entity/register cross-checks.
- [Home Assistant shared Modbus API](https://developers.home-assistant.io/docs/modbus/introduction/)

Run the included tests with Python 3.12 or newer and `modbus-connection>=4.8.1,<5`, `pytest`, `voluptuous`, `PyYAML`, `Jinja2` and `ruff`: `python -m pytest tests` and `ruff check .`. Set `THERMIA_HA_SOURCE` to the Core 2026.9.4 `homeassistant` source directory to also check API contracts. These tests do not replace verification on Home Assistant OS and the physical controller.

Independent community software, with no affiliation to Thermia.

## Solar surplus and alarm automation

The **B1.03 Heating — Excess Energy Ceiling** number entity supports **10–35°C in 1°C steps** and retains the unique identity of the earlier Heating charge temperature limit entity. It sets the inside charging ceiling without changing the normal thermostat target. It is intended for automations that temporarily set the ceiling and restore it afterwards.

The optional [solar surplus package](examples/thermia_pv_automation_package.yaml) implements the HHome alarm priorities, five-minute qualification, evening schedule, +2°C / +5°C heating limits, and settings restoration. Follow [AUTOMATION_SETUP.md](examples/AUTOMATION_SETUP.md) to install it. It is supplied disabled until you enable its helper.
