"""An async HTTP client to interface with the CAIC website.

CAIC Website Avy Obs request::

     /**
     * @param startDatetime a utc date string in format YYYY-MM-DD HH:mm:ss
     * @param endDatetime a utc date string in format YYYY-MM-DD HH:mm:ss
     * @param header the caic auth header (shared between all of the pagination requests should be fine)
     * @param page offset page
     * @return raw caic response json (including pagination data so we can call this function again if there is more data)
     */
    async getCaicPaginated (startDatetime, endDatetime, headers, page = 1) {
        queryArgs = new URLSearchParams({
        per: 1000,
        page,
        observed_after: startDatetime,
        observed_before: endDatetime,
        t: Date.now()
        });
        const result = await fetch(`https://api.avalanche.state.co.us/api/avalanche_observations?${queryArgs}`, { headers });
        return await result.json();
    },


A field reports example API call::

    https://api.avalanche.state.co.us/api/v2/observation_reports?page=1&per=250&r[observed_at_gteq]=2023-05-14T06%3A00%3A00.000Z&r[observed_at_lteq]=2023-05-22T05%3A59%3A59.999Z&r[sorts][]=observed_at+desc&r[sorts][]=created_at+desc
    https://api.avalanche.state.co.us/api/v2/observation_reports?page=1&per=250&r[backcountry_zone_title_in][]=Front+Range&r[snowpack_observations_cracking_in][]=Minor&r[snowpack_observations_collapsing_in][]=Rumbling&r[observed_at_gteq]=2023-05-14T06:00:00.000Z&r[observed_at_lteq]=2023-05-22T05:59:59.999Z&r[sorts][]=observed_at+desc&r[sorts][]=created_at+desc
    https://api.avalanche.state.co.us/api/v2/observation_reports?page=1&per=250&r[observed_at_gteq]=2023-05-14T06:00:00.000Z&r[observed_at_lteq]=2023-05-22T05:59:59.999Z&r[saw_avalanche_eq]=true&r[sorts][]=observed_at%20desc&r[sorts][]=created_at%20desc

A forecast API call - must use the proxy for these - the avid API is behind auth::

    https://avalanche.state.co.us/api-proxy/avid?_api_proxy_uri=/products/all?datetime=2023-05-22T06:31:00.000Z&includeExpired=true

Weather dispatches API call - also a different domain 😠::

    https://m.avalanche.state.co.us/api/dispatches/current?type=zone-weather-forecast

Example weather plot download::

    https://classic.avalanche.state.co.us/caic/obs_stns/hplot.php?title=VailResort%20CAVMM%20(10303%20ft)%20-%20Vail%20&%20Summit%20County&st=CAVMM&date=2023-05-22-06

"""

from json import JSONDecodeError
import time
import typing

import aiohttp
import pydantic

from . import LOGGER
from . import errors
from . import fieldobs
from . import models


API_BASE = "https://api.avalanche.state.co.us"
CLASSIC_BASE = "https://classic.avalanche.state.co.us"


class CaicClient:
    def __init__(self) -> None:
        self.headers = {}
        self.api_session = aiohttp.ClientSession(base_url=API_BASE)
        self.classic_session = aiohttp.ClientSession(base_url=CLASSIC_BASE)

    async def close(self) -> None:
        await self.api_session.close()
        await self.classic_session.close()

    async def _api_get(self, endpoint: str, params: dict | None = None):
        try:
            resp = await self.api_session.get(endpoint, params=params)
            if resp.status >= 400:
                raise errors.CaicRequestException(f"Error status from CAIC: {resp.status}")

            data = await resp.json()

        except aiohttp.ClientError as err:
            raise errors.CaicRequestException(f"Error connecting to CAIC: {err}") from err
        except JSONDecodeError as err:
            raise errors.CaicRequestException(f"Error decoding CAIC response: {err}") from err

        else:
            return data

    async def _classic_get(self, endpoint: str, params: dict | None = None):
        return await self.classic_session.get(endpoint, params=params)

    async def _classic_post(self, endpoint: str, json: dict | None = None):
        return await self.classic_session.post(endpoint, json=json)

    async def _api_paginate_get(self, page: int, per: int, uri: str, params: typing.Mapping | None = None) -> dict | None:
        if params is None:
            params = {}

        params["per"] = per
        params["page"] = page

        data = await self._api_get(uri, params=params)

        return data

    async def avy_obs(self, obs_before: str, obs_after: str) -> list[models.AvalancheObservation]:
        paginating = True

        obs = []
        page = 1

        params = {
            "observed_after": obs_after,
            "observed_before": obs_before,
            "t": str(int(time.time()))
        }

        while paginating:
            try:
                obs_resp = await self._api_paginate_get(page, 1000, "/api/avalanche_observations", params)
            except Exception as err:
                LOGGER.error("Failed to request the CAIC avy obs endpoint: %s" % err)
                page += 1
                continue

            try:
                caic_resp = models.CaicResponse(**obs_resp)
            except pydantic.ValidationError as err:
                LOGGER.warning("Unexpected response from the avy obs endpoint: %s" % str(err))
                page += 1
                continue

            if caic_resp.meta.current_page == caic_resp.meta.total_pages:
                paginating = False
            # Just a sanity check to avoid infinite looping
            elif page == caic_resp.meta.total_pages:
                LOGGER.debug("Paginating mismatch: %s" % "caic_resp.json()")
                paginating = False

            for item in caic_resp.data:
                try:
                    obs_obj = item.attrs_to_obs()
                except (pydantic.ValidationError, ValueError) as err:
                    LOGGER.warning("Unable to cast a response object to an Observation: %s" % str(err))
                else:
                    obs.append(obs_obj)

            page += 1

        return obs

    async def field_report(self, report_id: str) -> fieldobs.FieldObservation:
        """Get a single field report from CAIC."""

        params = dict(
            obs_id=report_id,
        )

        resp = await self._classic_get("/caic/obs/obs_report.php", params)
        page = await resp.text

        return fieldobs.FieldObservation.from_obs_page(page)
