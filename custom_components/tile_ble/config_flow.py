"""Config flow: one-time Tile cloud sign-in → tracker selection → key storage."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import selector

from .const import CONF_BATTERY_ENABLED, CONF_EMAIL, CONF_PASSWORD, CONF_TILES, CONF_TIMEOUT, DEFAULT_TIMEOUT, DOMAIN
from .tile_cloud import TileAuthError, TileRequestError, fetch_tiles


class TileBleConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._user_uuid = ""
        self._tiles: dict[str, dict[str, Any]] = {}
        self._email = ""
        self._password = ""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                self._user_uuid, tiles = await fetch_tiles(
                    async_get_clientsession(self.hass), user_input["email"], user_input["password"])
            except TileAuthError:
                errors["base"] = "invalid_auth"
            except TileRequestError:
                errors["base"] = "cannot_connect"
            else:
                if not tiles:
                    errors["base"] = "no_tiles"
                else:
                    self._tiles = {t["tile_uuid"]: t for t in tiles}
                    self._email = user_input["email"]
                    self._password = user_input["password"]
                    await self.async_set_unique_id(self._user_uuid)
                    self._abort_if_unique_id_configured()
                    return await self.async_step_select()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("email"): str,
                                    vol.Required("password"): selector.TextSelector(
                                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))}),
            errors=errors)

    async def async_step_select(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            chosen = [self._tiles[u] for u in user_input["tiles"]]
            data: dict[str, Any] = {CONF_TILES: chosen, CONF_BATTERY_ENABLED: user_input[CONF_BATTERY_ENABLED]}
            if user_input[CONF_BATTERY_ENABLED]:
                # Credentials only need to be stored for the once-per-24h battery_status query;
                # presence tracking and ringing do not need them.
                data[CONF_EMAIL] = self._email
                data[CONF_PASSWORD] = self._password
            return self.async_create_entry(title="Tile BLE", data=data)
        options = [selector.SelectOptionDict(value=u, label=f"{t['name']} ({t['product']})")
                   for u, t in self._tiles.items()]
        return self.async_show_form(
            step_id="select",
            data_schema=vol.Schema({
                vol.Required("tiles", default=list(self._tiles)): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=options, multiple=True)),
                vol.Required(CONF_BATTERY_ENABLED, default=False): bool,
            }))

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await fetch_tiles(async_get_clientsession(self.hass), entry.data[CONF_EMAIL],
                                  user_input[CONF_PASSWORD])
            except TileAuthError:
                errors["base"] = "invalid_auth"
            except TileRequestError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]})
        return self.async_show_form(
            step_id="reauth_confirm",
            description_placeholders={"email": entry.data[CONF_EMAIL]},
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))}),
            errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return TileBleOptionsFlow()


class TileBleOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        cur = self.config_entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)
        return self.async_show_form(step_id="init", data_schema=vol.Schema({
            vol.Required(CONF_TIMEOUT, default=cur): vol.All(int, vol.Range(min=1, max=240))}))
