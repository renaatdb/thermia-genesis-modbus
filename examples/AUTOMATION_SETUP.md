# Thermia solar surplus automation

This package is prefilled for your heat pump, HHome alarm and power sensors. It runs inside Home Assistant; no cloud scheduler is involved. Install Thermia Genesis Modbus **0.1.18** first: this package uses its heating preset labels, saved Normal temperatures and **Heating — Excess Energy ceiling** entity to apply the +2°C / +5°C ceiling.

## Installation

1. Replace `/config/custom_components/thermia_genesis_modbus/` with the folder from the 0.1.18 ZIP, and restart Home Assistant. Preserve your existing integration entry.
2. On the heat pump device page, open **B1.03 Heating — Excess Energy ceiling** and copy its entity ID. In `thermia_pv_automation_package.yaml`, replace the `limit_entity` value if it differs from the prefilled ID. Home Assistant chooses entity IDs from its registry, so this new ID cannot be guaranteed in advance.
3. Create `/config/packages/`, and copy `thermia_pv_automation_package.yaml` there as `thermia_pv.yaml`.
4. Add this under the existing `homeassistant:` section in `/config/configuration.yaml`. Create that section if absent; do not create a duplicate or replace an existing packages include.

   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```

5. Run Home Assistant's configuration check, then restart.
6. Enable the helper **Thermia PV automation enabled** (`input_boolean.thermia_pv_enabled`). It starts off on first installation. This switch enables both the sunset schedule and surplus charging. Turning it off restores an automation-owned charge on the next check.

The package is one complete configuration file. Do not paste it into the automation editor or `automations.yaml`. Disable older automations that change these same presets.

## Entities already filled in

| Purpose | Entity |
|---|---|
| Heating | `climate.storage_room_calibra_eco_cool_8_400v_bw_heating_and_cooling` |
| Hot water | `climate.storage_room_calibra_eco_cool_8_400v_bw_hot_water` |
| Alarm | `alarm_control_panel.hhome` |
| Battery SOC | `sensor.stp10_0_3se_40_146_battery_soc_total` |
| Grid power | `sensor.net_grid_power` |
| Battery power | `sensor.net_battery_power` |

Grid positive means export; grid negative means import. Battery negative means discharge. Power units must be W or kW; kW is converted to W. SOC must be a percentage from 0 to 100. The alarm mapping uses Home Assistant's standard states: Disarmed=`disarmed`, Home=`armed_home`, Away=`armed_away`, Holiday=`armed_vacation`. Check the Holiday raw state in Developer Tools → States if your alarm provider uses a custom mapping.

## What it does

- **Sunset:** hot water switches to Low Mode, unless you explicitly turned hot water Off. A PV charge owned by this automation ends. Manual hot-water Excess Energy is also replaced by Low Mode at the sunset event, as requested.
- **Two hours before the following sunset:** Low Mode switches to Normal. Excess Energy is left alone. Low Mode remains active through the morning until that afternoon event.
- **Night recovery:** if Home Assistant missed sunset and hot water is Normal at night, the next check switches it to Low Mode. A manually selected Excess Energy preset is left alone by this recovery check.
- **Disarmed / Home / Away:** hot water gets surplus first. Heating follows when hot water reaches its ceiling or does not qualify for charging.
- **Holiday:** heating gets surplus first; hot water follows when heating reaches its ceiling or does not qualify.
- Each actual alarm state must remain unchanged for **five minutes** before it changes priority. During confirmation, an existing charge continues; no new charge starts. Import/discharge stops still apply. Because the alarm confirmation template updates once per minute, confirmation can take up to about six minutes.

| Charge | Start conditions maintained for 5 minutes | Inside ceiling |
|---|---|---|
| Hot water | Battery SOC >80% and export ≥1500 W | Not applicable |
| Heating | Battery SOC >80% and export ≥750 W | Normal heating target +2°C, or +5°C in Holiday |

Starts are daytime only. The controller checks every 30 seconds after the five-minute qualification. SOC is a start condition; falling below 80% alone does not stop an existing charge.

Only one surplus charge runs at a time. Heating must already be in Heat or Auto (Heat/Cool); Off and Cool are respected. While charging the house, hot water is temporarily Off to prevent native hot-water demand from taking priority. An originally Off boiler remains Off afterwards.

In Heat, charging selects **Excess Energy**. In Auto, it selects **Excess Energy (Heating Only)** and temporarily prevents cooling; ending charging restores the integration's saved Normal behavior. Both heating labels are recognized when checking ownership, completion and manual charging. Hot water keeps the **Excess Energy** label.

Hot-water Excess Energy temporarily sets STOP **60°C** and START **the lower of 60°C minus the configured restart gap and the configured START maximum** (requested gap 1–30°C, default 2°C; START maximum 55°C gives 55/60°C). It also enables native Thermia Boost. The automation considers the boiler charged when the higher of the available top and weighted temperatures reaches 60°C; it does not infer completion from Idle.

Heating changes the **charge ceiling**, preserving the normal thermostat target. It uses the saved Normal heating target, including Auto's saved lower target, even when a Low or Vacation target is displayed. The ceiling is capped at 35°C and rounded down to the entity's 1°C step. A valid inside temperature is required. Your selected ventilation sensor supplies that temperature through the integration.

An import of **at least 200 W**, or battery discharge of **at least 200 W**, maintained for five minutes ends an automation-owned charge. Hot water returns to **Low Mode at night / Normal during the day**. Heating returns to Normal, and its previous charge ceiling is restored. If you changed the ceiling yourself during charging, that new value is preserved when the charge ends. Older fractional ceilings are rounded down to a whole degree when saved or restored.

Missing/invalid energy inputs stop an owned charge immediately. Lost pump communication may prevent immediate restoration; ownership and saved settings remain for retries once communication returns. The integration's own 12h heating / 6h hot-water defaults remain a second timeout. Polling does not reselect the preset or restart those clocks. Completion or timeout blocks that same function until its surplus qualification drops and a new five-minute window occurs.

Hot-water Low Mode has its own **24-hour default timer**. The sunset action starts that timer; routine checks leave it running. The following pre-sunset action normally returns hot water to Normal after about 22 hours, before the default timer expires. If you choose a shorter duration that expires at night, the package's night-recovery rule can select Low Mode again and start a new timer. Adjust that rule if you want a short Low Mode run to end permanently during the night.

The helpers persist ownership and saved ceilings across restarts. The automation reconciles them with the integration's restored state. A manual Excess Energy preset that the automation did not start blocks new automated charging and is not cancelled by an import/discharge check. Leave the ownership helpers alone; use the enabled helper to stop the automation.

## Checking it

Inspect **Thermia PV confirmed alarm mode**, **Thermia PV water ready**, **Thermia PV heating ready**, **Thermia PV grid stop**, **Thermia PV battery stop** and **Thermia PV automation owner** in Developer Tools → States. Use the automation trace to see the latest decision. These helpers are separate from the heat pump's existing remaining-time sensors.

The integration tests and simulated automation scenarios were run locally. This package has not been loaded into your Home Assistant instance or exercised against your pump. The Home Assistant configuration check and first live surplus cycle are still required.

References: [Home Assistant packages](https://www.home-assistant.io/docs/configuration/packages/), [template entities and delay_on](https://www.home-assistant.io/integrations/template/), [alarm states](https://www.home-assistant.io/integrations/alarm_control_panel/), [sun triggers](https://www.home-assistant.io/docs/automation/trigger/#sun-trigger).
